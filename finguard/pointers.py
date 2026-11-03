"""A memo that differs from a saved example only in its numbers can use that example's saved pointers.

Muse is never told a memo's number while it points, so a saved pointing reply still answers for a
new number. Muse did not read the new memo, so its claims come from the saved listing reply with the
new numbers put in, and the run's record says so. The rest is the normal check: code reads the PDF
and gives every verdict.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .check import EXTRACT_SYSTEM, _value_spots, list_claims
from .errors import MuseError
from .figures import numbers_in_memo
from .muse import Muse, ReplayTransport
from .record import sha256

_Mark = tuple[int, int]


@dataclass
class SavedPointers:
    """Which example a memo was built from, and the claims for the memo."""

    base_memo: str  # the example's name, such as anthropic_memo_corrected
    base_sha256: str
    replies: Path
    claims: list[dict]
    changes: list[dict]  # each number that differs, as {"from", "to"}
    listing_request_sha256: str
    listing_reply_sha256: str


class _Listening:
    """Stands in for a run's record while the saved listing reply is read. Muse is not called."""

    body: bytes = b""
    reply: bytes = b""

    def write_call(self, body: bytes, reply: bytes | None, error: str | None) -> None:
        self.body, self.reply = body, reply or b""


def _marks(memo: str) -> list[_Mark] | None:
    """Where each number sits, without the spaces around it. None if two overlap or one cannot be placed."""
    found: list[_Mark] = []
    last = 0
    for start, end, text in numbers_in_memo(memo):
        at = memo.find(text, start, end)
        if at < last:
            return None
        last = at + len(text)
        found.append((at, last))
    return found


def _between(memo: str, marks: list[_Mark]) -> list[str]:
    """The memo's text around its numbers."""
    edges = [0, *(edge for mark in marks for edge in mark), len(memo)]
    return [memo[edges[i]:edges[i + 1]] for i in range(0, len(edges), 2)]


def _carry(base: str, start: int, end: int, base_marks: list[_Mark], memo: str, marks: list[_Mark]) -> tuple[str, int] | None:
    """This piece of the example with the memo's numbers in it, and how many it holds. None if it cuts a number."""
    pieces, at, held = [], start, 0
    for (a, b), (x, y) in zip(base_marks, marks):
        if b <= start or a >= end:
            continue
        if a < start or b > end:
            return None
        pieces += [base[at:a], memo[x:y]]
        at, held = b, held + 1
    return "".join(pieces) + base[at:end], held


def _claims_for(memo: str, marks: list[_Mark], base: str, base_marks: list[_Mark], claims) -> list[dict]:
    """The example's claims in the memo's numbers. A claim that cannot be carried over is left out, so its number
    comes back unchecked."""
    out = []
    used: dict[tuple[str, str], int] = {}
    for claim in claims:
        spots = _value_spots(claim, base)
        turn = used.get((claim.quote, claim.value_text), 0)
        used[(claim.quote, claim.value_text)] = turn + 1
        if turn >= len(spots):
            continue
        spot = spots[turn]
        at = base.find(claim.quote, max(0, spot + len(claim.value_text) - len(claim.quote)))  # the copy that holds this spot
        value = _carry(base, spot, spot + len(claim.value_text), base_marks, memo, marks)
        quote = _carry(base, at, at + len(claim.quote), base_marks, memo, marks)
        if value is None or quote is None or value[1] != 1:
            continue
        out.append({**asdict(claim), "quote": quote[0], "value_text": value[0]})
    return out


def find_saved_pointers(memo: str, examples: list[tuple[str, str, Path]]) -> SavedPointers | None:
    """The saved pointers for a memo, if an example has the same text and the same count of numbers in the same order."""
    marks = _marks(memo)
    if marks is None:
        return None
    for name, base, replies in examples:
        base_marks = _marks(base)
        if base_marks is None or len(base_marks) != len(marks) or _between(base, base_marks) != _between(memo, marks):
            continue
        heard = _Listening()
        try:
            claims = list_claims(base, Muse(ReplayTransport(replies), heard))
        except MuseError:
            return None  # no saved listing reply, so this is a normal live check
        changes = [{"from": base[a:b], "to": memo[x:y]} for (a, b), (x, y) in zip(base_marks, marks)
                   if base[a:b] != memo[x:y]]
        return SavedPointers(name, sha256(base.encode("utf-8")), replies, _claims_for(memo, marks, base, base_marks, claims),
                             changes, sha256(heard.body), sha256(heard.reply))
    return None


class PointerMuse(Muse):
    """Listing is answered from the saved reply, with no call. Pointing goes to the transport as usual."""

    def __init__(self, transport, run, pointers: SavedPointers) -> None:
        super().__init__(transport, run)
        self.pointers = pointers

    def ask(self, system: str, content: list[dict], schema_name: str, schema: dict) -> dict:
        if system != EXTRACT_SYSTEM:
            return super().ask(system, content, schema_name, schema)
        found = self.pointers
        self.run.write("saved_pointers", base_memo=found.base_memo, base_memo_sha256=found.base_sha256, changes=found.changes,
                       listing_request_sha256=found.listing_request_sha256, listing_reply_sha256=found.listing_reply_sha256,
                       note=f"Saved pointers from {found.base_memo} were used. Muse did not read this memo. "
                            "Plain code gave every verdict.")
        return {"claims": found.claims}
