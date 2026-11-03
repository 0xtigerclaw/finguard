"""The verdicts, on real filings. Muse's replies are scripted: right, wrong and hostile ones."""
import json

import pytest

from finguard.check import MATCH, REFUSED, UNCHECKED, WRONG, Claim, candidate_pages, check_memo, decide, list_claims, skipped_numbers
from finguard.errors import MuseError
from finguard.record import read_lines, verify
from tests.conftest import box_for

BRIDGE, PNL = 166, 352  # zero-based PDF pages of the S-1


def claim(value="$1.6bn", metric="Adjusted Operating Income", kind="six_months", year=2026, direction="gain"):
    return Claim(f"It reported {value}.", value, metric, kind, year, direction)


def located(filing, page, text, row, nth=0):
    return {"found": True, "page": 0, "box": box_for(filing, page, text, nth), "row_label": row}


def test_a_right_number_passes_with_its_receipt(s1):
    r = decide(claim(), s1, located(s1, BRIDGE, "1,618", "Adjusted Operating Income (Loss)"), [BRIDGE])
    assert r.verdict == MATCH
    assert r.source_text == "1,618" and "Six Months Ended June 30, 2026" in r.column and r.box and r.page == BRIDGE


def test_a_wrong_number_is_caught(s1):
    r = decide(claim("$1.9bn"), s1, located(s1, BRIDGE, "1,618", "Adjusted Operating Income (Loss)"), [BRIDGE])
    assert r.verdict == WRONG and "1,618" in r.why


def test_a_loss_called_a_profit_is_caught(s1):
    c = claim("$693m", "Operating loss", direction="gain")
    r = decide(c, s1, located(s1, BRIDGE, "(693)", "Operating loss"), [BRIDGE])
    assert r.verdict == WRONG and "loss" in r.why


def test_a_loss_called_a_loss_passes(s1):
    c = claim("$80.7bn", "Net loss", direction="loss")
    assert decide(c, s1, located(s1, BRIDGE, "(80,748)", "Net loss"), [BRIDGE]).verdict == MATCH


def test_receipts_write_money_plainly_and_keep_the_printed_cell(s1):
    c = claim("$80.7bn", "Net loss", direction="loss")
    r = decide(c, s1, located(s1, BRIDGE, "(80,748)", "Net loss"), [BRIDGE])
    assert r.source_money == "-$80.7bn" and r.source_text == "(80,748)"
    assert "-$80.7bn (printed as (80,748))" in r.why


def test_tidying_the_display_never_loosens_the_check(s1):
    c = claim("$80.8bn", "Net loss", direction="loss")
    assert decide(c, s1, located(s1, BRIDGE, "(80,748)", "Net loss"), [BRIDGE]).verdict == WRONG


def test_the_wrong_columns_year_is_refused(s1):
    c = claim("$1.6bn", year=2025)
    r = decide(c, s1, located(s1, BRIDGE, "1,618", "Adjusted Operating Income (Loss)"), [BRIDGE])
    assert r.verdict == REFUSED and "column" in r.why


def test_a_full_year_claim_cannot_pass_on_a_half_year_cell(s1):
    c = claim("$1.6bn", kind="full_year")
    r = decide(c, s1, located(s1, BRIDGE, "1,618", "Adjusted Operating Income (Loss)"), [BRIDGE])
    assert r.verdict == REFUSED


def test_muse_lying_about_the_row_is_refused(s1):
    r = decide(claim("$2.2bn", "Adjusted Operating Income"), s1,
               located(s1, BRIDGE, "2,185", "Adjusted Operating Income (Loss)"), [BRIDGE])
    assert r.verdict == REFUSED and "0 numbers" in r.why


def test_a_row_that_does_not_name_the_metric_is_refused(s1):
    r = decide(claim("$2.2bn", "Free cash flow"), s1,
               located(s1, BRIDGE, "2,185", "Stock-based compensation expense"), [BRIDGE])
    assert r.verdict == REFUSED


def test_a_box_around_a_whole_row_is_refused(s1):
    x0, y0, x1, y1 = box_for(s1, BRIDGE, "1,618")
    r = decide(claim(), s1, {"found": True, "page": 0, "box": [100, y0, x1, y1],
                             "row_label": "Adjusted Operating Income (Loss)"}, [BRIDGE])
    assert r.verdict == REFUSED and "numbers" in r.why


def test_a_box_a_row_too_low_still_finds_the_named_row(s1):
    x0, y0, x1, y1 = box_for(s1, BRIDGE, "(693)")
    loc = {"found": True, "page": 0, "box": [x0, y0 + 20, x1, y1 + 20], "row_label": "Operating loss"}
    r = decide(claim("$693m", "Operating loss", direction="loss"), s1, loc, [BRIDGE])
    assert r.verdict == MATCH and r.source_text == "(693)" and r.muse_box == loc["box"]


def test_a_box_far_from_the_named_row_is_refused(s1):
    x0, y0, x1, y1 = box_for(s1, BRIDGE, "(693)")
    loc = {"found": True, "page": 0, "box": [x0, y0 + 200, x1, y1 + 200], "row_label": "Operating loss"}
    assert decide(claim("$693m", "Operating loss", direction="loss"), s1, loc, [BRIDGE]).verdict == REFUSED


def test_an_empty_box_is_refused(s1):
    r = decide(claim(), s1, {"found": True, "page": 0, "box": [1, 1, 5, 5], "row_label": "x"}, [BRIDGE])
    assert r.verdict == REFUSED


@pytest.mark.parametrize("loc", [
    {"found": False, "page": 0, "box": [], "row_label": ""},
    {"found": True, "page": 9, "box": [1, 1, 2, 2], "row_label": "x"},
    {"found": True, "page": -1, "box": [1, 1, 2, 2], "row_label": "x"},
    {"found": True, "page": 0, "box": [5, 5, 1, 1], "row_label": "x"},
    {"found": True, "page": 0, "box": [1, 2, 3], "row_label": "x"},
])
def test_unusable_replies_are_refused(s1, loc):
    assert decide(claim(), s1, loc, [BRIDGE]).verdict == REFUSED


def test_dollars_against_a_euro_filing_are_caught(asml):
    c = Claim("Sales were $11,662.0m.", "$11,662.0m", "Total net sales", "quarter", 2026, "neutral")
    r = decide(c, asml, located(asml, 2, "11,662.0", "Total net sales"), [2])
    assert r.verdict == WRONG and "currency" in r.why


def test_euros_on_asml_pass(asml):
    c = Claim("Sales were €11.7bn.", "€11.7bn", "Total net sales", "quarter", 2026, "neutral")
    r = decide(c, asml, located(asml, 2, "11,662.0", "Total net sales"), [2])
    assert r.verdict == MATCH and "Three months" in r.column


def test_nebius_quarter_and_half_year_are_not_confused(nebius):
    half = Claim("Revenue was $981.3m.", "$981.3m", "Revenues", "six_months", 2026, "neutral")
    assert decide(half, nebius, located(nebius, 6, "981.3", "Revenues"), [6]).verdict == MATCH
    quarter = Claim("Revenue was $981.3m.", "$981.3m", "Revenues", "quarter", 2026, "neutral")
    assert decide(quarter, nebius, located(nebius, 6, "981.3", "Revenues"), [6]).verdict == REFUSED


def test_us_dollars_in_words_count_as_dollars(nebius):
    half = Claim("Revenue was $981.3m.", "$981.3m", "Revenues", "six_months", 2026, "neutral")
    assert "currency" not in decide(half, nebius, located(nebius, 6, "981.3", "Revenues"), [6]).why


def test_candidate_pages_include_the_statements(s1):
    pages = candidate_pages(s1, claim("$693m", "Operating loss", direction="loss"))
    assert BRIDGE in pages and len(pages) <= 6


MEMO = "Anthropic posted an adjusted operating income of $1.6bn in the six months to June 30, 2026."
EXTRACTED = {"quote": MEMO, "value_text": "$1.6bn", "metric": "Adjusted Operating Income",
             "period_kind": "six_months", "period_year": 2026, "direction": "gain"}


def test_a_claim_not_in_the_memo_is_dropped(muse_with):
    invented = dict(EXTRACTED, quote="Anthropic made $9bn.", value_text="$9bn")
    muse, _ = muse_with({"claims": [EXTRACTED, invented]})
    assert [c.value_text for c in list_claims(MEMO, muse)] == ["$1.6bn"]


def test_a_number_muse_skipped_is_flagged():
    memo = MEMO + " Margin was 10%."
    c = Claim(MEMO, "$1.6bn", "x", "six_months", 2026, "gain")
    assert skipped_numbers(memo, [c]) == ["10%"]


def _page_position(s1, c):
    return candidate_pages(s1, c).index(BRIDGE)


def test_muse_is_never_told_the_memos_number_when_pointing(s1, run, muse_with):
    c = Claim(**EXTRACTED)
    muse, transport = muse_with({"claims": [EXTRACTED]}, {"found": True, "page": _page_position(s1, c),
                                                          "box": box_for(s1, BRIDGE, "1,618"),
                                                          "row_label": "Adjusted Operating Income (Loss)"})
    receipts = check_memo(MEMO, s1, muse, run)
    assert [r.verdict for r in receipts] == [MATCH]
    pointing = json.loads(transport.sent[1])
    words = json.dumps([p for p in pointing["messages"][1]["content"] if p["type"] == "text"])
    assert "1.6" not in words and "Anthropic posted" not in words


def test_a_whole_run_is_on_the_record_and_holds(s1, run, muse_with):
    c = Claim(**EXTRACTED)
    muse, _ = muse_with({"claims": [EXTRACTED]}, {"found": True, "page": _page_position(s1, c),
                                                  "box": box_for(s1, BRIDGE, "1,618"),
                                                  "row_label": "Adjusted Operating Income (Loss)"})
    check_memo(MEMO, s1, muse, run)
    kinds = [e["kind"] for e in read_lines(run.path)]
    assert kinds == ["start", "muse_call", "muse_call", "receipt", "end"]
    assert verify(run.folder) == []


def test_a_failed_call_refuses_and_assumes_nothing(s1, run, muse_with):
    muse, _ = muse_with({"claims": [EXTRACTED]}, MuseError("timeout", "slow"))
    r = check_memo(MEMO, s1, muse, run)[0]
    assert r.verdict == REFUSED and "timeout" in r.why


def test_a_skipped_number_shows_in_the_run(s1, run, muse_with):
    memo = MEMO + " Margin was 10%."
    c = Claim(**EXTRACTED)
    muse, _ = muse_with({"claims": [EXTRACTED]}, {"found": False, "page": 0, "box": [], "row_label": ""})
    assert UNCHECKED in [r.verdict for r in check_memo(memo, s1, muse, run)] and c
