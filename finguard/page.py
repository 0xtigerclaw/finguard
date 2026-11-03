"""What a filing page actually says at a given spot, read from the PDF's text. No model here."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pymupdf

from .figures import read_figure, scale_of_page
from .record import sha256

GRID = 1000  # Muse reports boxes as [x_min, y_min, x_max, y_max] on a 0-1000 grid
PADDING = 8  # forgive a box this many grid units too tight
QUALIFIERS = {"adjusted", "pro", "forma", "non", "gaap", "core", "underlying", "normalized", "normalised"}
ROW_REACH = 32  # Muse's boxes land up to about a row and a half off, measured on live replies
DPI = 150
_NUMERIC = re.compile(r"^\(?-?\$?€?\d[\d,]*(?:\.\d+)?\)?%?$")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_HEADING = re.compile(r"months|year|quarter|ended|as of|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)


# PyMuPDF is not safe across threads, and live calls overlap, so every read of a PDF takes this.
PDF_LOCK = threading.RLock()


@dataclass(frozen=True)
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass(frozen=True)
class Cell:
    """A number found on the page, with the row and column it sits in."""

    text: str
    amount: Decimal  # as printed, before the page's scale
    negative: bool
    percent: bool
    rect: tuple[float, float, float, float]
    row: str
    column: str


class Filing:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        data = self.path.read_bytes()
        self.sha256 = sha256(data)
        self.doc = pymupdf.open(stream=data, filetype="pdf")
        self.images: dict[int, bytes] = {}
        self.texts: dict[int, str] = {}
        self.page_words: dict[int, list[Word]] = {}

    @property
    def pages(self) -> int:
        return self.doc.page_count

    def text(self, page: int) -> str:
        if page not in self.texts:
            with PDF_LOCK:
                self.texts[page] = self.doc[page].get_text()
        return self.texts[page]

    def image(self, page: int) -> bytes:
        if page not in self.images:
            with PDF_LOCK:
                self.images[page] = self.doc[page].get_pixmap(dpi=DPI).tobytes("png")
        return self.images[page]

    def words(self, page: int) -> list[Word]:
        if page not in self.page_words:
            with PDF_LOCK:
                self.page_words[page] = [Word(*w[:5]) for w in self.doc[page].get_text("words")]
        return self.page_words[page]

    def size(self, page: int) -> tuple[float, float]:
        with PDF_LOCK:
            rect = self.doc[page].rect
            return rect.width, rect.height

    def grid_to_points(self, page: int, box: list[float]) -> tuple[float, float, float, float]:
        width, height = self.size(page)
        x0, y0, x1, y1 = box
        return (max(x0 - PADDING, 0) * width / GRID, max(y0 - PADDING, 0) * height / GRID,
                min(x1 + PADDING, GRID) * width / GRID, min(y1 + PADDING, GRID) * height / GRID)

    def points_to_grid(self, page: int, r: tuple[float, float, float, float]) -> list[int]:
        width, height = self.size(page)
        return [round(r[0] * GRID / width), round(r[1] * GRID / height),
                round(r[2] * GRID / width), round(r[3] * GRID / height)]

    def scale(self, page: int) -> int | None:
        return scale_of_page(self.text(page))

    def numbers_near(self, page: int, box: list[float], row_label: str) -> list[Word]:
        """The numbers in the box's column, on a row printed with this label, near the box.

        Muse's box is a pointer. The label on the page fixes the row, so a box a row out
        cannot pick up the number from the row next door.
        """
        x0, _, x1, _ = self.grid_to_points(page, box)
        height = self.size(page)[1]
        middle = (box[1] + box[3]) / 2 * height / GRID
        reach = ROW_REACH * height / GRID
        words = self.words(page)
        return [w for w in words
                if _NUMERIC.match(w.text) and x0 <= w.cx <= x1 and abs(w.cy - middle) <= reach
                and row_names(_row_of(w, words), row_label)]

    def cell(self, page: int, word: Word) -> Cell | None:
        words = self.words(page)
        figure = read_figure(_with_neighbours(word, words))
        if figure is None or figure.power:
            return None
        return Cell(word.text, figure.amount, figure.negative,
                    figure.percent or "%" in word.text or _percent_follows(word, words),
                    (word.x0, word.y0, word.x1, word.y1), _row_of(word, words), _column_of(word, words))


def squeeze(text: str) -> str:
    """Text reduced to lowercase words, so spacing and punctuation cannot fail a comparison."""
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def row_names(printed: str, said: str) -> bool:
    """True when `said` is the row's name. Headings above may lead it, a qualifier may not.

    "Cash" names "Assets Cash". "Operating margin" does not name "Adjusted Operating Margin".
    """
    row, name = _unfooted(printed), _unfooted(said)
    if not name or not row.endswith(name) or (len(row) > len(name) and row[-len(name) - 1] != " "):
        return False
    return not QUALIFIERS & set(row[:-len(name)].split())


def _unfooted(text: str) -> str:
    """Words of a label, without a footnote mark such as the (1) in "Net loss(1)"."""
    return squeeze(re.sub(r"\s*\(\d+\)|\s*[¹²³]", "", text))


def _same_line(a: Word, b: Word) -> bool:
    return abs(a.cy - b.cy) <= max(a.y1 - a.y0, b.y1 - b.y0) * 0.6


def _with_neighbours(word: Word, words: list[Word]) -> str:
    """A cell's text, joining a split "(" or ")" that the PDF stored as its own word."""
    text = word.text
    line = [w for w in words if _same_line(w, word)]
    if text.count("(") > text.count(")") and any(w.text.startswith(")") and 0 <= w.x0 - word.x1 < 6 for w in line):
        text += ")"
    if text.count(")") > text.count("(") and any(w.text == "(" and 0 <= word.x0 - w.x1 < 6 for w in line):
        text = "(" + text
    return text


def _percent_follows(word: Word, words: list[Word]) -> bool:
    return any(w.text.startswith(("%", ")%")) and 0 <= w.x0 - word.x1 < 12 for w in words if _same_line(w, word))


def _is_label(w: Word) -> bool:
    return not _NUMERIC.match(w.text) and w.text not in ("$", "€", "£", "—", "–", "-", ")", "(", ")%", "%")


def _row_of(word: Word, words: list[Word]) -> str:
    """The row's label: the words left of the first number on this line, and any line it wrapped from."""
    line = sorted((w for w in words if _same_line(w, word) and w.x1 <= word.x0 + 1), key=lambda w: w.x0, reverse=True)
    label: list[Word] = []
    for w in line:  # walk left from the number, past this row's other numbers, to the nearest label
        if _is_label(w):
            label.insert(0, w)
        elif label:
            break
    height = word.y1 - word.y0
    if not label:
        # The label sits only on the line above, ending at this row.
        above = [w for w in words if 0 < word.cy - w.cy < height * 2.2 and w.x1 < word.x0 - 40 and _is_label(w)]
        return " ".join(w.text for w in sorted(above, key=lambda w: w.x0))
    # Walk up through any lines this label wrapped from: same left edge, no numbers, not a heading.
    first = label[0]
    while True:
        wrapped = sorted((w for w in words if 0 < first.cy - w.cy < height * 1.6), key=lambda w: w.x0)
        text = " ".join(w.text for w in wrapped)
        if not (wrapped and abs(wrapped[0].x0 - first.x0) < 16 and all(_is_label(w) for w in wrapped)) \
                or text.endswith(":") or "millions" in text.lower() or "thousands" in text.lower():
            break
        label = wrapped + label
        first = wrapped[0]
    return " ".join(w.text for w in label)


def _column_of(word: Word, words: list[Word]) -> str:
    """The column's heading: the nearest year above this number, plus the heading over that year."""
    x0, x1 = word.x0 - 10, word.x1 + 10
    above = [w for w in words if w.cy < word.y0 and w.x1 >= x0 and w.x0 <= x1 and _YEAR.search(w.text)]
    if not above:
        return ""
    year = max(above, key=lambda w: w.cy)
    own = min(_groups([w for w in words if _same_line(w, year)]), key=lambda g: abs(_centre(g) - word.cx))
    parts = [" ".join(w.text for w in own)]
    # Spanning headings sit a line or two above. A units caption is skipped, and prose ends the search.
    tops = sorted({round(w.cy) for w in words if 0 < year.cy - w.cy < 32 and not _NUMERIC.match(w.text)}, reverse=True)
    for y in tops:
        groups = _groups([w for w in words if round(w.cy) == y])
        best = min(groups, key=lambda g: abs(_centre(g) - word.cx))
        text = " ".join(w.text for w in best)
        if "millions" in text.lower() or "thousands" in text.lower():
            continue
        if len(best) > 6:
            break
        if _HEADING.search(text):
            parts.insert(0, text)
    return " ".join(parts)


def _groups(line: list[Word], gap: float = 14.0) -> list[list[Word]]:
    """Split a line into runs of words, at a wide gap or where a new "As of" begins."""
    out: list[list[Word]] = []
    ordered = sorted(line, key=lambda w: w.x0)
    for i, w in enumerate(ordered):
        new_as_of = w.text.lower() == "as" and i + 1 < len(ordered) and ordered[i + 1].text.lower() == "of"
        if out and w.x0 - out[-1][-1].x1 <= gap and not new_as_of:
            out[-1].append(w)
        else:
            out.append([w])
    return out


def _centre(group: list[Word]) -> float:
    return (group[0].x0 + group[-1].x1) / 2
