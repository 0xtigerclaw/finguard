"""The verdicts, on real filings. Muse's replies are scripted: right, wrong and hostile ones."""
import json
from pathlib import Path

import pytest

from finguard.check import period_stated, MATCH, REFUSED, UNCHECKED, WRONG, Claim, _crop_bounds, _found_count, candidate_pages, check_memo, decide, list_claims, skipped_numbers
from finguard.errors import MuseError
from finguard.page import row_names
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


@pytest.mark.parametrize("printed,year,verdict", [("27,961.5", 2026, MATCH), ("27,961.5", 2025, REFUSED),
                                                  ("12,430.6", 2025, MATCH), ("12,430.6", 2026, REFUSED)])
def test_balance_sheet_columns_keep_their_own_year(nebius, printed, year, verdict):
    c = Claim("x", f"${printed}m", "Total assets", "as_of", year, "neutral")
    assert decide(c, nebius, located(nebius, 5, printed, "Total assets"), [5]).verdict == verdict


def test_a_side_by_side_table_reads_its_own_row(asml):
    c = Claim("x", "€36.9bn", "Total liabilities", "as_of", 2026, "neutral")
    r = decide(c, asml, located(asml, 4, "36,917.3", "Total liabilities"), [4])
    assert r.verdict == MATCH and r.row == "Total liabilities"


def test_an_adjusted_figure_cannot_pass_as_the_plain_one(s1):
    """The filing's adjusted margin is 10%. Its operating margin is not."""
    c = claim("10%", "Operating margin", direction="neutral")
    r = decide(c, s1, located(s1, BRIDGE, "10", "Operating margin"), [BRIDGE])
    assert r.verdict == REFUSED


def test_a_plain_figure_cannot_pass_as_the_adjusted_one(s1):
    c = claim("$693m", "Adjusted operating loss", direction="loss")
    r = decide(c, s1, located(s1, BRIDGE, "(693)", "Operating loss"), [BRIDGE])
    assert r.verdict == REFUSED and "adjusted" in r.why


@pytest.mark.parametrize("row,said,names", [
    ("Net loss(1)", "Net loss", True), ("Assets Cash and cash equivalents", "Cash and cash equivalents", True),
    ("Adjusted Operating Margin", "Operating margin", False), ("Operating loss", "Adjusted Operating Income", False),
    ("Total net sales", "sales", True), ("Net income", "income", True), ("", "x", False), ("Revenue", "", False),
])
def test_row_names(row, said, names):
    assert row_names(row, said) is names


def test_a_three_line_label_is_read_whole(s1):
    from finguard.page import _row_of
    words = s1.words(PNL)
    cell = next(w for w in words if w.text == "(39.88)")
    assert _row_of(cell, words).startswith("Net loss per share attributable")


def test_per_ordinary_share_is_per_share_not_a_share_count(asml):
    c = Claim("x", "€12.95", "Basic net income per ordinary share", "quarter", 2026, "gain")
    assert decide(c, asml, located(asml, 2, "12.95", "Basic net income per ordinary share"), [2]).verdict == MATCH


def test_a_positive_percentage_cannot_pass_on_a_negative_cell(s1):
    c = claim("4%", "Operating margin", direction="neutral")
    r = decide(c, s1, located(s1, BRIDGE, "(4)%", "Operating margin"), [BRIDGE])
    assert r.verdict == WRONG and "sign" in r.why


def test_a_negative_memo_figure_cannot_pass_on_a_positive_cell(s1):
    c = claim("-$16.3bn", "Revenue", direction="neutral")
    assert decide(c, s1, located(s1, BRIDGE, "16,286", "Revenue"), [BRIDGE]).verdict == WRONG


def test_a_negative_percentage_is_still_a_percentage(s1):
    c = claim("-4%", "Operating margin", direction="neutral")
    r = decide(c, s1, located(s1, BRIDGE, "(4)%", "Operating margin"), [BRIDGE])
    assert r.verdict == MATCH and r.source_text == "(4)%"


@pytest.mark.parametrize("memo,kind,stated", [
    ("Revenue in the six months to June 30, 2026 was $1m.", "six_months", True),
    ("Revenue in the interim period was $1m.", "six_months", False),
    ("Revenue in the six months was $1m.", "six_months", False),
    ("Q2 2026 revenue was $1m.", "quarter", True),
    ("Revenue in 2026 was $1m.", "quarter", False),
    ("Cash at June 30, 2026 was $1m.", "as_of", True),
    ("Cash was $1m in 2026.", "as_of", False),
    ("Revenue in 2026 was $1m.", "full_year", True),
])
def test_a_memo_must_state_its_own_period(memo, kind, stated):
    assert period_stated(memo, Claim(memo, "$1m", "Revenue", kind, 2026, "neutral")) is stated


def test_a_vague_memo_is_refused_before_muse_points(s1, run, muse_with):
    memo = "Adjusted operating income was $1.6bn for the interim period."
    extracted = {"quote": memo, "value_text": "$1.6bn", "metric": "Adjusted Operating Income",
                 "period_kind": "six_months", "period_year": 2026, "direction": "gain"}
    muse, transport = muse_with({"claims": [extracted]})
    r = check_memo(memo, s1, muse, run)[0]
    assert r.verdict == REFUSED and "period" in r.why and len(transport.sent) == 1


@pytest.mark.parametrize("value,year,kind,verdict", [("$541m", 2025, "full_year", MATCH), ("$541m", 2025, "six_months", REFUSED)])
def test_a_number_cannot_pass_under_the_next_columns_heading(s1, value, year, kind, verdict):
    """541 prints under full year 2025. Six months 2025 prints a dash, and the numbers sit close together."""
    c = claim(value, "Non-cash charitable contributions", kind=kind, year=year, direction="neutral")
    r = decide(c, s1, located(s1, BRIDGE, "541", "Non-cash charitable contributions(2)", nth=1), [BRIDGE])
    assert r.verdict == verdict and "Year Ended December 31, 2025" in r.column


def test_a_box_beside_the_number_finds_it_when_the_column_is_clear(s1):
    x0, y0, x1, y1 = box_for(s1, BRIDGE, "1,618")
    loc = {"found": True, "page": 0, "box": [x0 - 25, y0, x0 - 5, y1], "row_label": "Adjusted Operating Income (Loss)"}
    r = decide(claim(), s1, loc, [BRIDGE])
    assert r.verdict == MATCH and r.source_text == "1,618"


def test_a_box_between_two_columns_stays_unclear(s1):
    words = s1.words(BRIDGE)
    a = next(w for w in words if w.text == "(1,849)")
    b = next(w for w in words if w.text == "1,618")
    w = s1.size(BRIDGE)[0]
    mid = (a.cx + b.cx) / 2 * 1000 / w
    _, y0, _, y1 = box_for(s1, BRIDGE, "1,618")
    loc = {"found": True, "page": 0, "box": [mid - 3, y0, mid + 3, y1], "row_label": "Adjusted Operating Income (Loss)"}
    assert decide(claim(), s1, loc, [BRIDGE]).verdict == REFUSED


def test_a_box_landing_on_the_wrong_year_cannot_pass(s1):
    """The closest number is 2025's. Its own heading says so, so a 2026 claim is refused."""
    x0, y0, x1, y1 = box_for(s1, BRIDGE, "(1,849)")
    loc = {"found": True, "page": 0, "box": [x0 - 20, y0, x0 - 4, y1], "row_label": "Adjusted Operating Income (Loss)"}
    assert decide(claim("$1.8bn"), s1, loc, [BRIDGE]).verdict != MATCH


def test_a_pinned_page_image_is_used_as_saved(s1):
    """Page 371 renders differently on Linux, and a saved reply is keyed to the exact image."""
    pin = Path(__file__).parent.parent / "data" / "pinned_pages" / s1.sha256[:16] / "371.png"
    assert s1.image(371) == pin.read_bytes()


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


def _in_crop(filing, page, text, first_box):
    """The box Muse would report for `text` when shown only the crop around `first_box`."""
    bounds = _crop_bounds(first_box)
    full = box_for(filing, page, text)
    return [(full[0] - bounds[0]) * 1000 / (bounds[2] - bounds[0]),
            (full[1] - bounds[1]) * 1000 / (bounds[3] - bounds[1]),
            (full[2] - bounds[0]) * 1000 / (bounds[2] - bounds[0]),
            (full[3] - bounds[1]) * 1000 / (bounds[3] - bounds[1])]


def test_a_second_look_recovers_when_the_first_box_finds_several(s1, run, muse_with):
    c = Claim(**EXTRACTED)
    x0, y0, x1, y1 = box_for(s1, BRIDGE, "1,618")
    first = {"found": True, "page": _page_position(s1, c), "box": [100, y0, x1, y1],
             "row_label": "Adjusted Operating Income (Loss)"}
    assert _found_count(s1, [BRIDGE], dict(first, page=0)) > 1
    again = {"found": True, "page": 0, "box": _in_crop(s1, BRIDGE, "1,618", first["box"]),
             "row_label": "Adjusted Operating Income (Loss)"}
    muse, transport = muse_with({"claims": [EXTRACTED]}, first, again)
    receipts = check_memo(MEMO, s1, muse, run)
    assert [r.verdict for r in receipts] == [MATCH] and receipts[0].source_text == "1,618"
    assert len(transport.sent) == 3  # list the claims, point, then the one second look
    second = json.loads(transport.sent[2])
    parts = second["messages"][1]["content"]
    assert sum(p["type"] == "image_url" for p in parts) == 1  # the crop alone, enlarged
    words = json.dumps([p for p in parts if p["type"] == "text"])
    assert "Adjusted Operating Income" in words and "1.6" not in words and "Anthropic posted" not in words
    assert [e["kind"] for e in read_lines(run.path)] == ["start", "muse_call", "muse_call", "muse_call", "receipt", "end"]


def test_a_second_look_runs_once_when_it_still_finds_none(s1, run, muse_with):
    c = Claim(**EXTRACTED)
    first = {"found": True, "page": _page_position(s1, c), "box": box_for(s1, BRIDGE, "1,618"),
             "row_label": "Operating loss"}  # the box is on another row, so it finds no number
    assert _found_count(s1, [BRIDGE], dict(first, page=0)) == 0
    again = {"found": True, "page": 0, "box": _in_crop(s1, BRIDGE, "1,618", first["box"]),
             "row_label": "Operating loss"}
    muse, transport = muse_with({"claims": [EXTRACTED]}, first, again)
    r = check_memo(MEMO, s1, muse, run)[0]
    assert r.verdict == REFUSED and "0 numbers" in r.why
    assert len(transport.sent) == 3  # no third pointing call, however the second look went


def test_a_failed_second_look_keeps_the_first_refusal(s1, run, muse_with):
    c = Claim(**EXTRACTED)
    first = {"found": True, "page": _page_position(s1, c), "box": box_for(s1, BRIDGE, "1,618"),
             "row_label": "Operating loss"}
    muse, transport = muse_with({"claims": [EXTRACTED]}, first, MuseError("timeout", "slow"))
    r = check_memo(MEMO, s1, muse, run)[0]
    assert r.verdict == REFUSED and "0 numbers" in r.why and len(transport.sent) == 3


def test_no_second_look_when_the_first_reply_found_nothing(s1, run, muse_with):
    muse, transport = muse_with({"claims": [EXTRACTED]}, {"found": False, "page": 0, "box": [], "row_label": ""})
    r = check_memo(MEMO, s1, muse, run)[0]
    assert r.verdict == REFUSED and len(transport.sent) == 2


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
    assert r.verdict == REFUSED and "too long" in r.why


def test_a_skipped_number_shows_in_the_run(s1, run, muse_with):
    memo = MEMO + " Margin was 10%."
    c = Claim(**EXTRACTED)
    muse, _ = muse_with({"claims": [EXTRACTED]}, {"found": False, "page": 0, "box": [], "row_label": ""})
    assert UNCHECKED in [r.verdict for r in check_memo(memo, s1, muse, run)] and c
