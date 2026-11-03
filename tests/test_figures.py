from decimal import Decimal

import pytest

from finguard.figures import money, money_in_millions, numbers_in_memo, read_figure, same_size, scale_of_page


@pytest.mark.parametrize("text,amount,negative", [
    ("$1.6bn", "1600000000", False), ("(693)", "693", True), ("-$693m", "693000000", True),
    ("€11,662.0 million", "11662000000.0", False), ("$80.7 billion", "80700000000", False),
])
def test_reads_a_figure(text, amount, negative):
    f = read_figure(text)
    assert f.amount == Decimal(amount) and f.negative is negative


def test_two_numbers_are_not_one_figure():
    assert read_figure("$1 and $2") is None


def test_a_badly_grouped_number_is_refused():
    assert read_figure("1,61,8") is None


@pytest.mark.parametrize("claim,source,agrees", [
    ("$1.6bn", 1_618_000_000, True),
    ("$1.7bn", 1_618_000_000, False),
    ("$1,618m", 1_618_000_000, True),
    ("$1,600m", 1_618_000_000, False),  # written to the million, so it must agree to the million
    ("$80.7bn", 80_748_000_000, True),
    ("$81bn", 80_748_000_000, True),
])
def test_rounding_follows_how_the_claim_was_written(claim, source, agrees):
    assert same_size(read_figure(claim), Decimal(source)) is agrees


@pytest.mark.parametrize("text,power", [
    ("(in millions, except for percentages)", 6), ("In USD $ millions", 6), ("(Unaudited, €, in millions)", 6),
    ("(in thousands)", 3), ("no units here", None), ("in millions and in thousands", None),
    ("(in millions of U.S. dollars, except share and per share data)", 6),
    ("(In millions, except share amounts which are in thousands, and per share data)", 6),
])
def test_page_units(text, power):
    assert scale_of_page(text) == power


def test_every_figure_in_a_memo_is_seen_but_dates_are_not():
    found = [t for _, _, t in numbers_in_memo("The S-1 and 10-K say on June 30, 2026 revenue was $16.3bn, up 14% from 1,163 in Q2.")]
    assert found == ["$16.3bn", "14%", "1,163"]


@pytest.mark.parametrize("amount,currency,negative,text", [
    (80_748_000_000, "$", True, "-$80.7bn"), (16_286_000_000, "$", False, "$16.3bn"),
    (1_618_000_000, "$", False, "$1.6bn"), (693_000_000, "$", True, "-$693m"),
    ("582300000.0", "$", False, "$582.3m"), ("11662000000.0", "€", False, "€11.7bn"),
    ("0.13", "$", False, "$0.13"),
])
def test_one_way_to_write_money(amount, currency, negative, text):
    assert money(Decimal(amount), currency, negative) == text


@pytest.mark.parametrize("amount,negative,text", [
    (1_618_000_000, False, "$1,618m"), (693_000_000, True, "-$693m"), ("46700000", False, "$46.7m"),
    (8_499_000_000, False, "$8,499m"), (126_000_000, False, "$126m"), (1_000_000_000, False, "$1,000m"),
])
def test_money_for_a_sum_keeps_every_digit(amount, negative, text):
    assert money_in_millions(Decimal(amount), "$", negative) == text
