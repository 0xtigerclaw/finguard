"""Ways a false number once reached `match`, each proved on a real filing page. Muse's replies here are written by hand."""
from pathlib import Path

import pytest

from finguard.check import MATCH, REFUSED, UNCHECKED, WRONG, Claim, check_memo, decide, list_claims, period_stated, skipped_numbers
from finguard.figures import numbers_in_memo, read_figure, same_size, too_rough
from finguard.house import answer_question, load_rules
from finguard.muse import Muse, ReplayTransport
from finguard.page import Filing
from finguard.record import Run

DATA = Path(__file__).parent.parent / "data"
RECORDINGS = Path(__file__).parent / "recordings"
NVDA = DATA / "heldout" / "NVDA-Q2-FY2027.pdf"
filings: dict = {}


def filing(path) -> Filing:
    return filings.setdefault(path, Filing(path))


S1, ASML, NBIS = DATA / "ANTH-S1-2026.pdf", DATA / "ASML-Q3-2026.pdf", DATA / "NBIS-Q2-2026.pdf"


def verdict(path, page, printed, value, metric, kind, year, direction="neutral", label=None, nth=0, quote=None, memo=""):
    """The receipt when Muse points at this printed cell, on the row name it would copy."""
    f = filing(path)
    word = [w for w in f.words(page) if w.text == printed][nth]
    box = f.points_to_grid(page, (word.x0, word.y0, word.x1, word.y1))
    row = label if label is not None else f.cell(page, word).row
    claim = Claim(quote or f"It was {value}.", value, metric, kind, year, direction)
    return decide(claim, f, {"found": True, "page": 0, "box": box, "row_label": row}, [page], memo)


def not_match(*args, **kw):
    r = verdict(*args, **kw)
    assert r.verdict != MATCH, r.why
    return r


def matches(*args, **kw):
    r = verdict(*args, **kw)
    assert r.verdict == MATCH, r.why
    return r


# 1. The memo names a quarter, a month or a half. The claim keeps only a kind and a year.

@pytest.mark.parametrize("printed,value,metric,kind,page,quote", [
    ("11,662.0", "€11.7bn", "Total net sales", "quarter", 2, "Total net sales in the second quarter of 2026 were €11.7bn."),
    ("4,960.4", "€5.0bn", "Net income", "quarter", 2, "Net income in Q1 2026 was €5.0bn."),
    ("58,981.2", "€59.0bn", "Total assets", "as_of", 4, "At March 29, 2026, total assets were €59.0bn."),
])
def test_another_period_named_in_the_memo_is_refused(printed, value, metric, kind, page, quote):
    r = not_match(ASML, page, printed, value, metric, kind, 2026, quote=quote)
    assert r.verdict == REFUSED and "this number is under" in r.why


def test_the_second_half_is_not_the_first():
    r = not_match(S1, 157, "16,286", "$16.3bn", "Revenue", "six_months", 2026, quote="Revenue in the second half of 2026 was $16.3bn.")
    assert r.verdict == REFUSED and "H2" in r.why


def test_a_period_said_in_an_earlier_sentence_counts():
    memo = "Q2 2026 was strong. Net income was €5.0bn."
    r = not_match(ASML, 2, "4,960.4", "€5.0bn", "Net income", "quarter", 2026, quote="Net income was €5.0bn.", memo=memo)
    assert "Q2" in r.why


def test_the_right_period_still_passes():
    matches(ASML, 2, "11,662.0", "€11.7bn", "Total net sales", "quarter", 2026, quote="Q3 2026 total net sales were €11.7bn.")
    matches(ASML, 4, "58,981.2", "€59.0bn", "Total assets", "as_of", 2026, quote="At September 27, 2026, total assets were €59.0bn.")
    memo = "ASML's third quarter of 2026 was its best yet. Net income was €5.0bn."
    matches(ASML, 2, "4,960.4", "€5.0bn", "Net income", "quarter", 2026, quote="Net income was €5.0bn.", memo=memo)


def test_nearest_period_wins_when_a_sentence_has_two():
    quote = "Net sales in Q2 2026 were €11.7bn and in Q3 2026 €9.3bn."
    not_match(ASML, 2, "11,662.0", "€11.7bn", "Total net sales", "quarter", 2026, quote=quote)


# 2. A fiscal year is not a calendar year.

@pytest.mark.parametrize("printed,value,metric,kind,quote", [
    ("63,734", "$63.7bn", "Operating income", "quarter", "Operating income for the second quarter of fiscal 2026 was $63.7bn."),
    ("118,010", "$118.0bn", "Net income", "six_months", "Net income for the first half of fiscal 2026 was $118.0bn."),
    ("63,734", "$63.7bn", "Operating income", "quarter", "Q2 2026 operating income was $63.7bn."),
])
def test_a_filer_whose_quarters_end_off_the_calendar_is_not_mapped(printed, value, metric, kind, quote):
    r = not_match(NVDA, 5, printed, value, metric, kind, 2026, direction="gain", quote=quote)
    assert r.verdict == REFUSED


def test_a_date_on_the_same_filer_still_passes():
    matches(NVDA, 5, "63,734", "$63.7bn", "Operating income", "quarter", 2026, "gain",
            quote="Operating income for the three months ended July 26, 2026 was $63.7bn.")


def test_a_december_fiscal_year_is_the_calendar_year():
    matches(S1, 157, "4,585", "$4.6bn", "Revenue", "full_year", 2025, quote="Revenue for fiscal year 2025 was $4.6bn.")


# 3. Currency.

@pytest.mark.parametrize("value", ["£16.3bn", "¥16.3bn", "C$16.3bn", "A$16.3bn", "HK$16.3bn", "EUR 16.3bn", "16.3bn euros", "RMB16.3bn"])
def test_another_currency_is_wrong(value):
    assert verdict(S1, 157, "16,286", value, "Revenue", "six_months", 2026).verdict == WRONG


def test_no_currency_at_all_is_refused():
    assert verdict(S1, 157, "16,286", "16.3bn", "Revenue", "six_months", 2026).verdict == REFUSED


def test_a_currency_word_beside_the_value_counts():
    r = verdict(S1, 157, "16,286", "16.3bn", "Revenue", "six_months", 2026, quote="Revenue was 16.3bn euros in H1 2026.")
    assert r.verdict == WRONG


def test_the_right_currency_still_passes():
    matches(S1, 157, "16,286", "$16.3bn", "Revenue", "six_months", 2026)
    matches(S1, 157, "16,286", "USD 16.3bn", "Revenue", "six_months", 2026)
    matches(ASML, 2, "11,662.0", "€11.7bn", "Total net sales", "quarter", 2026)
    assert verdict(ASML, 2, "11,662.0", "£11.7bn", "Total net sales", "quarter", 2026).verdict == WRONG


def test_figures_read_the_whole_currency():
    assert [read_figure(v).currency for v in ("$5m", "US$5m", "C$5m", "€5m", "EUR5m", "5m")] == ["$", "$", "C$", "€", "€", None]


# 4. Sign and direction words when the claim's direction is neutral.

@pytest.mark.parametrize("path,page,printed,value,metric,kind,nth", [
    (ASML, 5, "(4,885.1)", "€4.9bn", "Increase in cash and cash equivalents", "nine_months", 0),
    (ASML, 5, "1,359.0", "€1.4bn", "Decrease in cash and cash equivalents", "quarter", 0),
    (NBIS, 7, "2,246.1", "$2.2bn", "Cash used in operating activities", "quarter", 1),
])
def test_a_two_sided_row_settles_which_side_by_the_sign(path, page, printed, value, metric, kind, nth):
    assert verdict(path, page, printed, value, metric, kind, 2026, nth=nth).verdict == WRONG


def test_an_unsigned_number_on_a_negative_row_is_not_passed():
    not_match(NBIS, 7, "(5,645.7)", "$5.6bn", "Cash from investing activities", "quarter", 2026, label="Net cash used in investing activities")
    not_match(S1, 32, "(39.88)", "$39.88", "Earnings per share", "six_months", 2026)


def test_true_sign_wording_still_passes():
    matches(ASML, 5, "1,359.0", "€1.4bn", "Increase in cash and cash equivalents", "quarter", 2026)
    matches(ASML, 5, "(4,885.1)", "€4.9bn", "Decrease in cash and cash equivalents", "nine_months", 2026)
    matches(ASML, 2, "(1,174.4)", "€1.2bn", "Research and development costs", "quarter", 2026)
    matches(NBIS, 7, "(5,645.7)", "$5.6bn", "Net cash used in investing activities", "quarter", 2026, nth=0, label="Net cash used in investing activities")


# 5. A gain called a loss, an expense called a benefit.

@pytest.mark.parametrize("page,printed", [(6, "780.6"), (7, "(780.6)")])
def test_a_gain_is_not_a_loss(page, printed):
    r = verdict(NBIS, page, printed, "$780.6m", "Loss from revaluation of investments in equity securities", "six_months", 2026, "loss")
    assert r.verdict == WRONG


def test_an_expense_is_not_a_benefit():
    assert verdict(NBIS, 6, "13.7", "$13.7m", "Income tax benefit", "quarter", 2026, "gain").verdict == WRONG


def test_a_loss_needs_a_row_that_says_so():
    assert verdict(ASML, 2, "68.2", "€68.2m", "Interest and other, net", "quarter", 2026, "loss").verdict == REFUSED


@pytest.mark.parametrize("page,printed", [(7, "(12.7)"), (9, "(12.7)")])
def test_brackets_around_income_mean_the_figure_in_brackets_is_income(page, printed):
    r = verdict(NBIS, page, printed, "$12.7m", "Loss from equity method investments", "quarter", 2026, "loss")
    assert r.verdict == WRONG
    matches(NBIS, page, printed, "$12.7m", "Income from equity method investments", "quarter", 2026, "gain")


def test_income_taxes_are_not_income():
    r = verdict(S1, 157, "(80,727)", "$80.7bn", "Income before provision for income taxes", "six_months", 2026)
    assert r.verdict == WRONG


def test_gains_in_a_cash_flow_statement_are_still_gains():
    matches(NBIS, 7, "(780.6)", "$780.6m", "Gain from revaluation of investments in equity securities", "six_months", 2026, "gain")


# 6. The words around the number.

@pytest.mark.parametrize("quote,value", [
    ("Revenue grew by $16.3bn in the first half of 2026.", "$16.3bn"),
    ("Revenue rose $16.3bn in the first half of 2026.", "$16.3bn"),
    ("Revenue was under $16bn in the six months of 2026.", "$16bn"),
    ("Revenue was more than $16bn in the six months of 2026.", "$16bn"),
    ("Revenue will reach $16.3bn in the first half of 2026.", "$16.3bn"),
    ("A $16.3bn increase in revenue in the first half of 2026.", "$16.3bn"),
])
def test_a_change_a_limit_or_a_forecast_is_not_a_level(quote, value):
    r = verdict(S1, 157, "16,286", value, "Revenue", "six_months", 2026, quote=quote)
    assert r.verdict == REFUSED


def test_plain_levels_still_pass():
    for quote in ("Revenue reached $16.3bn in the first half of 2026.", "Revenue rose to $16.3bn in the first half of 2026.",
                  "Revenue was about $16.3bn in the six months of 2026."):
        matches(S1, 157, "16,286", "$16.3bn", "Revenue", "six_months", 2026, quote=quote)
    matches(ASML, 5, "1,359.0", "€1.4bn", "Increase in cash and cash equivalents", "quarter", 2026,
            quote="Cash rose by €1.4bn in the quarter ended Sep 27, 2026.")


# 7. Rounding.

@pytest.mark.parametrize("printed,value,metric,year,kind,path,page,expected", [
    ("1,412", "$1bn", "Other operating and administrative", 2025, "full_year", S1, 157, REFUSED),
    ("1,500", "$2bn", "Bartz settlement", 2025, "full_year", S1, 157, REFUSED),
    ("431", "$0bn", "Interest income", 2025, "full_year", S1, 157, WRONG),
    ("7.9", "$0.0bn", "Income tax expense", 2026, "six_months", NBIS, 6, WRONG),
    ("320,272", "$0tn", "Total assets", 2026, "as_of", NVDA, 6, WRONG),
    ("0.2", "0%", "Other", 2024, "full_year", S1, 399, WRONG),
])
def test_rounding_that_loses_the_number_does_not_pass(printed, value, metric, year, kind, path, page, expected):
    assert verdict(path, page, printed, value, metric, kind, year).verdict == expected


def test_honest_rounding_still_passes():
    matches(S1, 157, "16,286", "$16bn", "Revenue", "six_months", 2026)
    matches(S1, 157, "4,585", "$4.6bn", "Revenue", "full_year", 2025)
    assert same_size(read_figure("$1tn"), read_figure("$500bn").amount) and too_rough(read_figure("$1tn"), read_figure("$500bn").amount)
    assert not same_size(read_figure("$0.0bn"), read_figure("$49m").amount)
    assert not too_rough(read_figure("58%"), read_figure("57.5").amount)


# 8. A narrower row.

@pytest.mark.parametrize("page", [0, 6])
def test_continuing_operations_is_not_the_whole(page):
    r = verdict(NBIS, page, "502.5", "$502.5m", "Net income", "quarter", 2025, "gain")
    assert r.verdict == REFUSED and "same measure" in r.why


def test_the_narrower_row_passes_when_named():
    matches(NBIS, 6, "502.5", "$502.5m", "Net income from continuing operations", "quarter", 2025, "gain")


# 9, 10. Table titles, prose read as column headings, and the table above.

@pytest.mark.parametrize("printed", ["$53,954", "$46,743", "$59,688", "$2.22"])
def test_a_sentence_is_not_a_column_heading(printed):
    f = filing(NVDA)
    word = next(w for w in f.words(2) if w.text == printed)
    assert f.cell(2, word).column == ""


def test_the_non_gaap_table_cannot_pass_as_net_income():
    assert verdict(NVDA, 2, "$53,954", "$54.0bn", "Net income", "quarter", 2026, "gain").verdict == REFUSED


@pytest.mark.parametrize("page,printed,value,metric,kind,year,direction,nth", [
    (399, "20", "$20m", "Provision for income taxes", "full_year", 2023, "neutral", 1),
    (399, "(41,945)", "$41.9bn", "Pre-tax book income", "full_year", 2023, "loss", 0),
    (160, "7", "$7m", "Provision for income taxes", "six_months", 2026, "neutral", 0),
    (160, "(8,757)", "$8.8bn", "Other income (expense), net", "six_months", 2025, "neutral", 1),
    (32, "2", "$2m", "Sales, marketing, and partnerships", "full_year", 2026, "neutral", 0),
])
def test_a_column_is_not_read_from_prose_or_another_table(page, printed, value, metric, kind, year, direction, nth):
    words = [w for w in filing(S1).words(page) if w.text == printed]
    if metric.startswith("Sales"):
        nth = next(i for i, w in enumerate(words) if filing(S1).cell(page, w).row.startswith("Sales"))
    assert verdict(S1, page, printed, value, metric, kind, year, direction, nth=nth).verdict == REFUSED


def test_a_narrow_number_is_read_under_its_own_year():
    f = filing(S1)
    word = next(w for w in f.words(32) if w.text == "2" and f.cell(32, w).row.startswith("Sales"))
    assert f.cell(32, word).column == "Year Ended December 31, 2023"


# 11. A heading that names two kinds.

def test_a_merged_heading_does_not_open_the_gate():
    r = verdict(S1, 357, "(2,637)", "$2.6bn", "Net cash provided by (used in) operating activities", "six_months", 2025)
    assert r.verdict == REFUSED
    f = filing(S1)
    word = next(w for w in f.words(357) if w.text == "(2,637)")
    assert f.cell(357, word).column == "Year Ended December 31, 2025"


# 12. A table of percentages with % on two rows only.

@pytest.mark.parametrize("printed,value,metric,direction", [
    ("55", "$55m", "Compute and infrastructure", "neutral"), ("19", "$19m", "Personnel", "neutral"), ("(4)", "$4m", "Operating loss", "loss"),
])
def test_a_percent_table_is_not_money(printed, value, metric, direction):
    assert verdict(S1, 158, printed, value, metric, "six_months", 2026, direction).verdict == WRONG


def test_a_table_mixing_money_and_percent_keeps_its_money():
    matches(S1, 159, "8,945", "$8,945m", "Compute and infrastructure", "six_months", 2026)
    matches(ASML, 3, "57.5", "57.5%", "Gross margin", "quarter", 2026)


# 13, 14. Units.

def test_a_caption_below_a_table_is_not_its_units():
    r = verdict(NVDA, 9, "46", "$46bn", "Cost of revenue", "quarter", 2026)
    assert r.verdict == REFUSED and "units" in r.why


def test_billion_in_a_sentence_is_not_the_page_scale():
    f = filing(NVDA)
    word = next(w for w in f.words(2) if w.text == "$108.0")
    assert f.cell(2, word) is None
    assert verdict(NVDA, 2, "$108.0", "$108.0m", "Revenue", "quarter", 2027, label="Revenue is expected to be").verdict == REFUSED


def test_a_share_count_is_not_money():
    assert verdict(S1, 32, "2,024,972,990", "$2.0bn", "Weighted-average number of shares outstanding, basic and diluted",
                   "six_months", 2026).verdict == REFUSED


# 15. Row names.

def test_a_name_must_hold_the_whole_printed_line():
    r = verdict(NBIS, 6, "133.6", "$133.6m", "Revenues", "quarter", 2026, label="Revenues")
    assert r.verdict == REFUSED and r.source_text == ""


@pytest.mark.parametrize("path,page,printed,value,metric,kind,label", [
    (ASML, 4, "37,025.9", "€37.0bn", "Total assets", "as_of", None),
    (ASML, 4, "36,917.3", "€36.9bn", "Total liabilities and shareholders equity", "as_of", None),
    (NBIS, 6, "24.1", "$24.1m", "Interest expense", "quarter", "Interest income"),
])
def test_one_shared_word_is_not_the_same_row(path, page, printed, value, metric, kind, label):
    r = verdict(path, page, printed, value, metric, kind, 2026, label=label)
    assert r.verdict == REFUSED and "plainly name" in r.why


def test_the_row_next_door_no_longer_blocks_a_true_number():
    matches(NVDA, 5, "96,221", "$96.2bn", "Revenue", "quarter", 2026)


def test_the_same_words_in_another_order_still_name_a_row():
    matches(NBIS, 6, "(175.9)", "$175.9m", "Operating loss", "quarter", 2026, "loss", label="Loss from operations")
    matches(ASML, 2, "(1,174.4)", "€1.2bn", "Research and development", "quarter", 2026, label="Research and development costs")


# 16. Repeated rows.

def test_a_row_printed_twice_with_other_numbers_is_refused():
    r = verdict(NBIS, 6, "84.5", "$84.5m", "Product development", "quarter", 2026)
    assert r.verdict == REFUSED and "more than once" in r.why


@pytest.mark.parametrize("printed,value,metric", [("191.0", "$191.0m", "Product development"), ("133.6", "$133.6m", "Cost of revenues"),
                                                   ("173.9", "$173.9m", "Sales, general and administrative")])
def test_a_row_is_not_refused_over_the_footnote_it_points_to(printed, value, metric):
    matches(NBIS, 6, printed, value, metric, "quarter", 2026)


def test_the_footnote_cell_still_cannot_pass_as_the_row():
    for printed, value, metric in (("84.5", "$84.5m", "Product development"), ("0.6", "$0.6m", "Cost of revenues"),
                                   ("17.4", "$17.4m", "Sales, general and administrative")):
        r = verdict(NBIS, 6, printed, value, metric, "quarter", 2026)
        assert r.verdict == REFUSED and "more than once" in r.why


def test_two_tables_with_no_footnote_between_them_still_refuse():
    r = verdict(ASML, 4, "1,547.5", "€1.5bn", "Other assets", "as_of", 2026)
    assert r.verdict == REFUSED and "more than once" in r.why
    r = verdict(ASML, 4, "1,554.1", "€1.6bn", "Other assets", "as_of", 2026)
    assert r.verdict == REFUSED and "more than once" in r.why


def test_a_repeated_label_needs_its_own_name():
    not_match(S1, 159, "55", "55%", "Personnel as a percentage of revenue", "six_months", 2026)


def test_bare_rows_take_their_meaning_from_the_heading():
    assert verdict(NVDA, 5, "24,190", "$24.2bn", "Basic earnings", "quarter", 2026).verdict == REFUSED
    assert verdict(ASML, 2, "383.1", "€383.1m", "Basic income", "quarter", 2026).verdict == REFUSED
    r = matches(NVDA, 5, "2.47", "$2.47", "Basic earnings per share", "quarter", 2026, "gain")
    assert r.source_money == "$2.47"


# 17. The number finder.

@pytest.mark.parametrize("memo", [
    "a $10-billion charge", "a $1.6-billion loss", "margins of 5-6%", "revenue of EUR1.6bn", "revenue of USD16,286m", "revenue of c.16bn",
    "$.5bn", "revenue of .5bn", "x16bn", "Revenue in June 12 million", "revenue in May 16.3bn", "Mar 3.5bn of debt", "Oct 12%",
    "sixteen billion dollars", "one hundred million", "revenue of 2025 dollars",
])
def test_the_finder_sees_it(memo):
    assert numbers_in_memo(memo)


def test_a_range_is_two_numbers():
    assert [t for _, _, t in numbers_in_memo("revenue of $1.6-1.7bn")] == ["$1.6", "1.7bn"]
    assert [t for _, _, t in numbers_in_memo("margins of 5-6%")] == ["5", "6%"]


@pytest.mark.parametrize("memo", ["The 10-K and the S-1", "On June 30, 2026 and 30 June 2026", "a 12-month period and a 5-year plan", "FY26 and Q3"])
def test_the_finder_still_leaves_these_alone(memo):
    assert not numbers_in_memo(memo)


# 18, 19, 20. The claim list and the memo.

class Lists:
    def __init__(self, memo, **claim):
        self.reply = {"claims": [{"quote": memo, "period_kind": "six_months", "period_year": 2026, "direction": "neutral",
                                  "metric": "Revenue", **claim}]}

    def ask(self, *_args, **_kw):
        return self.reply


def test_a_slice_of_a_bigger_number_is_not_a_claim():
    memo = "In the six months of 2026 revenue was $116.3bn."
    assert list_claims(memo, Lists(memo, value_text="16.3bn")) == []
    assert list_claims(memo, Lists(memo, value_text="$116.3bn"))
    signed = "In the six months of 2026 the result was -$5m."
    assert list_claims(signed, Lists(signed, value_text="$5m")) == []


def test_each_copy_of_a_sentence_needs_its_own_claim():
    memo = "H1 2026 six months. Revenue was $16.3bn. Full-year 2025. Revenue was $16.3bn."
    claim = Claim("Revenue was $16.3bn.", "$16.3bn", "Revenue", "six_months", 2026, "neutral")
    assert skipped_numbers(memo, [claim]) == ["$16.3bn"]
    assert skipped_numbers(memo, [claim, claim]) == []


@pytest.mark.parametrize("memo,quote,kind,stated", [
    ("Full-year 2025 revenue was $16.3bn. We plan a six-month review in 2026.", "Full-year 2025 revenue was $16.3bn.", "six_months", False),
    ("Revenue was $1m. 12026 ref. Q2.", "Revenue was $1m.", "quarter", False),
    ("Cash was at least $5m in 2026.", "Cash was at least $5m in 2026.", "as_of", False),
    ("Q2 2026 was fine.\n\nRevenue was $1m.", "Revenue was $1m.", "quarter", False),
    ("Second quarter 2026\n\nRevenue was $1m.", "Revenue was $1m.", "quarter", True),
    ("In Q2 2026 we grew. Revenue was $1m.", "Revenue was $1m.", "quarter", True),
])
def test_the_period_must_be_said_before_the_number_in_its_paragraph(memo, quote, kind, stated):
    assert period_stated(memo, Claim(quote, "$1m", "Revenue", kind, 2026, "neutral")) is stated


def test_muses_qualifier_must_be_in_the_memo():
    memo = "EBITDA was $236.2m in Q2 2026."
    r = verdict(NBIS, 0, "236.2", "$236.2m", "Adjusted EBITDA", "quarter", 2026, quote=memo, memo=memo)
    assert r.verdict == REFUSED
    memo = "Adjusted EBITDA was $236.2m in Q2 2026."
    matches(NBIS, 0, "236.2", "$236.2m", "Adjusted EBITDA", "quarter", 2026, quote=memo, memo=memo)


def test_a_qualifier_in_the_memo_must_be_in_the_claim():
    r = verdict(NBIS, 6, "(190.4)", "$190.4m", "Net loss", "quarter", 2026, "loss", label="Net income / (loss)",
                quote="Adjusted net loss was $190.4m in Q2 2026.")
    assert r.verdict == REFUSED


# Section D. Labels and house answers.

def test_a_refused_bare_row_shows_per_share_money_plainly():
    r = verdict(NBIS, 6, "(0.68)", "$0.68", "Basic loss per share", "quarter", 2026, "loss")
    assert "680" not in r.why and "680" not in r.source_money


@pytest.mark.parametrize("question", ["Under the house method, what is revenue for H1 2026?",
                                      "Under the house method, what is the net income for Q2 2026?"])
def test_a_house_answer_is_not_given_to_another_measure(tmp_path, question):
    answer = answer_question(load_rules(tmp_path), filing(S1), question)
    assert answer["status"] == "no_rule" and answer.get("house_money") is None


def test_the_house_working_stays_right(tmp_path):
    answer = answer_question(load_rules(tmp_path), filing(S1), "Under the house method, what is adjusted operating income for H1 2026?")
    assert answer["house_money"] == "-$567m" and "$1,618m - $2,185m = -$567m" in answer["working"]


# The saved memos, end to end.

def _saved(tmp_path, name, memo_file, pdf) -> dict:
    memo = (DATA.parent / "examples" / memo_file).read_text().strip()
    muse = Muse(ReplayTransport(RECORDINGS / name), Run(tmp_path / "muse"))
    return {r.value_text: r.verdict for r in check_memo(memo, filing(pdf), muse, Run(tmp_path / "run"))}


def test_the_planted_errors_stay_wrong(tmp_path):
    got = _saved(tmp_path / "a", "anthropic_memo", "anthropic_memo.txt", S1)
    assert got["$693m"] == WRONG and got["$8.7bn"] == WRONG
    got = _saved(tmp_path / "b", "asml_memo", "asml_memo.txt", ASML)
    assert got["$5.0bn"] == WRONG and got["€10.8bn"] == WRONG
    got = _saved(tmp_path / "c", "nebius_memo", "nebius_memo.txt", NBIS)
    assert got["$190.4m"] == WRONG and got["$2.8bn"] == WRONG


def test_the_corrected_memo_stays_all_match(tmp_path):
    got = _saved(tmp_path, "anthropic_memo_corrected", "anthropic_memo_corrected.txt", S1)
    assert set(got.values()) == {MATCH} and UNCHECKED not in got.values()


# Muse's claim is not the memo's words.

def test_a_memo_that_names_no_month_cannot_pick_between_two_dates():
    memo = "As of 2026, cash and cash equivalents were $10.6bn."
    r = verdict(NVDA, 6, "10,605", "$10.6bn", "Cash and cash equivalents", "as_of", 2026, quote=memo, memo=memo)
    assert r.verdict == REFUSED and "which" in r.why
    memo = "At January 25, 2026, cash and cash equivalents were $10.6bn."
    matches(NVDA, 6, "10,605", "$10.6bn", "Cash and cash equivalents", "as_of", 2026, quote=memo, memo=memo)


def test_muses_metric_must_be_in_the_memos_sentence():
    memo = "Revenues were $133.6m in Q2 2026."
    r = verdict(NBIS, 6, "133.6", "$133.6m", "Cost of revenues", "quarter", 2026, quote=memo, memo=memo, label="Cost of revenues")
    assert r.verdict == REFUSED


def test_muses_direction_cannot_turn_a_profit_into_a_loss():
    memo = "The company made an operating profit of $693m in the six months to June 30, 2026."
    r = verdict(S1, 157, "(693)", "$693m", "Operating loss", "six_months", 2026, "loss", quote=memo, memo=memo)
    assert r.verdict == WRONG


@pytest.mark.parametrize("words", [
    "Revenue was not", "Revenue fell short of", "Revenue came in shy of", "Revenue added", "Sub-",
    "Revenue wasn't", "Revenue was never", "Revenue missed", "Analysts doubted the",
])
def test_a_number_not_plainly_stated_is_not_passed(words):
    quote = f"{words}{'' if words.endswith('-') else ' '}$16.3bn in the six months ended June 30, 2026."
    r = verdict(S1, 157, "16,286", "$16.3bn", "Revenue", "six_months", 2026, quote=quote, memo=quote)
    assert r.verdict == REFUSED, r.why


@pytest.mark.parametrize("words", ["Revenue was", "Revenue of", "Revenue reached", "Revenue:"])
def test_a_number_plainly_stated_still_passes(words):
    quote = f"{words}{' ' if words else ''}$16.3bn in the six months ended June 30, 2026."
    r = verdict(S1, 157, "16,286", "$16.3bn", "Revenue", "six_months", 2026, quote=quote, memo=quote)
    assert r.verdict == MATCH, r.why
