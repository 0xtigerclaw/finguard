"""Tests run on real filings with no API. `Scripted` stands in for Muse: it is not Muse."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from finguard.errors import MuseError
from finguard.muse import Muse
from finguard.page import Filing
from finguard.record import Run

DATA = Path(__file__).parent.parent / "data"


class Scripted:
    """Answers each call with the next prepared reply, and keeps what it was sent."""

    def __init__(self, *replies) -> None:
        self.replies, self.sent = list(replies), []

    def send(self, body: bytes) -> bytes:
        self.sent.append(body)
        reply = self.replies.pop(0)
        if isinstance(reply, MuseError):
            raise reply
        if isinstance(reply, bytes):
            return reply
        return json.dumps({"choices": [{"message": {"content": json.dumps(reply)}}]}).encode()


@pytest.fixture
def s1() -> Filing:
    return Filing(DATA / "ANTH-S1-2026.pdf")


@pytest.fixture
def nebius() -> Filing:
    return Filing(DATA / "NBIS-Q2-2026.pdf")


@pytest.fixture
def asml() -> Filing:
    return Filing(DATA / "ASML-Q3-2026.pdf")


@pytest.fixture
def run(tmp_path) -> Run:
    return Run(tmp_path / "run")


@pytest.fixture
def muse_with(run):
    def build(*replies):
        transport = Scripted(*replies)
        return Muse(transport, run), transport
    return build


def box_for(filing: Filing, page: int, text: str, nth: int = 0) -> list[int]:
    """A tight box around the nth place `text` is printed, as Muse would report it."""
    hits = filing.doc[page].search_for(text)
    r = hits[nth]
    return filing.points_to_grid(page, (r.x0, r.y0, r.x1, r.y1))
