"""Read a figure from text and compare two figures. No model is involved here."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

ROUGH = Decimal("0.05")
SCALES = {
    "k": 3, "thousand": 3, "thousands": 3,
    "m": 6, "mn": 6, "million": 6, "millions": 6,
    "b": 9, "bn": 9, "billion": 9, "billions": 9,
    "t": 12, "tn": 12, "trillion": 12,
}
_FIGURE = re.compile(
    r"(?P<open>\()?\s*(?P<minus>[-−–])?\s*(?P<cur>[$€£¥₹₩])?\s*(?P<minus2>[-−–])?\s*"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<scale>trillion|billions?|millions?|thousands?|bn|mn|tn|[kmbt])?(?![a-z])\s*"
    r"(?P<pct>%)?\s*(?P<close>\))?",
    re.IGNORECASE,
)


# Longest first, so C$ is never read as plain dollars. A figure with none of these has no stated currency.
CURRENCIES = (
    ("C$", r"\b(?:ca?)\$|\bcad(?![a-z])|\bcanadian dollars?\b"),
    ("A$", r"\b(?:au?)\$|\baud(?![a-z])|\baustralian dollars?\b"),
    ("HK$", r"\bhk\$|\bhkd(?![a-z])|\bhong kong dollars?\b"),
    ("S$", r"\bs\$|\bsgd(?![a-z])|\bsingapore dollars?\b"),
    ("NZ$", r"\bnz\$|\bnzd(?![a-z])"),
    ("NT$", r"\bnt\$|\btwd(?![a-z])"),
    ("R$", r"\br\$|\bbrl(?![a-z])|\breais\b"),
    ("MX$", r"\bmx\$|\bmxn(?![a-z])"),
    ("£", r"£|\bgbp(?![a-z])|\bpounds?\b|\bsterling\b"),
    ("¥", r"¥|\bjpy(?![a-z])|\byen\b"),
    ("RMB", r"\brmb(?![a-z])|\bcny(?![a-z])|\byuan\b|\brenminbi\b"),
    ("₹", r"₹|\binr(?![a-z])|\brupees?\b"),
    ("₩", r"₩|\bkrw(?![a-z])"),
    ("CHF", r"\bchf(?![a-z])|\bswiss francs?\b"),
    ("kr", r"\bsek(?![a-z])|\bnok(?![a-z])|\bdkk(?![a-z])|\bkronor\b|\bkroner\b"),
    ("€", r"€|\beur(?![a-z])|\beuros?\b"),
    ("$", r"\$|\busd(?![a-z])|\bdollars?\b"),
)


def currency_in(text: str) -> str | None:
    """The currency a piece of text names, if it names one."""
    for name, pattern in CURRENCIES:
        if re.search(pattern, text, re.IGNORECASE):
            return name
    return None


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
                  currency_in(text), bool(m.group("pct")), m.group(0).strip())


def same_size(claim: Figure, source_amount: Decimal) -> bool:
    """True when the source rounds to the claim at the precision the claim was written in.

    "$1.6bn" agrees with 1,618 million. "$1.7bn" and "$1,600m" do not. Nothing that is not zero agrees with zero.
    """
    if claim.amount == 0 and source_amount != 0:
        return False
    unit = Decimal(1).scaleb(claim.power - claim.decimals)
    rounded = (abs(source_amount) / unit).quantize(Decimal(1), rounding=ROUND_HALF_UP) * unit
    return rounded == abs(claim.amount)


def too_rough(claim: Figure, source_amount: Decimal) -> bool:
    """True when money rounds to the claim but sits over 5% from it, such as "$1bn" for 1,412 million.

    Percentages are left out: whole-number percentages are how people write them.
    """
    return not claim.percent and abs(abs(source_amount) - claim.amount) > abs(source_amount) * ROUGH


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
    if text.endswith(".0") and (unit == "m" or not unit):
        text = text[:-2]
    return f"{'-' if negative else ''}{currency}{text}{unit}"


def money_in_millions(amount: Decimal, currency: str, negative: bool = False) -> str:
    """Money for a sum a person follows, where rounding would break it: $1,618m, -$693m, $46.7m."""
    millions = abs(amount) / Decimal(10) ** 6
    text = f"{millions.normalize():,f}" if millions != millions.to_integral_value() else f"{millions.to_integral_value():,f}"
    return f"{'-' if negative else ''}{currency}{text}m"


_PAGE_SCALE = re.compile(r"\bin\s+(?:(?:usd|eur|u\.s\.|us)\s+)?(?:dollars?\s+)?(?:[$€]\s*)?(thousands|millions|billions)\b",
                         re.IGNORECASE)


def scale_of_page(text: str) -> int | None:
    """The power of ten a page's tables are stated in, or None if it does not say or says two."""
    # "In millions, except share amounts which are in thousands" states one scale for money.
    stated = re.sub(r"\bexcept\b[^)]*\)", ")", text, flags=re.IGNORECASE)
    powers = {SCALES[m.group(1).lower()] for m in _PAGE_SCALE.finditer(stated)}
    return powers.pop() if len(powers) == 1 else None


_SCALE_WORD = r"(?:trillion|billion|million|thousand|bn|mn|tn)"
# A year or day of a date is not a figure anyone needs to check. "May 16.3bn" and "June 12 million" are figures.
_NOT_A_DAY = rf"(?!\s*(?:{_SCALE_WORD}|[kmbt])(?![a-z]))(?!\s*%)(?!\.\d)(?!,\d{{3}}(?!\d))"
_DATE = re.compile(
    rf"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{{1,2}}\b{_NOT_A_DAY}"
    rf"|\b\d{{1,2}}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    re.IGNORECASE,
)
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_CODES = r"usd|eur|gbp|jpy|cad|aud|rmb|cny|hkd|sgd|chf|c\.|x"
_NOT_A_COUNT = r"(?!-(?:k|q|f|month|months|year|years|week|weeks|day|days|quarter|quarters|hour|point|points|page|pages|for|fold|to)\b)"
_MEMO_NUMBER = re.compile(
    rf"(?:(?<![a-z])(?:{_CODES})\s*|(?<![\w.,-])|(?<=\d-)|(?<=%-)|(?<=[mnk]-))"  # after a currency code, or after "5-" in "5-6%"
    r"[-−–(]?\s*[$€£¥₹₩]?\s*(?:\d[\d,]*(?:\.\d+)?|\.\d+)(?!\d)"
    + _NOT_A_COUNT
    + rf"(?:[\s-]*{_SCALE_WORD}s?(?![a-z])|\s*[kmbt](?![a-z]))?\s*%?\)?",
    re.IGNORECASE,
)
_NUMBER_WORD = r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|half(?: a)?|a)"
_WORD_NUMBER = re.compile(rf"\b{_NUMBER_WORD}(?:[\s-]+{_NUMBER_WORD})*[\s-]+(?:trillion|billion|million)\b(?:\s+(?:dollars|euros|pounds))?", re.IGNORECASE)


_MONEY_WORD = re.compile(r"\s*(?:dollars|euros|pounds)\b", re.IGNORECASE)


def numbers_in_memo(memo: str) -> list[tuple[int, int, str]]:
    """Every number a reader would take as a figure, with its place, so none can be skipped."""
    blanked = _DATE.sub(lambda m: " " * len(m.group(0)), memo)
    out = []
    for m in _MEMO_NUMBER.finditer(blanked):
        text = m.group(0).strip().rstrip(",.")
        if _YEAR.fullmatch(text) and not _MONEY_WORD.match(blanked, m.end()):
            continue
        out.append((m.start(), m.end(), text))
    out += [(m.start(), m.end(), m.group(0)) for m in _WORD_NUMBER.finditer(memo)]
    return sorted(out)
