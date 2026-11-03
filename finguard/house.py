"""House rules: a fund's method, written as data and applied by plain code. No model here.

The same filing cell means different things under different rules. A person changes the rules,
and this code applies them to the page's own text. Muse never sees a rule and never decides one.
A house note sits beside a receipt. It never changes the receipt's verdict.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .check import MATCH, WRONG, Claim, Receipt, _page_currency, _period_fits
from .core.plugin import Refused, hash_of
from .figures import money, money_in_millions
from .page import _HEADING, _KIND_SAID, PDF_LOCK, Filing, Word, _centre, _groups, _in_prose, _row_of, _same_line, squeeze

FILE_NAME = "house_rules.json"
DEFAULT_RULES = Path(__file__).parent.parent / "rules" / FILE_NAME
DIFFERS, UNAVAILABLE = "differs", "unavailable"
NO_RULE, NO_ROWS, NEEDS_PERIOD = "no_rule", "no_rows", "needs_period"

_NUMBER = re.compile(r"^\(?-?\$?€?\d[\d,]*(?:\.\d+)?\)?%?$")
_DASHES = {"—", "–", "-"}
_YEAR_WORD = re.compile(r"^(?:19|20)\d{2}$")
_MONTH_WORD = re.compile(r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[.,]?$", re.IGNORECASE)
HEADING_REACH = 32  # spanning headings sit a line or two above the years
_FOOTNOTE = re.compile(r"\s*\(\d+\)|\s*[¹²³]")
_ROW_FIELDS = ("interprets", "removes", "starts_from")
_PATTERN_FIELDS = ("method_patterns", "refuse_patterns", "allowed_patterns")


@dataclass(frozen=True)
class Rules:
    data: dict
    sha256: str
    source: str  # "repo" or "state"

    @property
    def label(self) -> str:
        return self.data["label"]

    @property
    def enabled(self) -> list[dict]:
        return [r for r in self.data["rules"] if r["enabled"]]


# Loading

def _problem_with(data) -> str | None:
    """What is wrong with these rules as data, in plain words, or None."""
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        return "they must hold a list of rules."
    if not isinstance(data.get("version"), int) or not isinstance(data.get("label"), str):
        return "they must say their version and label."
    seen: set[str] = set()
    for rule in data["rules"]:
        if not isinstance(rule, dict) or not all(isinstance(rule.get(k), str) and rule[k]
                                                 for k in ("id", "text", "result_label")):
            return "every rule needs an id, a text and a result label."
        if rule["id"] in seen:
            return f"the id {rule['id']} is used twice."
        seen.add(rule["id"])
        if not isinstance(rule.get("enabled"), bool):
            return f"{rule['id']} must say whether it is on."
        for field in _ROW_FIELDS:
            row = rule.get(field)
            if not (isinstance(row, dict) and isinstance(row.get("label"), str) and row.get("names")
                    and isinstance(row["names"], list) and all(isinstance(n, str) and n for n in row["names"])):
                return f"{rule['id']} must name the rows it uses."
        for field in _PATTERN_FIELDS:
            patterns = rule.get(field)
            if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
                return f"{rule['id']} must list its question patterns."
            try:
                for pattern in patterns:
                    re.compile(pattern)
            except re.error:
                return f"{rule['id']} has a question pattern that cannot be read."
    taken: set[str] = set()
    for rule in data["rules"]:
        names = {_plain(n) for n in rule["interprets"]["names"]} if rule["enabled"] else set()
        if names & taken:
            return f"{rule['id']} and another rule that is on both interpret the same row."
        taken |= names
    return None


def state_file(state: Path | str) -> Path:
    return Path(state) / "rules" / FILE_NAME


def load_rules(state: Path | str) -> Rules:
    """The active rules and their sha256. A person's change in the state folder overrides the repo default."""
    override = state_file(state)
    source, path = ("state", override) if override.exists() else ("repo", DEFAULT_RULES)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refused("The house rules could not be read.") from None
    problem = _problem_with(data)
    if problem:
        raise Refused(f"The house rules cannot be used: {problem}")
    return Rules(data, hash_of(data), source)


def write_rules(state: Path | str, data: dict) -> None:
    """Keep a person's approved change in the state folder, whole or not at all."""
    path = state_file(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    spare = path.with_suffix(".tmp")
    spare.write_text(json.dumps(data, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(spare, path)


def tamper_problem(rules: Rules, ledger_lines: list[dict]) -> str | None:
    """The ledger holds the hash of each approved change. A file that disagrees was changed some other way."""
    changes = [e for e in ledger_lines if e.get("kind") == "rules_changed"]
    if changes and changes[-1].get("rules_sha256") != rules.sha256:
        return "The house rules do not match the last approved change, so someone changed them outside rules.change."
    if not changes and rules.source == "state":
        return "A rules file is in the state folder, but no approved change on the ledger put it there."
    return None


def active_rules(ctx) -> Rules:
    """The rules to work under now, or a refusal if they are not the ones a person approved."""
    rules = load_rules(ctx.state)
    problem = tamper_problem(rules, ctx.ledger.lines())
    if problem:
        raise Refused(f"{problem} Restore the file. Nothing was done.")
    return rules


# Reading a table off the page

@dataclass(frozen=True)
class _Cell:
    word: Word
    row: str
    value: Decimal | None  # None for a printed dash


def _plain(label: str) -> str:
    return squeeze(_FOOTNOTE.sub("", label))


def _names(printed: str, row_rule: dict) -> bool:
    """True when the printed row is exactly one of the rule's names, so a longer row cannot pass for it."""
    return _plain(printed) in {_plain(n) for n in row_rule["names"]}


def _is_cell(word: Word) -> bool:
    return (bool(_NUMBER.match(word.text)) and not _YEAR_WORD.match(word.text)) or word.text in _DASHES


def _header_above(words: list[Word], word: Word) -> list[Word]:
    """The year headings of the table this number sits in: the nearest line of years above it."""
    years = [w for w in words if _YEAR_WORD.match(w.text) and w.cy < word.y0 and not _in_prose(w, words)]
    if not years:
        return []
    nearest = max(years, key=lambda w: w.cy)
    return sorted((w for w in years if _same_line(w, nearest)), key=lambda w: w.x0)


def _own_year(header: list[Word], word: Word) -> Word | None:
    """The year heading a number sits under, by position. None if it is not plainly under one."""
    if not header:
        return None
    gaps = [b.cx - a.cx for a, b in zip(header, header[1:])]
    reach = min(gaps) * 0.45 if gaps else 60.0
    nearest = min(header, key=lambda w: abs(w.cx - word.cx))
    return nearest if abs(nearest.cx - word.cx) <= reach else None


def _runs(line: list[Word]) -> list[list[Word]]:
    """A heading line split into one run per column, even where two columns' headings almost touch."""
    runs: list[list[Word]] = []
    for group in _groups(line):
        runs.append([])
        for word in group:
            second_month = _MONTH_WORD.match(word.text) and any(_MONTH_WORD.match(w.text) for w in runs[-1])
            if runs[-1] and (second_month or _YEAR_WORD.match(runs[-1][-1].text)):
                runs.append([])
            runs[-1].append(word)
    return runs


def _heading(year: Word, words: list[Word]) -> str:
    """The column's full heading, read at the year itself, since a number can sit off centre under it."""
    def nearest(line: list[Word]) -> list[Word]:
        return min(_runs(line), key=lambda run: abs(_centre(run) - year.cx))

    parts = [" ".join(w.text for w in nearest([w for w in words if _same_line(w, year)]))]
    tops = sorted({round(w.cy) for w in words if 0 < year.cy - w.cy < HEADING_REACH}, reverse=True)
    for y in tops:
        best = nearest([w for w in words if round(w.cy) == y])
        text = " ".join(w.text for w in best)
        if "millions" in text.lower() or "thousands" in text.lower():
            continue
        if len(best) > 6:
            break
        if _HEADING.search(text):
            parts.insert(0, text)
        if _KIND_SAID.search(text):  # the column's kind is known. A kind higher up belongs to the next column over
            break
    return " ".join(parts)


def _read(filing: Filing, page: int, word: Word) -> Decimal | None:
    if word.text in _DASHES:
        return None
    cell = filing.cell(page, word)
    if cell is None or cell.percent:
        raise ValueError(word.text)
    return -cell.amount if cell.negative else cell.amount


def _cells_above(filing: Filing, page: int, header: list[Word], year: Word, word: Word) -> list[_Cell]:
    """The cells in this column between its heading and this number, top to bottom."""
    words = filing.words(page)
    found = [w for w in words if year.cy < w.cy < word.cy and not _same_line(w, word) and _is_cell(w)
             and _own_year(header, w) is year]
    return [_Cell(w, _row_of(w, words), _read(filing, page, w)) for w in sorted(found, key=lambda w: w.cy)]


def _number(value: Decimal) -> str:
    return f"{value:,f}"


def _unmarked(row: str) -> str:
    """A row's name without its footnote mark, for a sentence a person reads."""
    return _FOOTNOTE.sub("", row)


# Applying a rule to one cell

def _work(rule: dict, filing: Filing, page: int, word: Word) -> dict:
    """What this rule makes of this cell, from the same column of the same table, or why it cannot say."""
    words = filing.words(page)
    row = filing.cell(page, word).row
    note = {"rule_id": rule["id"], "text": rule["text"], "status": UNAVAILABLE, "house_money": None, "working": "",
            "inputs": [], "row": row, "column": "", "page": page, "gap": None, "reason": ""}

    def unavailable(reason: str) -> dict:
        return {**note, "reason": reason, "working": reason}

    header = _header_above(words, word)
    year = _own_year(header, word)
    if year is None:
        return unavailable("The page does not make clear which column this number sits in, so nothing was computed.")
    column = _heading(year, words)
    note["column"] = column
    scale = filing.scale_at(page, word)
    if scale is None:
        return unavailable("The page does not say what units its table is in, so nothing was computed.")
    currency = _page_currency(filing, page)

    def in_money(value: Decimal) -> str:
        return money(abs(value) * Decimal(10) ** scale, currency, value < 0)

    def exact(value: Decimal) -> str:
        return money_in_millions(abs(value) * Decimal(10) ** scale, currency, value < 0)

    def entry(cell_word: Word, printed_row: str, value: Decimal | None) -> dict:
        return {"row": printed_row, "printed": cell_word.text,
                "box": filing.points_to_grid(page, (cell_word.x0, cell_word.y0, cell_word.x1, cell_word.y1)),
                "page": page, "money": in_money(value) if value is not None else ""}

    try:
        value = _read(filing, page, word)
        note["inputs"] = [entry(word, row, value)]
        cells = _cells_above(filing, page, header, year, word)
    except ValueError:
        return unavailable(f"A number in the column {column} could not be read, so nothing was computed.")
    removed = [c for c in cells if _names(c.row, rule["removes"])]
    if not removed:
        return unavailable(f"This table has no row for {rule['removes']['label']} above {row} under {column}, "
                           "so nothing was computed.")
    if len(removed) > 1:
        return unavailable(f"This table has more than one row for {rule['removes']['label']} under {column}, "
                           "so nothing was computed.")
    taken = removed[0]
    note["inputs"].append(entry(taken.word, taken.row, taken.value))
    if taken.value is None:
        return unavailable(f"The row {taken.row} prints a dash under {column}, so there is no number to remove.")
    if taken.value <= 0:
        return unavailable(f"The row {taken.row} prints {taken.word.text} under {column}. That is not an amount "
                           "added back, so nothing was computed.")
    result = value - taken.value
    starts = [c for c in cells if _names(c.row, rule["starts_from"])]
    if len(starts) > 1:
        return unavailable(f"This table has more than one row for {rule['starts_from']['label']} under {column}, "
                           "so nothing was computed.")
    gap_words = "This table prints no starting row, so the gap to it was not checked."
    gap = None
    if starts:
        start = starts[0]
        after = cells[cells.index(start) + 1:]
        if taken not in after or start.value is None:
            return unavailable(f"The row {taken.row} is not part of the steps from {start.row} to {row} under {column}, "
                               "so nothing was computed.")
        others = [c for c in after if c is not taken]
        if start.value + taken.value + sum((c.value or Decimal(0) for c in others), Decimal(0)) != value:
            return unavailable(f"The rows of this table do not add up to {row} under {column}, so nothing was computed.")
        difference = result - start.value
        gap = {"from_row": start.row, "printed": start.word.text, "money": in_money(start.value),
               "gap": _number(difference), "gap_money": in_money(difference), "ties": True,
               "from": entry(start.word, start.row, start.value),
               "items": [entry(c.word, c.row, c.value) for c in others]}
        printed_start = f"{in_money(start.value)} (printed as {start.word.text})"
        if difference == 0:
            gap_words = f"That equals the {_unmarked(start.row)} of {printed_start}, because nothing else is added back here."
        else:
            items = ", and ".join(f"{_unmarked(c.row)}, {exact(c.value)}" if c.value is not None
                                  else f"{_unmarked(c.row)}, which prints a dash here" for c in others)
            gap_words = (f"That is not the {_unmarked(start.row)} of {printed_start}. The gap is {exact(difference)}: {items}. "
                         f"The filing adds these back too, and {rule['id']} leaves them in. "
                         f"{exact(start.value)} + {exact(difference)} = {exact(result)}.")
    return {**note, "status": DIFFERS, "house_money": in_money(result), "gap": gap, "working": (
        f"Under {column}, {_unmarked(row)} is {in_money(value)} (printed as {word.text}) and {_unmarked(taken.row)} is "
        f"{in_money(taken.value)} (printed as {taken.word.text}). {rule['id']} does not add {rule['removes']['label']} back, so "
        f"{exact(value)} - {exact(taken.value)} = {exact(result)}. The {rule['result_label']} is {in_money(result)} rather than "
        f"{in_money(value)}. {gap_words}")}


def _word_at(filing: Filing, page: int, box: list, printed: str) -> Word | None:
    """The word a receipt points at. Code wrote the box, so it matches exactly or not at all."""
    for w in filing.words(page):
        if w.text == printed and filing.points_to_grid(page, (w.x0, w.y0, w.x1, w.y1)) == box:
            return w
    return None


def apply_to_receipts(rules: Rules, filing: Filing, receipts: list[dict]) -> list[dict]:
    """Add a house note to each receipt on a row a rule interprets. No verdict changes."""
    notes = []
    with PDF_LOCK:
        for receipt in receipts:
            # A refused receipt may point at the wrong period, so its cell says nothing about the claim.
            if receipt.get("verdict") not in (MATCH, WRONG) or receipt.get("page") is None or not receipt.get("source_text"):
                continue
            if receipt.get("filing_sha256") != filing.sha256:
                continue
            word = _word_at(filing, receipt["page"], receipt.get("box") or [], receipt["source_text"])
            cell = filing.cell(receipt["page"], word) if word else None
            rule = next((r for r in rules.enabled if cell and _names(cell.row, r["interprets"])), None)
            if rule is None:
                continue
            note = _work(rule, filing, receipt["page"], word)
            receipt["house"] = note
            notes.append({"value_text": receipt.get("value_text", ""), "verdict": receipt["verdict"], **note})
    return notes


# Questions

def _searchable(text: str, rule: dict) -> str:
    for pattern in rule["allowed_patterns"]:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return text


def forbidding_rule(rules: Rules, question: str) -> dict | None:
    """The rule on, if any, that forbids this question."""
    for rule in rules.enabled:
        text = _searchable(question, rule)
        if any(re.search(p, text, re.IGNORECASE) for p in rule["refuse_patterns"]):
            return rule
    return None


def _refuse_if_forbidden(rules: Rules, question: str) -> None:
    rule = forbidding_rule(rules, question)
    if rule:
        raise Refused(f"{rule['id']}: {rule['text']}. This question goes against that rule, so it was refused. "
                      "Nothing was sent to Muse.")


def refuse_forbidden(ctx, question: str) -> None:
    """Refuse a question a rule forbids, before anything reaches Muse. memo.draft calls this."""
    _refuse_if_forbidden(active_rules(ctx), question)


_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december")
_KINDS = {
    "six_months": r"\bsix[- ]months?\b|\b6[- ]months?\b|\b(?:first|second) half\b|\bh[12]\b|\bhalf[- ]year\b",
    "nine_months": r"\bnine[- ]months?\b|\b9[- ]months?\b",
    "quarter": r"\bthree[- ]months?\b|\b3[- ]months?\b|\bquarter\b|\bq[1-4]\b",
    "full_year": r"\b(?:full|fiscal|calendar)[- ]year\b|\bfiscal\b|\bfy\s?\d|\bannual\b|\byear ended\b|\btwelve[- ]months?\b",
}
_ENDS = {r"\bfirst half\b|\bh1\b": "june", r"\bsecond half\b|\bh2\b": "december", r"\bq1\b": "march", r"\bq2\b": "june",
         r"\bq3\b": "september", r"\bq4\b": "december"}


def read_period(question: str) -> tuple[str, int, set[str]] | None:
    """The one period a question names: its kind, year and any month its column must end in. None if not clear."""
    low = question.lower()
    years = {int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", low)}
    years |= {2000 + int(y) for y in re.findall(r"\bfy\s?(\d{2})\b", low)}
    kinds = [k for k, words in _KINDS.items() if re.search(words, low)]
    if len(years) != 1 or len(kinds) != 1:
        return None
    months = {m for m in _MONTHS if re.search(rf"\b{m}\b", low)}
    months |= {month for words, month in _ENDS.items() if re.search(words, low)}
    return kinds[0], years.pop(), months


def _column_fits(column: str, period: tuple[str, int, set[str]]) -> bool:
    kind, year, months = period
    low = column.lower()
    return _period_fits(column, Claim("", "", "", kind, year, "neutral")) and all(m in low for m in months)


def _rows_for(rule: dict, filing: Filing) -> list[tuple[int, Word, str]]:
    """Every printed cell on a row this rule interprets, with its column, found by plain text search."""
    names = [_plain(n) for n in rule["interprets"]["names"]]
    found = []
    for page in range(filing.pages):
        text = squeeze(filing.text(page))
        if not any(n in text for n in names):
            continue
        words = filing.words(page)
        for word in words:
            if not _is_cell(word) or word.text in _DASHES:
                continue
            cell = filing.cell(page, word)
            if cell is None or not _names(cell.row, rule["interprets"]):
                continue
            year = _own_year(_header_above(words, word), word)
            if year is not None:
                found.append((page, word, _heading(year, words)))
    return found


def _receipts_from(note: dict, filing: Filing) -> list[dict]:
    """Each cell the answer used, as a receipt. Code read every one of them from the page."""
    entries = list(note["inputs"])
    if note["gap"]:
        entries += [note["gap"]["from"], *note["gap"]["items"]]
    return [Receipt(MATCH, f"Read from the filing: {e['printed']} in the row {e['row']}, under {note['column']}.",
                    e["printed"], metric=e["row"], filing_sha256=filing.sha256, page=e["page"], box=e["box"],
                    source_text=e["printed"], source_money=e["money"], row=e["row"], column=note["column"]).to_dict()
            for e in entries]


_MEASURES = re.compile(r"\b(?:revenues?|net sales|sales|net income|net loss|net profit|earnings|eps|gross (?:margin|profit)|margins?|"
                       r"cash flow|cash|capex|assets|liabilities|debt|equity|expenses?|costs?|ebitda)\b", re.IGNORECASE)


def _other_measure(rule: dict, question: str) -> str | None:
    """A measure the question names that this rule does not speak to. Its answer would look like an answer to that."""
    text = question
    for row in ("interprets", "removes", "starts_from"):
        for name in rule[row]["names"] + [rule[row]["label"]]:
            text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    found = _MEASURES.search(text)
    return found.group(0) if found else None


def _answer_one(rule: dict, filing: Filing, question: str) -> dict:
    base = {"rule_id": rule["id"], "text": rule["text"], "house_money": None, "working": "", "inputs": [],
            "gap": None, "receipts": [], "column": "", "page": None}

    def said(status: str, answer: str, **extra) -> dict:
        return {**base, "status": status, "answer": answer, "reason": answer, "working": answer, **extra}

    label = rule["interprets"]["label"]
    other = _other_measure(rule, question)
    if other:
        return said(NO_RULE, f"{rule['id']} speaks to {label}, not {other}, so nothing was computed.")
    rows = _rows_for(rule, filing)
    if not rows:
        return said(NO_ROWS, f"This filing has no row named {label}, so {rule['id']} has nothing to apply to.")
    columns = list(dict.fromkeys(c for _, _, c in rows))
    period = read_period(question)
    if period is None:
        return said(NEEDS_PERIOD, f"Name one period, so the right column can be read. This filing prints {label} under: "
                                  f"{'; '.join(columns)}.", columns=columns)
    fits = [row for row in rows if _column_fits(row[2], period)]
    if not fits:
        return said(UNAVAILABLE, f"This filing prints {label} only under: {'; '.join(columns)}. "
                                 "It has no column for the period asked, so nothing was computed.", columns=columns)
    named = list(dict.fromkeys(c for _, _, c in fits))
    if len(named) > 1:
        return said(NEEDS_PERIOD, f"More than one column fits that period: {'; '.join(named)}. Name one.", columns=named)
    notes = [_work(rule, filing, page, word) for page, word, _ in fits]
    done = [n for n in notes if n["status"] == DIFFERS]
    if not done:
        closest = max(notes, key=lambda n: len(n["inputs"]))  # the table that got furthest gives the truest reason
        return said(UNAVAILABLE, closest["reason"], column=named[0], page=closest["page"], inputs=closest["inputs"],
                    receipts=_receipts_from(closest, filing))
    if len({n["house_money"] for n in done}) > 1:
        return said(UNAVAILABLE, f"Two tables in this filing give different results under {named[0]}, so nothing was "
                                 "chosen.", column=named[0])
    note = done[0]
    return {**base, **note, "receipts": _receipts_from(note, filing), "answer": (
        f"Under {rule['id']}, the {rule['result_label']} for {note['column']} is {note['house_money']}. {note['working']}")}


def answer_question(rules: Rules, filing: Filing, question: str) -> dict:
    """Answer in code, from cells, with no memo and no Muse. A forbidden question is refused first."""
    _refuse_if_forbidden(rules, question)
    matched = [r for r in rules.enabled
               if any(re.search(p, question, re.IGNORECASE) for p in r["method_patterns"])]
    if not matched:
        return {"status": NO_RULE, "answer": "No house rule that is on speaks to this question, so nothing was computed.",
                "answers": [], "receipts": [], "working": "", "inputs": [], "house_money": None}
    with PDF_LOCK:
        answers = [_answer_one(rule, filing, question) for rule in matched]
    first = answers[0]
    return {**first, "answer": " ".join(a["answer"] for a in answers), "answers": answers,
            "receipts": [r for a in answers for r in a["receipts"]]}
