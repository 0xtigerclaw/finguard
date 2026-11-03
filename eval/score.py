"""The public score: how many true numbers pass, and whether any false one does.

Replays saved Muse replies by default, with no API. `--live --round N` calls Muse and saves
replies for round N. Muse is never told the memo's number, so one reply serves a true claim
and all of its altered versions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from finguard.check import MATCH, REFUSED, WRONG, Claim, candidate_pages, decide, locate
from finguard.errors import MuseError
from finguard.figures import money, read_figure
from finguard.muse import LiveTransport, Muse, RecordingTransport, ReplayTransport
from finguard.page import Filing, _NUMERIC, _same_line
from finguard.record import Run, read_lines

HERE = Path(__file__).parent
DATA = HERE.parent / "data"
IN_PER_M, OUT_PER_M = 1.25, 4.25  # muse-spark-1.3 list price, dollars per million tokens
OTHER_KIND = {"quarter": "full_year", "six_months": "quarter", "nine_months": "quarter",
              "full_year": "six_months", "as_of": "full_year"}


def plain(printed: str) -> tuple[Decimal, str, bool]:
    """The printed cell as a magnitude, the digits it was printed with, and whether it is a percentage."""
    digits = re.sub(r"[()%,]", "", printed)
    return Decimal(digits), digits, "%" in printed


def exact_text(amount: Decimal, currency: str, percent: bool, per_share: bool) -> str:
    if percent:
        return f"{amount}%"
    if per_share:
        return f"{currency}{amount}"
    return f"{currency}{amount:,}m"


def natural_text(amount: Decimal, currency: str, percent: bool, per_share: bool) -> str:
    if percent or per_share:
        return exact_text(amount, currency, percent, per_share)
    return money(amount * Decimal(10) ** 6, currency)


def other_columns(filing: Filing, page: int, printed: str) -> list[str]:
    """Other numbers printed on the same row: what a memo quoting the wrong column would say."""
    words = filing.words(page)
    cell = printed.rstrip("%")
    here = next((w for w in words if w.text.rstrip("%") == cell), None)
    if here is None:
        return []
    return [w.text for w in words if _same_line(w, here) and _NUMERIC.match(w.text) and w.text.rstrip("%") != cell]


def score_item(item: dict, filing: Filing, pages: list[int], reply: dict | None, error: str | None) -> dict:
    amount, digits, percent = plain(item["printed"])
    places = len(digits.split(".")[1]) if "." in digits else 0
    step = Decimal(1).scaleb(-places)
    per_share = "per share" in item["metric"].lower() or "per ordinary share" in item["metric"].lower()
    currency = "€" if "ASML" in item["filing"] else "$"
    make = lambda value, **kw: Claim(f"x {value}", value, item["metric"], kw.get("kind", item["period_kind"]),
                                     kw.get("year", item["period_year"]), kw.get("direction", item["direction"]))
    verdict = lambda c: decide(c, filing, reply, pages) if reply is not None else None
    exact = make(exact_text(amount, currency, percent, per_share))
    natural = make(natural_text(amount, currency, percent, per_share))
    out = {"metric": item["metric"], "filing": item["filing"], "period": f'{item["period_kind"]} {item["period_year"]}',
           "printed": item["printed"], "error": error}
    if reply is None:
        out.update(true={"exact": REFUSED, "natural": REFUSED}, right_cell=False, false={}, why=f"call failed: {error}")
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
    out["false"] = {name: verdict(c).verdict for name, c in altered.items()}
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--round", default="1", help="1, 2 or heldout: names the saved replies and the results file")
    args = parser.parse_args()
    key_file = "answer_key_heldout.json" if args.round == "heldout" else "answer_key.json"
    key = json.loads((HERE / key_file).read_text())
    saved = HERE / "recordings" / f"round{args.round}"
    folder = Path(tempfile.mkdtemp(prefix="finguard-score-"))
    run = Run(folder)
    transport = RecordingTransport(LiveTransport(timeout=300), saved) if args.live else ReplayTransport(saved)
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

    def ask(job):
        _, claim, filing, pages = job
        if not pages:
            return {"found": False, "page": 0, "box": [], "row_label": ""}, None
        try:
            return locate(claim, filing, pages, muse), None
        except MuseError as e:
            return None, e.code

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
    calls = [e for e in read_lines(folder / "run.jsonl") if e["kind"] == "muse_call"]
    usage = [json.loads(e["reply"]).get("usage", {}) for e in calls if e["reply"]]
    cost = (sum(u.get("prompt_tokens", 0) for u in usage) * IN_PER_M
            + sum(u.get("completion_tokens", 0) for u in usage) * OUT_PER_M) / 1e6
    summary = {
        "round": args.round, "model": muse.model, "calls": len(calls),
        "failed_calls": sum(1 for e in calls if e["error"]),
        "true_numbers": len(present),
        "true_exact": {v: count(true_exact, v) for v in (MATCH, REFUSED, WRONG)},
        "true_natural": {v: count(true_natural, v) for v in (MATCH, REFUSED, WRONG)},
        "right_cell": sum(p["right_cell"] for p in present),
        "false_numbers": len(false_all),
        "false_passed": count(false_all, MATCH),
        "false_called_wrong": count(false_all, WRONG),
        "false_refused": count(false_all, REFUSED),
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
    out = HERE / f"results_round{args.round}.json"
    out.write_text(json.dumps({"summary": summary, "present": present, "absent": absent}, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=1))
    for p in present:
        if p["true"]["exact"] != MATCH:
            print("MISSED", Path(p["filing"]).name[:4], p["metric"], p["period"], p["printed"], "->", p["true"]["exact"], "|", p.get("why", "")[:120])
        for name, v in p["false"].items():
            if v == MATCH:
                print("FALSE PASS", Path(p["filing"]).name[:4], p["metric"], p["period"], name)
    for a in absent:
        print("ABSENT", Path(a["filing"]).name[:4], a["metric"], a["verdict"], "|", a["why"][:90])
    if not args.live:
        transport.assert_all_used()


if __name__ == "__main__":
    main()
