"""Real Muse replies, saved from live calls and replayed with no API."""
from pathlib import Path

import pytest

from finguard.check import MATCH, WRONG, Claim, check_memo, decide, locate
from finguard.muse import Muse, ReplayTransport
from finguard.record import verify

SAVED = Path(__file__).parent / "recordings" / "first_page"
BRIDGE = 166
CASES = [("Adjusted Operating Income", "$1.6bn", "six_months", 2026, "gain", "1,618"),
         ("Operating loss", "$693m", "six_months", 2026, "loss", "(693)"),
         ("Net loss", "$80.7bn", "six_months", 2026, "loss", "(80,748)"),
         ("Revenue", "$16.3bn", "six_months", 2026, "neutral", "16,286"),
         ("Operating loss", "$8.1bn", "full_year", 2025, "loss", "(8,060)")]


def test_live_replies_lead_to_the_right_cells(s1, run):
    transport = ReplayTransport(SAVED)
    muse = Muse(transport, run)
    for metric, value, kind, year, direction, printed in CASES:
        c = Claim(f"x {value}", value, metric, kind, year, direction)
        r = decide(c, s1, locate(c, s1, [BRIDGE], muse), [BRIDGE])
        assert (r.verdict, r.source_text) == (MATCH, printed), r.why
    transport.assert_all_used()


def test_a_whole_memo_with_two_planted_errors(s1, run):
    """The memo is ours, with two errors planted: a loss called a profit, and $8.7bn for $80.7bn."""
    memo = (Path(__file__).parent.parent / "examples" / "anthropic_memo.txt").read_text().strip()
    transport = ReplayTransport(Path(__file__).parent / "recordings" / "anthropic_memo")
    receipts = check_memo(memo, s1, Muse(transport, run), run)
    transport.assert_all_used()
    got = {r.value_text: r.verdict for r in receipts}
    assert got == {"$16.3bn": MATCH, "$1.6bn": MATCH, "$693m": WRONG, "$8.7bn": WRONG, "$2.2bn": MATCH, "10%": MATCH}
    assert verify(run.folder) == []
