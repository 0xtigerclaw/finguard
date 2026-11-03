"""Read a figure from text and compare two figures. No model is involved here."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

SCALES = {
    "k": 3, "thousand": 3, "thousands": 3,
    "m": 6, "mn": 6, "million": 6, "millions": 6,
    "b": 9, "bn": 9, "billion": 9, "billions": 9,
    "t": 12, "tn": 12, "trillion": 12,
}
_FIGURE = re.compile(
    r"(?P<open>\()?\s*(?P<minus>[-−–])?\s*(?P<cur>[$€£])?\s*(?P<minus2>[-−–])?\s*"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<scale>trillion|billions?|millions?|thousands?|bn|mn|tn|[kmbt])?(?![a-z])\s*"
    r"(?P<pct>%)?\s*(?P<close>\))?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Figure:
    """A number as written: its size in plain units, and how many decimals the writer kept."""

    amount: Decimal
    power: int  # the scale it was written at, 6 for "$12m"
    decimals: int
    negative: bool
    currency: str | None
    percent: bool
    text: str


def read_figure(text: str) -> Figure | None:
    """The one figure in `text`, or None if it holds none or more than one."""
    found = [m for m in _FIGURE.finditer(text) if m.group("num")]
    if len(found) != 1:
        return None
    m = found[0]
    digits = m.group("num").replace(",", "")
    if m.group("num").count(",") and not re.fullmatch(r"\d{1,3}(,\d{3})*(\.\d+)?", m.group("num")):
        return None
    power = SCALES[m.group("scale").lower()] if m.group("scale") else 0
    negative = bool(m.group("minus") or m.group("minus2")) or bool(m.group("open") and m.group("close"))
    decimals = len(digits.split(".")[1]) if "." in digits else 0
    return Figure(Decimal(digits).scaleb(power), power, decimals, negative,
                  m.group("cur"), bool(m.group("pct")), m.group(0).strip())


def same_size(claim: Figure, source_amount: Decimal) -> bool:
    """True when the source rounds to the claim at the precision the claim was written in.

    "$1.6bn" agrees with 1,618 million. "$1.7bn" and "$1,600m" do not.
    """
    unit = Decimal(1).scaleb(claim.power - claim.decimals)
    rounded = (abs(source_amount) / unit).quantize(Decimal(1), rounding=ROUND_HALF_UP) * unit
    return rounded == abs(claim.amount)


def money(amount: Decimal, currency: str, negative: bool = False) -> str:
    """One way to write money: $80.7bn, -$693m, $582.3m, $0.13."""
    size = abs(amount)
    if size >= Decimal(10) ** 9:
        number, unit = (size / Decimal(10) ** 9).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP), "bn"
    elif size >= Decimal(10) ** 6:
        number, unit = (size / Decimal(10) ** 6).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP), "m"
    else:
        number, unit = size, ""
    text = f"{number:,f}"
    if unit and text.endswith(".0"):
        text = text[:-2]
    return f"{'-' if negative else ''}{currency}{text}{unit}"


_PAGE_SCALE = re.compile(r"\bin\s+(?:(?:usd|eur|u\.s\.|us)\s+)?(?:dollars?\s+)?(?:[$€]\s*)?(thousands|millions|billions)\b",
                         re.IGNORECASE)


def scale_of_page(text: str) -> int | None:
    """The power of ten a page's tables are stated in, or None if it does not say or says two."""
    # "In millions, except share amounts which are in thousands" states one scale for money.
    stated = re.sub(r"\bexcept\b[^)]*\)", ")", text, flags=re.IGNORECASE)
    powers = {SCALES[m.group(1).lower()] for m in _PAGE_SCALE.finditer(stated)}
    return powers.pop() if len(powers) == 1 else None


# A year or day of a date is not a figure anyone needs to check.
_DATE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    re.IGNORECASE,
)
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_MEMO_NUMBER = re.compile(
    r"(?<![\w.,-])[-−–(]?\s*[$€£]?\s*\d[\d,]*(?:\.\d+)?(?!\d|-\w)"
    r"(?:\s*(?:trillion|billion|million|thousand|bn|mn|tn|[kmbt])(?![a-z]))?\s*%?\)?",
    re.IGNORECASE,
)


def numbers_in_memo(memo: str) -> list[tuple[int, int, str]]:
    """Every number a reader would take as a figure, with its place, so none can be skipped."""
    blanked = _DATE.sub(lambda m: " " * len(m.group(0)), memo)
    out = []
    for m in _MEMO_NUMBER.finditer(blanked):
        text = m.group(0).strip().rstrip(",.")
        if _YEAR.fullmatch(text):
            continue
        out.append((m.start(), m.end(), text))
    return out
