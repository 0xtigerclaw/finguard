"""What a filing page actually says at a given spot, read from the PDF's text. No model here."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pymupdf

from .figures import SCALES, read_figure, scale_of_page
from .record import sha256

GRID = 1000  # Muse reports boxes as [x_min, y_min, x_max, y_max] on a 0-1000 grid
PADDING = 8  # forgive a box this many grid units too tight
QUALIFIERS = {"adjusted", "pro", "forma", "non", "gaap", "core", "underlying", "normalized", "normalised",
              "continuing", "discontinued"}
COLUMN_REACH = 130  # how far left or right of a number a box may land, measured on live replies
CLEARLY_CLOSER = 0.8  # the closest number must be this much nearer than the next, or the column is unclear
ROW_REACH = 32  # Muse's boxes land up to about a row and a half off, measured on live replies
DPI = 150
_NUMERIC = re.compile(r"^\(?-?\$?€?\d[\d,]*(?:\.\d+)?\)?%?$")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_MONTH = re.compile(r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?", re.I)
_HEADING = re.compile(r"months|year|quarter|ended|as of|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)
_KIND_SAID = re.compile(r"months|year ended|quarter", re.I)
_BARE_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_BARE_ROW = {"basic", "diluted", "basic and diluted"}
PROSE_RUN = 9  # this many words set close together on one line are a sentence, not a table
PROSE_GAP = 8.0


_CAPTION = re.compile(r"\bin\s+(?:(?:usd|eur|u\.s\.|us)\s+)?(?:dollars?\s+)?(?:[$€]\s*)?(?:thousands|millions|billions)\b", re.I)


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
    section: str = ""  # the heading over a bare row such as "Basic"


class Filing:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        data = self.path.read_bytes()
        self.sha256 = sha256(data)
        self.doc = pymupdf.open(stream=data, filetype="pdf")
        self.images: dict[int, bytes] = {}
        self.texts: dict[int, str] = {}
        self.page_words: dict[int, list[Word]] = {}
        self.page_cells: dict[int, list[tuple[Word, Cell]]] = {}

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
            pinned = self.path.parents[0 if self.path.parent.name == "data" else 1] / "pinned_pages" / self.sha256[:16] / f"{page}.png"
            if pinned.exists():  # a page that renders differently across systems would not match its saved reply
                self.images[page] = pinned.read_bytes()
            else:
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

    def scale_at(self, page: int, word: Word) -> int | None:
        """The page's units, if they are stated above this number. A caption below it is about another table."""
        scale = self.scale(page)
        if scale is None:
            return None
        spots, joined = [], ""
        for w in sorted(self.words(page), key=lambda w: (round(w.cy), w.x0)):
            spots.append((len(joined), w))
            joined += w.text + " "
        for m in _CAPTION.finditer(joined):
            if next(w for at, w in reversed(spots) if at <= m.start()).cy <= word.cy:
                return scale
        return None

    def numbers_near(self, page: int, box: list[float], row_label: str) -> list[Word]:
        """The numbers on a row printed with this label, near Muse's box.

        Muse's box is a pointer, and live runs show it lands on the right row area but often
        left of the number. The label on the page fixes the row. A box that covers no number on
        that row picks the closest one, unless two are about as close, which stays unclear.
        Whichever it picks, its own column heading must still fit the claim's period.
        """
        x0, _, x1, _ = self.grid_to_points(page, box)
        width, height = self.size(page)
        middle = (box[1] + box[3]) / 2 * height / GRID
        reach = ROW_REACH * height / GRID
        words = self.words(page)
        on_row = [w for w in words if _NUMERIC.match(w.text) and abs(w.cy - middle) <= reach
                  and row_names(_row_of(w, words), row_label) and _names_whole_line(w, words, row_label)]
        inside = [w for w in on_row if x0 <= w.cx <= x1]
        if inside:
            return inside
        centre = (box[0] + box[2]) / 2 * width / GRID
        nearby = sorted((w for w in on_row if abs(w.cx - centre) <= COLUMN_REACH * width / GRID),
                        key=lambda w: abs(w.cx - centre))
        if len(nearby) > 1 and abs(nearby[0].cx - centre) > CLEARLY_CLOSER * abs(nearby[1].cx - centre):
            return nearby[:2]
        return nearby[:1]

    def cell(self, page: int, word: Word) -> Cell | None:
        words = self.words(page)
        figure = read_figure(_with_neighbours(word, words))
        if figure is None or figure.power or _BARE_YEAR.match(word.text) or _scale_follows(word, words):
            return None
        row = _row_of(word, words)
        return Cell(word.text, figure.amount, figure.negative,
                    figure.percent or "%" in word.text or _percent_follows(word, words) or _percent_table(word, words),
                    (word.x0, word.y0, word.x1, word.y1), row, _column_of(word, words),
                    _section_of(word, words) if _unfooted(row) in _BARE_ROW else "")

    def cells_on(self, page: int) -> list[Cell]:
        return [c for _, c in self._cells(page)]

    def _cells(self, page: int) -> list[tuple[Word, Cell]]:
        if page not in self.page_cells:
            found = [(w, self.cell(page, w)) for w in self.words(page) if _NUMERIC.match(w.text)]
            self.page_cells[page] = [(w, c) for w, c in found if c is not None]
        return self.page_cells[page]

    def same_named(self, page: int, word: Word, cell: Cell) -> list[str]:
        """Other numbers printed on this page under the same row name and column, in the same unit."""
        name, column = _unfooted(f"{cell.section} {cell.row}"), cell.column.lower()
        return [c.text for w, c in self._cells(page)
                if w is not word and c.column.lower() == column and c.percent == cell.percent
                and _unfooted(f"{c.section} {c.row}") == name and (c.amount, c.negative) != (cell.amount, cell.negative)
                and not _is_note_on(word, w, self.words(page))]


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


def _is_note_on(word: Word, other: Word, words: list[Word]) -> bool:
    """True when `other` sits in the footnote this row points to: "Cost of revenues (1)" and, lower down, a line opening "(1)".

    The note breaks the row down, so it does not compete with the row. The note's own cell gets no such pass.
    """
    marks = {m for w in words if _same_line(w, word) and w.x1 <= word.x0 for m in re.findall(r"\((\d+)\)$", w.text)}
    for w in words:
        if word.cy < w.cy < other.cy and any(w.text == f"({m})" for m in marks) \
                and not any(o.x1 <= w.x0 for o in words if _same_line(o, w)):
            return True
    return False


def _names_whole_line(word: Word, words: list[Word], said: str) -> bool:
    """A name must hold all the words printed beside the number. "Revenues" does not name "Cost of revenues"."""
    return _unfooted(said).endswith(_unfooted(" ".join(w.text for w in _line_label(word, words))))


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


def _scale_follows(word: Word, words: list[Word]) -> bool:
    """True when the next word is a scale, as in "$108.0 billion". The page's units then do not apply."""
    after = (w for w in words if _same_line(w, word) and 0 <= w.x0 - word.x1 < 12)
    return any(w.text.lower().rstrip(",.;:)") in SCALES and len(w.text) > 1 for w in after)


def _run_around(word: Word, words: list[Word]) -> int:
    """How many words on this line sit in one unbroken run with this one."""
    line = sorted((w for w in words if _same_line(w, word)), key=lambda w: w.x0)
    at = line.index(word)
    left = at
    while left > 0 and line[left].x0 - line[left - 1].x1 <= PROSE_GAP:
        left -= 1
    right = at
    while right + 1 < len(line) and line[right + 1].x0 - line[right].x1 <= PROSE_GAP:
        right += 1
    return right - left + 1


def _in_prose(word: Word, words: list[Word]) -> bool:
    return _run_around(word, words) >= PROSE_RUN


def _is_label(w: Word) -> bool:
    return not _NUMERIC.match(w.text) and w.text not in ("$", "€", "£", "—", "–", "-", ")", "(", ")%", "%")


def _line_label(word: Word, words: list[Word]) -> list[Word]:
    """The label words on the number's own line: walk left, past this row's other numbers, to the nearest label."""
    line = sorted((w for w in words if _same_line(w, word) and w.x1 <= word.x0 + 1), key=lambda w: w.x0, reverse=True)
    label: list[Word] = []
    for w in line:
        if _is_label(w):
            label.insert(0, w)
        elif label:
            break
    return label


def _section_of(word: Word, words: list[Word]) -> str:
    """The heading just above a bare row such as "Basic", which says what the row counts."""
    first = (_line_label(word, words) or [word])[0]
    reach = (word.y1 - word.y0) * 2.4
    lines: list[str] = []
    y = first.cy
    while len(lines) < 2:
        above = [w for w in words if 0 < y - w.cy < reach and w.x1 < word.x0 - 40]
        if not above:
            break
        y = max(w.cy for w in above)
        line = sorted((w for w in above if abs(w.cy - y) < 3), key=lambda w: w.x0)
        if any(_NUMERIC.match(w.text) for w in words if _same_line(w, line[0])):
            if not lines and _unfooted(" ".join(w.text for w in line)) in _BARE_ROW:
                continue  # "Basic" over "Diluted": keep climbing to their heading
            break
        lines.insert(0, " ".join(w.text for w in line))
    return " ".join(lines)


def _heading_year(word: Word, words: list[Word]) -> bool:
    """A year that heads a column. "October 1, 2026," in a sentence does not."""
    return bool(_YEAR.search(word.text)) and not word.text.endswith((",", ".", ";", ":")) and not _in_prose(word, words)


def _year_line_above(word: Word, words: list[Word]) -> list[Word]:
    years = [w for w in words if w.cy < word.y0 and _heading_year(w, words)]
    if not years:
        return []
    lowest = max(years, key=lambda w: w.cy)
    return sorted((w for w in years if _same_line(w, lowest)), key=lambda w: w.x0)


def _year_over(line: list[Word], word: Word) -> Word | None:
    """The year this number sits under: it touches that year, or is plainly nearest it. None if neither."""
    touching = [w for w in line if w.x1 >= word.x0 - 10 and w.x0 <= word.x1 + 10]
    if touching:
        return min(touching, key=lambda w: abs(w.cx - word.cx))
    if len(line) < 2:
        return None
    reach = min(b.cx - a.cx for a, b in zip(line, line[1:])) * 0.45
    nearest = min(line, key=lambda w: abs(w.cx - word.cx))
    return nearest if abs(nearest.cx - word.cx) <= reach else None


def _percent_table(word: Word, words: list[Word]) -> bool:
    """A table of percentages may print % on its first and last rows only. Those two decide the whole column."""
    line = _year_line_above(word, words)
    year = _year_over(line, word) if line else None
    if year is None:
        return False
    below = [w for w in words if w.cy > year.cy and (_NUMERIC.match(w.text) or w.text in ("—", "–")) and not _BARE_YEAR.match(w.text)
             and not _in_prose(w, words) and _year_over(line, w) is year]
    next_years = [w.cy for w in words if w.cy > year.cy + 2 and _heading_year(w, words)]
    if next_years:
        below = [w for w in below if w.cy < min(next_years)]
    below.sort(key=lambda w: w.cy)
    rows: list[Word] = []
    for w in below:
        if rows and w.cy - rows[-1].cy > 40:
            break
        rows.append(w)
    if len(rows) < 3 or word not in rows:
        return False
    return all("%" in w.text or _percent_follows(w, words) for w in (rows[0], rows[-1]))


def _row_of(word: Word, words: list[Word]) -> str:
    """The row's label: the words left of the first number on this line, and any line it wrapped from."""
    label = _line_label(word, words)
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
    if _in_prose(word, words):
        return ""
    line = _year_line_above(word, words)
    # Numbers sit right of a centred year, so the year is the anchor for every heading above it.
    year = _year_over(line, word) if line else None
    if year is None:  # stop here: a higher year belongs to another table or a sentence
        return ""
    own = next(g for g in _groups([w for w in words if _same_line(w, year)]) if year in g)
    parts = [" ".join(w.text for w in own)]
    # Spanning headings sit a line or two above. A units caption is skipped, and prose ends the search.
    tops = sorted({round(w.cy) for w in words if 0 < year.cy - w.cy < 32 and not _NUMERIC.match(w.text)}, reverse=True)
    for y in tops:
        groups = _groups([w for w in words if round(w.cy) == y])
        best = min(groups, key=lambda g: abs(_centre(g) - year.cx))
        text = " ".join(w.text for w in best)
        if "millions" in text.lower() or "thousands" in text.lower():
            continue
        if len(best) > 6 or _in_prose(best[0], words):
            break
        if _HEADING.search(text):
            parts.insert(0, text)
        if _KIND_SAID.search(text):  # the column's kind is known. A kind higher up belongs to the next column over
            break
    return " ".join(parts)


def _groups(line: list[Word], gap: float = 14.0) -> list[list[Word]]:
    """Split a line into runs of words: at a wide gap, after a year, or where a new date or "As of" begins."""
    out: list[list[Word]] = []
    ordered = sorted(line, key=lambda w: w.x0)
    for i, w in enumerate(ordered):
        new_as_of = w.text.lower() == "as" and i + 1 < len(ordered) and ordered[i + 1].text.lower() == "of"
        new_date = bool(out and _MONTH.fullmatch(w.text) and any(_MONTH.fullmatch(p.text) for p in out[-1]))
        after_year = bool(out and _YEAR.search(out[-1][-1].text))
        if out and w.x0 - out[-1][-1].x1 <= gap and not (new_as_of or new_date or after_year):
            out[-1].append(w)
        else:
            out.append([w])
    return out


def _centre(group: list[Word]) -> float:
    return (group[0].x0 + group[-1].x1) / 2
