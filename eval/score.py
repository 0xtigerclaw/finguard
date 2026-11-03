"""The public score: how many true numbers pass, and whether any false one does.

Replays saved Muse replies by default, with no API. `--live --round N` calls Muse and saves
replies for round N. Muse is never told the memo's number, so one reply serves a true claim
and all of its altered versions.

Two sets of altered numbers are kept apart. The old classes stay as they were, so results stay
comparable. The new classes come from the audit: they also move the box to a neighbouring cell,
and they say the number with a memo's wording. The memo path runs on the saved memos.

Pointing takes the same path as a real check: the first box, then one second look when that box
does not pick out exactly one number. A replay with no saved second look keeps the first box.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from math import exp, lgamma, log
from pathlib import Path

from finguard.check import (MATCH, REFUSED, WRONG, Claim, _found_count, _locate_again, candidate_pages, check_memo, decide, list_claims,
                            locate, period_stated, skipped_numbers)
from finguard.errors import MuseError
from finguard.figures import money, numbers_in_memo, read_figure
from finguard.muse import LiveTransport, Muse, RecordingTransport, ReplayTransport
from finguard.page import Filing, _NUMERIC, _same_line
from finguard.record import Run, read_lines

HERE = Path(__file__).parent
DATA = HERE.parent / "data"
IN_PER_M, OUT_PER_M = 1.25, 4.25  # muse-spark-1.3 list price, dollars per million tokens
OTHER_KIND = {"quarter": "full_year", "six_months": "quarter", "nine_months": "quarter",
              "full_year": "six_months", "as_of": "full_year"}


def upper_95(hits: int, n: int) -> float | None:
    """The highest true rate still 5% likely to show `hits` or fewer in n tries: a one-sided 95% upper bound."""
    if n == 0:
        return None
    if hits >= n:
        return 1.0
    low, high = hits / n, 1.0
    for _ in range(60):
        rate = (low + high) / 2
        chance = sum(exp(lgamma(n + 1) - lgamma(k + 1) - lgamma(n - k + 1) + k * log(rate) + (n - k) * log(1 - rate)) for k in range(hits + 1))
        low, high = (rate, high) if chance > 0.05 else (low, rate)
    return round(high, 4)


def limits(present: list[dict], true_exact: dict, false_numbers: int, false_passed: int) -> dict:
    """Coverage and risk, the standard terms for a system allowed to refuse, each with what the sample can support."""
    answered = true_exact[MATCH] + true_exact[WRONG]
    n = len(present)
    sources_passed = sum(any(v == MATCH for v in [*p["false"].values(), *p["new_false"].values()]) for p in present)
    return {
        "coverage": {"answered": answered, "of": n, "rate": round(answered / n, 4) if n else None},
        "risk": {"wrong": true_exact[WRONG], "of": answered, "rate": round(true_exact[WRONG] / answered, 4) if answered else None,
                 "upper_95": upper_95(true_exact[WRONG], answered)},
        "refused": true_exact[REFUSED],
        "true_called_wrong": {"wrong": true_exact[WRONG], "of": n, "upper_95": upper_95(true_exact[WRONG], n)},
        "false_pass": {"passed": false_passed, "of": false_numbers, "rate": round(false_passed / false_numbers, 6) if false_numbers else None,
                       "upper_95": upper_95(false_passed, false_numbers),
                       "source_numbers": n, "upper_95_counting_each_source_once": upper_95(sources_passed, n),
                       "note": "The altered numbers are not independent. They are made from the source numbers, so the "
                               "bound counting each source once is the careful one."},
    }


def plain(printed: str) -> tuple[Decimal, str, bool]:
    """The printed cell as a magnitude, the digits it was printed with, and whether it is a percentage."""
    digits = re.sub(r"[()%,]", "", printed)
    return Decimal(digits), digits, "%" in printed


def exact_text(amount: Decimal, currency: str, percent: bool, per_share: bool, negative: bool = False) -> str:
    if percent:
        return f"{'-' if negative else ''}{amount}%"
    if per_share:
        return f"{currency}{amount}"
    return f"{currency}{amount:,}m"


def natural_text(amount: Decimal, currency: str, percent: bool, per_share: bool, negative: bool = False) -> str:
    if percent or per_share:
        return exact_text(amount, currency, percent, per_share, negative)
    return money(amount * Decimal(10) ** 6, currency)


def other_columns(filing: Filing, page: int, printed: str) -> list[str]:
    """Other numbers printed on the same row: what a memo quoting the wrong column would say."""
    words = filing.words(page)
    cell = printed.rstrip("%")
    here = next((w for w in words if w.text.rstrip("%") == cell), None)
    if here is None:
        return []
    return [w.text for w in words if _same_line(w, here) and _NUMERIC.match(w.text) and w.text.rstrip("%") != cell]


_OPPOSITE_WORDS = (("expense", "benefit"), ("used", "provided"), ("provided", "used"), ("loss", "income"), ("income", "loss"),
                   ("profit", "loss"), ("gain", "loss"), ("increase", "decrease"), ("decrease", "increase"))
_CALENDAR_QUARTER = {3: 1, 6: 2, 9: 3, 12: 4}
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


def _month_of(column: str) -> int:
    found = [i for i, m in enumerate(_MONTHS, 1) if re.search(rf"\b{m[:3]}[a-z]*\b", column, re.IGNORECASE)]
    return found[-1] if found else 0


def _opposite(metric: str) -> str | None:
    if "(" in metric:
        return None
    for word, other in _OPPOSITE_WORDS:
        if re.search(rf"\b{word}\b", metric, re.IGNORECASE):
            return re.sub(rf"\b{word}\b", other, metric, count=1, flags=re.IGNORECASE)
    return None


def _word_of(filing: Filing, page: int, first) -> object | None:
    return next((w for w in filing.words(page) if w.text == first.source_text
                 and filing.points_to_grid(page, (w.x0, w.y0, w.x1, w.y1)) == first.box), None)


def _neighbours(filing: Filing, page: int, word) -> dict:
    """The nearest cell on each side of this one: left and right on its row, up and down in its column."""
    words = [w for w in filing.words(page) if _NUMERIC.match(w.text) and w is not word and filing.cell(page, w)]
    line = [w for w in words if _same_line(w, word)]
    column = [w for w in words if not _same_line(w, word) and w.x1 >= word.x0 - 4 and w.x0 <= word.x1 + 4]
    found = {"next_column": min((w for w in line if w.x0 > word.x1), key=lambda w: w.x0, default=None),
             "previous_column": max((w for w in line if w.x1 < word.x0), key=lambda w: w.x1, default=None),
             "next_row": min((w for w in column if w.cy > word.cy), key=lambda w: w.cy, default=None),
             "previous_row": max((w for w in column if w.cy < word.cy), key=lambda w: w.cy, default=None)}
    return {k: w for k, w in found.items() if w is not None}


def new_classes(item: dict, first, exact: Claim, amount: Decimal,
                currency: str, percent: bool, per_share: bool) -> dict:
    """Every altered number the audit asks for, built from one saved reply: name -> (claim, reply)."""
    metric, kind, year, direction = item["metric"], item["period_kind"], item["period_year"], item["direction"]
    value = exact.value_text
    negative = first.source_text.startswith("(")

    def said(quote: str, shown: str = value, **kw) -> Claim:
        return Claim(quote, shown, kw.get("metric", metric), kw.get("kind", kind), kw.get("year", year), kw.get("direction", direction))

    def plain(shown: str, **kw) -> Claim:
        return said(f"x {shown}", shown, **kw)

    out: dict = {}
    # The memo's words give another period than the column.
    month = _month_of(first.column)
    if kind == "quarter" and month in _CALENDAR_QUARTER:
        other = _CALENDAR_QUARTER[month] % 4 + 1
        out["memo_says_other_quarter"] = said(f"{metric} in Q{other} {year} was {value}.")
    if kind == "as_of" and month:
        out["memo_says_other_date"] = said(f"At {_MONTHS[month % 12]} 15, {year}, {metric} was {value}.")
    if kind == "six_months" and month in (6, 12):
        out["memo_says_other_half"] = said(f"{metric} in H{2 if month == 6 else 1} {year} was {value}.")
    if month != 12:
        out["memo_says_fiscal"] = said(f"{metric} for fiscal {year} was {value}.")
    # Wording that says it is not a plain level.
    out["memo_says_growth"] = said(f"{metric} grew by {value}.")
    out["memo_says_a_bound"] = said(f"{metric} was under {value}.")
    out["memo_says_a_forecast"] = said(f"{metric} will reach {value}.")
    # Names.
    opposite = _opposite(metric)
    if opposite:
        out["opposite_word"] = plain(value, metric=opposite, direction="neutral")
    out["another_row_name"] = plain(value, metric=f"Deferred {metric}", direction="neutral")
    if not re.search(r"adjusted|non-gaap", metric, re.IGNORECASE):
        out["qualifier_added"] = plain(value, metric=f"Adjusted {metric}")
    else:
        out["qualifier_dropped"] = plain(value, metric=re.sub(r"\s*(?:adjusted|non-gaap)\s*", " ", metric, flags=re.IGNORECASE).strip())
    # Units and size.
    if percent:
        out["percent_as_money"] = plain(f"{currency}{amount}m")
        if amount >= Decimal("0.5"):
            out["zero"] = plain("0%")
    else:
        out["money_as_percent"] = plain(f"{amount}%")
        out["currency_pound"] = plain(value.replace(currency, "£"))
        out["currency_yen"] = plain(value.replace(currency, "¥"))
        out["currency_canadian"] = plain(value.replace(currency, "C$"))
        out["currency_code"] = plain(value.replace(currency, "GBP "))
        out["no_currency"] = plain(value.replace(currency, ""))
        if not per_share:
            out["thousands_for_millions"] = plain(f"{currency}{amount:,}k")
            size = amount / 1000
            whole = size.quantize(Decimal(1), rounding="ROUND_HALF_UP")
            if whole and abs(size - whole) > size * Decimal("0.05"):
                out["rough_rounding"] = plain(f"{currency}{whole}bn")
            if not whole:
                out["zero"] = plain(f"{currency}0bn")
        elif amount >= Decimal("0.5") and amount.quantize(Decimal(1), rounding="ROUND_HALF_UP"):
            whole = amount.quantize(Decimal(1), rounding="ROUND_HALF_UP")
            if abs(amount - whole) > amount * Decimal("0.05"):
                out["rough_rounding"] = plain(f"{currency}{whole}")
    # Sign.
    if not negative:
        out["sign_flipped"] = plain(f"-{value}", direction="neutral")
        if direction == "neutral" and not percent:
            out["loss_on_a_positive_row"] = plain(value, direction="loss")
    elif not re.search(r"loss|cost|expense|used|purchase|payment|provision|deficit|repurchase|dividend", metric, re.IGNORECASE):
        out["unsigned_on_a_negative_row"] = plain(value.lstrip("-"), direction="neutral")
    return out


def moved_box(item: dict, filing: Filing, reply: dict, first, currency: str, per_share: bool) -> dict:
    """The box moved to a neighbouring cell, and the memo holding that cell's number: name -> (claim, reply)."""
    page = first.page
    word = _word_of(filing, page, first)
    out: dict = {}
    if word is None:
        return out
    for where, other in _neighbours(filing, page, word).items():
        cell = filing.cell(page, other)
        if cell.amount == filing.cell(page, word).amount:
            continue  # the same figure is not a false number
        shown = exact_text(cell.amount, currency, cell.percent, per_share, cell.negative and cell.percent)
        quote = f"{item['metric']} for {first.column} was {shown}."
        claim = Claim(quote, shown, item["metric"], item["period_kind"], item["period_year"], item["direction"])
        box = filing.points_to_grid(page, (other.x0, other.y0, other.x1, other.y1))
        out[f"wrong_cell_{where}"] = (claim, {**reply, "box": box})  # Muse keeps the true row's name
        out[f"wrong_cell_{where}_own_label"] = (claim, {**reply, "box": box, "row_label": cell.row})
    return out


def score_item(item: dict, filing: Filing, pages: list[int], reply: dict | None, error: str | None) -> dict:
    amount, digits, percent = plain(item["printed"])
    places = len(digits.split(".")[1]) if "." in digits else 0
    step = Decimal(1).scaleb(-places)
    per_share = bool(re.search(r"\bper\b.*\bshare\b", item["metric"].lower()))
    currency = "€" if "ASML" in item["filing"] else "$"
    make = lambda value, **kw: Claim(f"The figure was {value}.", value, item["metric"], kw.get("kind", item["period_kind"]),
                                     kw.get("year", item["period_year"]), kw.get("direction", item["direction"]))
    verdict = lambda c: decide(c, filing, reply, pages) if reply is not None else None
    negative = percent and item["printed"].startswith("(")
    exact = make(exact_text(amount, currency, percent, per_share, negative))
    natural = make(natural_text(amount, currency, percent, per_share, negative))
    out = {"metric": item["metric"], "filing": item["filing"], "period": f'{item["period_kind"]} {item["period_year"]}',
           "printed": item["printed"], "error": error}
    if reply is None:
        out.update(true={"exact": REFUSED, "natural": REFUSED}, right_cell=False, false={}, new_false={}, new_passed=[], why=f"call failed: {error}")
        return out
    first = verdict(exact)
    out["true"] = {"exact": first.verdict, "natural": verdict(natural).verdict}
    out["why"] = first.why
    out["right_cell"] = first.source_text.rstrip("%") == item["printed"].rstrip("%")
    out["muse_box"], out["box"], out["page"] = first.muse_box, first.box, first.page

    altered = {
        "ten_times": make(exact_text(amount * 10, currency, percent, per_share)),
        "ten_percent_more": make(exact_text((amount * Decimal("1.1")).quantize(step), currency, percent, per_share)),
        "last_digit": make(exact_text(amount + step, currency, percent, per_share)),
        "wrong_year": make(exact.value_text, year=item["period_year"] - 1),
        "wrong_period": make(exact.value_text, kind=OTHER_KIND[item["period_kind"]]),
    }
    if not percent:
        altered["wrong_currency"] = make(exact_text(amount, "$" if currency == "€" else "€", percent, per_share))
    if item["direction"] in ("gain", "loss"):
        altered["flipped"] = make(exact.value_text, direction="gain" if item["direction"] == "loss" else "loss")
    for n, other in enumerate(other_columns(filing, first.page, item["printed"]) if first.page is not None else []):
        figure = read_figure(other.replace(",", "").strip("()%") or "0")
        if figure and abs(figure.amount) != amount:
            altered[f"other_column_{n}"] = make(exact_text(abs(figure.amount), currency, percent, per_share))
    # An alteration that rounds back to the true figure, such as 10% more than 4%, changes nothing. It is not false.
    out["false"] = {name: verdict(c).verdict for name, c in altered.items()
                    if not (c.value_text == exact.value_text and c.period_kind == exact.period_kind
                            and c.period_year == exact.period_year and c.direction == exact.direction)}
    out["new_false"], out["new_passed"] = {}, []
    if first.verdict == MATCH:  # the new classes start from a cell known to be the right one
        cases = {name: (c, reply) for name, c in new_classes(item, first, exact, amount, currency,
                                                              percent, per_share).items()}
        cases.update(moved_box(item, filing, reply, first, currency, per_share))
        for name, (c, used) in cases.items():
            r = decide(c, filing, used, pages)
            out["new_false"][name] = r.verdict
            if r.verdict == MATCH:
                out["new_passed"].append({"class": name, "value": c.value_text, "metric": c.metric, "row": r.row, "column": r.column,
                                          "source": r.source_text, "page": r.page})
    return out


SAVED_MEMOS = {"anthropic_memo": ("anthropic_memo.txt", "ANTH-S1-2026.pdf"),
               "anthropic_memo_corrected": ("anthropic_memo_corrected.txt", "ANTH-S1-2026.pdf"),
               "asml_memo": ("asml_memo.txt", "ASML-Q3-2026.pdf"), "nebius_memo": ("nebius_memo.txt", "NBIS-Q2-2026.pdf")}
MEMOS_THAT_HIDE_A_NUMBER = ("a $10-billion charge", "margins of 5-6%", "revenue of EUR1.6bn", "revenue of USD16,286m",
                            "revenue of c.16bn", "$.5bn", "revenue of .5bn", "x16bn", "Revenue in June 12 million",
                            "revenue in May 16.3bn", "Mar 3.5bn of debt", "Oct 12%", "sixteen billion dollars")


def memo_path() -> dict:
    """The saved memos through list_claims, skipped_numbers, period_stated and the check, then memos built to slip through it."""
    folder = Path(tempfile.mkdtemp(prefix="finguard-score-memos-"))
    root = HERE.parent
    saved = {}
    for name, (text, filing_name) in SAVED_MEMOS.items():
        memo = (root / "examples" / text).read_text().strip()
        filing = Filing(DATA / filing_name)
        replies = root / "tests" / "recordings" / name
        muse = Muse(ReplayTransport(replies), Run(folder / f"{name}-claims"))
        claims = list_claims(memo, muse)
        muse = Muse(ReplayTransport(replies), Run(folder / f"{name}-check"))
        receipts = check_memo(memo, filing, muse, Run(folder / f"{name}-run"))
        saved[name] = {"claims": len(claims), "period_stated": sum(period_stated(memo, c) for c in claims),
                       "skipped": skipped_numbers(memo, claims),
                       "verdicts": {r.value_text: r.verdict for r in receipts}}
    twice = "H1 2026 six months. Revenue was $16.3bn. Full-year 2025. Revenue was $16.3bn."
    one = Claim("Revenue was $16.3bn.", "$16.3bn", "Revenue", "six_months", 2026, "neutral")
    slice_memo = "In the six months of 2026 revenue was $116.3bn."

    class Lists:  # hands back a hostile claim list, with no API
        def ask(self, *_args, **_kw):
            return {"claims": [{"quote": slice_memo, "value_text": "16.3bn", "metric": "Revenue", "period_kind": "six_months",
                                "period_year": 2026, "direction": "neutral"}]}

    unstated = "Full-year 2025 revenue was $16.3bn. We plan a six-month review in 2026."
    passes = {"number_hidden_from_the_finder": [m for m in MEMOS_THAT_HIDE_A_NUMBER if not numbers_in_memo(m)],
              "quote_twice_second_copy_not_flagged": [] if skipped_numbers(twice, [one]) else [twice],
              "value_slice_kept": [slice_memo] if list_claims(slice_memo, Lists()) else [],
              "period_from_a_later_sentence": [unstated] if period_stated(
                  unstated, Claim("Full-year 2025 revenue was $16.3bn.", "$16.3bn", "Revenue", "six_months", 2026, "neutral")) else []}
    return {"saved": saved, "built_to_slip_through": {k: {"cases": len(MEMOS_THAT_HIDE_A_NUMBER) if k.startswith("number") else 1,
                                                          "passed": len(v), "examples": v} for k, v in passes.items()}}


def score_round(name: str, live: bool = False, trial: int | None = None) -> dict:
    """Score one round and return what the results file holds. A trial repeats a round live under its own names."""
    key_file = {"heldout": "answer_key_heldout.json", "fresh": "answer_key_fresh.json", "final": "answer_key_final.json"}.get(name, "answer_key.json")
    key = json.loads((HERE / key_file).read_text())
    label = f"{name}_trial{trial}" if trial else name
    saved = HERE / "recordings" / f"round{label}"
    folder = Path(tempfile.mkdtemp(prefix="finguard-score-"))
    run = Run(folder)
    transport = RecordingTransport(LiveTransport(timeout=300), saved) if live else ReplayTransport(saved)
    muse = Muse(transport, run)
    filings = {name: Filing(DATA / name) for name in {i["filing"] for i in key["present"] + key["absent"]}}

    jobs = []
    for item in key["present"] + key["absent"]:
        filing = filings[item["filing"]]
        claim = Claim("x", "x", item["metric"], item["period_kind"], item["period_year"], "neutral")
        pages = candidate_pages(filing, claim)
        for page in pages:
            filing.image(page)
        jobs.append((item, claim, filing, pages))

    looks = {"asked": 0, "replied": 0, "not_saved": 0, "failed": 0}

    def ask(job):
        _, claim, filing, pages = job
        if not pages:
            return {"found": False, "page": 0, "box": [], "row_label": ""}, None
        try:
            first = locate(claim, filing, pages, muse)
        except MuseError as e:
            return None, e.code
        if _found_count(filing, pages, first) in (None, 1):
            return first, None
        looks["asked"] += 1
        try:
            again = _locate_again(claim, filing, pages, first, muse)
            looks["replied"] += 1
            return again, None
        except MuseError as e:
            looks["not_saved" if e.code == "no_recording" else "failed"] += 1
            return first, None

    with ThreadPoolExecutor(4) as pool:
        replies = list(pool.map(ask, jobs))

    present, absent = [], []
    for (item, claim, filing, pages), (reply, error) in zip(jobs, replies):
        if "printed" in item:
            present.append(score_item(item, filing, pages, reply, error))
        else:
            c = Claim("x", item["value"], item["metric"], item["period_kind"], item["period_year"], "neutral")
            r = decide(c, filing, reply, pages) if reply is not None else None
            absent.append({"metric": item["metric"], "filing": item["filing"], "verdict": r.verdict if r else REFUSED,
                           "why": r.why if r else f"call failed: {error}"})

    count = lambda values, v: sum(x == v for x in values)
    true_exact = [p["true"]["exact"] for p in present]
    true_natural = [p["true"]["natural"] for p in present]
    false_all = [v for p in present for v in p["false"].values()]

    def by_class(field: str) -> dict:
        names = sorted({n for p in present for n in p.get(field, {})})
        return {n: {v: sum(p.get(field, {}).get(n) == v for p in present) for v in (MATCH, REFUSED, WRONG)} for n in names}

    new_all = [v for p in present for v in p["new_false"].values()]
    calls = [e for e in read_lines(folder / "run.jsonl") if e["kind"] == "muse_call"]
    # A second look with no saved reply is not a call Muse failed: it was never made.
    unsaved = looks["not_saved"]
    usage = [json.loads(e["reply"]).get("usage", {}) for e in calls if e["reply"]]
    cost = (sum(u.get("prompt_tokens", 0) for u in usage) * IN_PER_M
            + sum(u.get("completion_tokens", 0) for u in usage) * OUT_PER_M) / 1e6
    summary = {
        "round": label, "model": muse.model, "calls": len(calls) - unsaved,
        "failed_calls": sum(1 for e in calls if e["error"]) - unsaved,
        "second_looks": looks,
        "true_numbers": len(present),
        "true_exact": {v: count(true_exact, v) for v in (MATCH, REFUSED, WRONG)},
        "true_natural": {v: count(true_natural, v) for v in (MATCH, REFUSED, WRONG)},
        "right_cell": sum(p["right_cell"] for p in present),
        "false_numbers": len(false_all),
        "false_passed": count(false_all, MATCH),
        "false_called_wrong": count(false_all, WRONG),
        "false_refused": count(false_all, REFUSED),
        "old_false_by_class": by_class("false"),
        "new_false_numbers": len(new_all),
        "new_false_passed": count(new_all, MATCH),
        "new_false_called_wrong": count(new_all, WRONG),
        "new_false_refused": count(new_all, REFUSED),
        "new_false_by_class": by_class("new_false"),
        "absent_metrics": len(absent),
        "absent_passed": sum(a["verdict"] == MATCH for a in absent),
        "estimated_cost_usd": round(cost, 2),
    }
    by_filing = {}
    for name in sorted(filings):
        rows = [p for p in present if p["filing"] == name]
        by_filing[name] = {"true": len(rows), "matched": sum(p["true"]["exact"] == MATCH for p in rows)}
    summary["by_filing"] = by_filing
    engine = b"".join((HERE.parent / "finguard" / n).read_bytes() for n in ("check.py", "page.py", "figures.py"))
    summary["engine_sha256"] = hashlib.sha256(engine).hexdigest()
    memos = memo_path()
    summary["memo_path_false_passed"] = sum(v["passed"] for v in memos["built_to_slip_through"].values())
    summary.update(limits(present, summary["true_exact"], len(false_all) + len(new_all), count(false_all, MATCH) + count(new_all, MATCH)))
    return {"summary": summary, "present": present, "absent": absent, "memo_path": memos, "unused_replies": len(transport.unused) if not live else 0}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--round", default="1", help="1, 2, heldout, fresh or final: names the saved replies and the results file")
    parser.add_argument("--trial", type=int, help="A repeat of the round, saved under its own names. Never touches the round's own files.")
    args = parser.parse_args()
    scored = score_round(args.round, args.live, args.trial)
    unused = scored.pop("unused_replies")
    summary, present, absent = scored["summary"], scored["present"], scored["absent"]
    out = HERE / f"results_round{summary['round']}.json"
    out.write_text(json.dumps(scored, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=1))
    for p in present:
        if p["true"]["exact"] != MATCH:
            print("MISSED", Path(p["filing"]).name[:4], p["metric"], p["period"], p["printed"], "->", p["true"]["exact"], "|", p.get("why", "")[:120])
        for name, v in p["false"].items():
            if v == MATCH:
                print("FALSE PASS", Path(p["filing"]).name[:4], p["metric"], p["period"], name)
        for passed in p["new_passed"]:
            print("NEW FALSE PASS", Path(p["filing"]).name[:4], p["metric"], p["period"], passed["class"], passed["value"], "|", passed["row"], "|", passed["column"])
    for a in absent:
        print("ABSENT", Path(a["filing"]).name[:4], a["metric"], a["verdict"], "|", a["why"][:90])
    if unused:
        print(f"Note: {unused} saved replies were not needed by this version of the code.")


if __name__ == "__main__":
    main()
