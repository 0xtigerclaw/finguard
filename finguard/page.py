"""What a filing page actually says at a given spot, read from the PDF's text. No model here."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pymupdf

from .figures import read_figure, scale_of_page
from .record import sha256

GRID = 1000  # Muse reports boxes as [x_min, y_min, x_max, y_max] on a 0-1000 grid
PADDING = 8  # forgive a box this many grid units too tight
DPI = 150
_NUMERIC = re.compile(r"^\(?-?\$?€?\d[\d,]*(?:\.\d+)?\)?%?$")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")


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

    @property
    def pages(self) -> int:
        return self.doc.page_count

    def text(self, page: int) -> str:
        return self.doc[page].get_text()

    def image(self, page: int) -> bytes:
        return self.doc[page].get_pixmap(dpi=DPI).tobytes("png")

    def words(self, page: int) -> list[Word]:
        return [Word(*w[:5]) for w in self.doc[page].get_text("words")]

    def grid_to_points(self, page: int, box: list[float]) -> tuple[float, float, float, float]:
        rect = self.doc[page].rect
        x0, y0, x1, y1 = box
        return (max(x0 - PADDING, 0) * rect.width / GRID, max(y0 - PADDING, 0) * rect.height / GRID,
                min(x1 + PADDING, GRID) * rect.width / GRID, min(y1 + PADDING, GRID) * rect.height / GRID)

    def points_to_grid(self, page: int, r: tuple[float, float, float, float]) -> list[int]:
        rect = self.doc[page].rect
        return [round(r[0] * GRID / rect.width), round(r[1] * GRID / rect.height),
                round(r[2] * GRID / rect.width), round(r[3] * GRID / rect.height)]

    def scale(self, page: int) -> int | None:
        return scale_of_page(self.text(page))

    def numbers_in_box(self, page: int, box: list[float]) -> list[Word]:
        x0, y0, x1, y1 = self.grid_to_points(page, box)
        return [w for w in self.words(page)
                if _NUMERIC.match(w.text) and x0 <= w.cx <= x1 and y0 <= w.cy <= y1]

    def cell(self, page: int, word: Word) -> Cell | None:
        words = self.words(page)
        figure = read_figure(_with_neighbours(word, words))
        if figure is None or figure.power:
            return None
        return Cell(word.text, figure.amount, figure.negative,
                    figure.percent or _percent_follows(word, words),
                    (word.x0, word.y0, word.x1, word.y1), _row_of(word, words), _column_of(word, words))


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


def _row_of(word: Word, words: list[Word]) -> str:
    """The row's label: the words to the left of the first number on this line."""
    line = sorted((w for w in words if _same_line(w, word) and w.x1 <= word.x0 + 1), key=lambda w: w.x0)
    label = []
    for w in line:
        if _NUMERIC.match(w.text) or w.text in ("$", "€", "£", "—", "–", "-", ")", "(", ")%", "%"):
            if label:
                break
            continue
        label.append(w.text)
    if label:
        return " ".join(label)
    # A label that wrapped sits on the line above, ending at this row.
    above = [w for w in words if 0 < word.cy - w.cy < (word.y1 - word.y0) * 2.2 and w.x1 < word.x0 - 40
             and not _NUMERIC.match(w.text)]
    return " ".join(w.text for w in sorted(above, key=lambda w: w.x0))


def _column_of(word: Word, words: list[Word]) -> str:
    """The column's heading: the nearest year above this number, plus the heading over that year."""
    x0, x1 = word.x0 - 10, word.x1 + 10
    above = [w for w in words if w.cy < word.y0 and w.x1 >= x0 and w.x0 <= x1 and _YEAR.search(w.text)]
    if not above:
        return ""
    year = max(above, key=lambda w: w.cy)
    line = [w for w in words if _same_line(w, year)]
    groups = _groups(line)
    own = min(groups, key=lambda g: abs(_centre(g) - word.cx))
    parts = [" ".join(w.text for w in own)]
    # Spanning headings sit a line or two above. Prose or a units caption ends the heading.
    tops = sorted({round(w.cy) for w in words if 0 < year.cy - w.cy < 32 and not _NUMERIC.match(w.text)}, reverse=True)
    for y in tops:
        groups = _groups([w for w in words if round(w.cy) == y])
        best = min(groups, key=lambda g: abs(_centre(g) - word.cx))
        text = " ".join(w.text for w in best)
        if len(best) > 6 or "millions" in text.lower() or "thousands" in text.lower():
            break
        parts.insert(0, text)
    return " ".join(parts)


def _groups(line: list[Word], gap: float = 14.0) -> list[list[Word]]:
    """Split a line into runs of words, wherever a wide gap separates them."""
    out: list[list[Word]] = []
    for w in sorted(line, key=lambda w: w.x0):
        if out and w.x0 - out[-1][-1].x1 <= gap:
            out[-1].append(w)
        else:
            out.append([w])
    return out


def _centre(group: list[Word]) -> float:
    return (group[0].x0 + group[-1].x1) / 2
