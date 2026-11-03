"""House rules: rules as data, applied by plain code to the real filing. A scripted Muse only, never the API."""
import json
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from finguard import house, web
from finguard.core.context import FILINGS, corrected_memo, open_filing
from finguard.core.door import dispatch
from finguard.core.plugin import Refused, hash_of
from finguard.page import Filing
from finguard.plugins.release import verify_release
from finguard.record import read_lines
from tests.test_plugins_support import CORRECTED_MEMO, DATA, ScriptedMuse, _reply, build, check_corrected

ROOT = Path(__file__).parent.parent
BRIDGE = 166
METHOD = "Under our method, what was the operating result for the six months ended June 30, 2026?"
HR1_TEXT = "Stock-based compensation is an operating cost and is never added back"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    return build(tmp_path)


@pytest.fixture
def s1():
    return open_filing(DATA / "ANTH-S1-2026.pdf")


@pytest.fixture
def rules(tmp_path):
    return house.load_rules(tmp_path)


def approve(ctx, approval_id, by="terminal"):
    ctx.approvals.decide(approval_id, "approve", by, "looks right")


def change(ctx, enabled=False, rule_id="HR-1"):
    params = {"rule_id": rule_id, "enabled": enabled}
    asked = dispatch("rules.change", params, "web", ctx)
    assert asked.status == "needs_approval", asked.why
    approve(ctx, asked.approval["id"])
    out = dispatch("rules.change", params, "web", ctx, approval_id=asked.approval["id"])
    assert out.status == "done", out.why
    return out


def apply(ctx, question=METHOD, filing="anthropic"):
    return dispatch("rules.apply", {"filing": filing, "question": question}, "web", ctx)


def printed_at(filing, entry):
    """What the PDF itself prints inside an entry's box, read without any of Finguard's table code."""
    width, height = filing.size(entry["page"])
    x0, y0, x1, y1 = entry["box"]
    clip = pymupdf.Rect((x0 - 1) * width / 1000, (y0 - 1) * height / 1000, (x1 + 1) * width / 1000, (y1 + 1) * height / 1000)
    return [w[4] for w in filing.doc[entry["page"]].get_text("words", clip=clip) if w[4] != "$"]


def column_x(filing, entry):
    width, _ = filing.size(entry["page"])
    return (entry["box"][0] + entry["box"][2]) / 2 * width / 1000


# The worked example, read from the real page

def test_the_working_comes_from_the_real_page(ctx):
    out = apply(ctx)
    assert out.status == "done", out.why
    answer = out.result
    assert answer["status"] == "differs" and answer["house_money"] == "-$567m"
    assert answer["column"] == "Six Months Ended June 30, 2026" and answer["page"] == BRIDGE
    assert answer["working"] == (
        "Under Six Months Ended June 30, 2026, Adjusted Operating Income (Loss) is $1.6bn (printed as 1,618) and "
        "Stock-based compensation expense is $2.2bn (printed as 2,185). HR-1 does not add stock-based compensation back, so "
        "$1,618m - $2,185m = -$567m. The house operating result is -$567m rather than $1.6bn. "
        "That is not the Operating loss of -$693m (printed as (693)). The gap is $126m: Non-cash charitable contributions, $126m, "
        "and Bartz settlement, which prints a dash here. The filing adds these back too, and HR-1 leaves them in. "
        "-$693m + $126m = -$567m.")
    assert answer["drafted_by_muse"] is False and ctx.transport.calls == 0


def test_every_input_is_the_real_printed_cell_in_one_column(ctx, s1):
    answer = apply(ctx).result
    rows = {i["row"]: i for i in answer["inputs"]}
    assert set(rows) == {"Adjusted Operating Income (Loss)", "Stock-based compensation expense"}
    assert rows["Adjusted Operating Income (Loss)"]["printed"] == "1,618"
    assert rows["Stock-based compensation expense"]["printed"] == "2,185"
    entries = [*answer["inputs"], answer["gap"]["from"], *answer["gap"]["items"]]
    assert [e["printed"] for e in entries] == ["1,618", "2,185", "(693)", "126", "—"]
    centre = column_x(s1, answer["inputs"][0])
    for entry in entries:
        assert entry["page"] == BRIDGE
        assert printed_at(s1, entry) == [entry["printed"]]
        assert abs(column_x(s1, entry) - centre) < 25  # the five columns sit about 63 points apart
    # The stock compensation row is the one in this table, not the same row in the net income table below it.
    assert entries[1]["box"][3] < entries[0]["box"][1]
    assert answer["gap"]["gap"] == "126" and answer["gap"]["ties"] is True
    assert [r["source_text"] for r in answer["receipts"]] == ["1,618", "2,185", "(693)", "126", "—"]
    assert all(r["verdict"] == "match" and r["filing_sha256"] == s1.sha256 for r in answer["receipts"])


@pytest.mark.parametrize("question, column, adjusted, removed, operating, result, money", [
    ("six months 2025", "Six Months Ended June 30, 2025", "(1,849)", "286", "(2,135)", "-$2,135m", "-$2.1bn"),
    ("the year ended December 31, 2025", "Year Ended December 31, 2025", "(4,854)", "1,165", "(8,060)", "-$6,019m", "-$6.0bn"),
    ("FY2024", "Year Ended December 31, 2024", "(2,788)", "192", "(2,980)", "-$2,980m", "-$3.0bn"),
    ("full year 2023", "Year Ended December 31, 2023", "(522)", "29", "(551)", "-$551m", "-$551m"),
])
def test_another_column_uses_that_columns_own_cells(ctx, s1, question, column, adjusted, removed, operating, result, money):
    answer = apply(ctx, f"Under our rules, what is the operating result for {question}?").result
    assert (answer["status"], answer["column"], answer["house_money"]) == ("differs", column, money)
    assert [i["printed"] for i in answer["inputs"]] == [adjusted, removed]
    assert answer["gap"]["from"]["printed"] == operating
    assert result in answer["working"] and "-$567m" not in answer["working"]
    for entry in answer["inputs"]:
        assert printed_at(s1, entry) == [entry["printed"]]


def test_the_full_year_gap_names_both_other_items(ctx):
    answer = apply(ctx, "Under our method, FY2025?").result
    assert answer["gap"]["gap"] == "2,041"
    assert "Non-cash charitable contributions, $541m, and Bartz settlement, $1,500m" in answer["working"]


def test_a_column_whose_operating_loss_is_the_answer_says_so(ctx):
    answer = apply(ctx, "Under our method, H1 2025?").result
    assert answer["gap"]["gap"] == "0" and "That equals the Operating loss of -$2.1bn (printed as (2,135))" in answer["working"]


@pytest.mark.parametrize("quarter, column, cells, money", [
    ("Q3 2025", "Three Months Ended September 30, 2025", ["(1,284)", "305"], "-$1.6bn"),
    ("Q4 2025", "Three Months Ended December 31, 2025", ["(1,721)", "574"], "-$2.3bn"),
    ("Q2 2026", "Three Months Ended June 30, 2026", ["2,235", "1,261"], "$974m"),
])
def test_quarterly_columns_with_wrapped_rows_stay_apart(ctx, quarter, column, cells, money):
    answer = apply(ctx, f"Under our method, {quarter}?").result
    assert (answer["column"], answer["house_money"], answer["page"]) == (column, money, 170)
    assert [i["printed"] for i in answer["inputs"]] == cells


# When the rule cannot say

@pytest.mark.parametrize("filing", ["nebius", "asml"])
def test_a_filing_without_the_rows_says_so(ctx, filing):
    answer = apply(ctx, "Under our method, the six months ended June 30, 2026?", filing).result
    assert answer["status"] == "no_rows" and answer["house_money"] is None and answer["receipts"] == []
    assert "has nothing to apply to" in answer["answer"] and "adjusted operating income" in answer["answer"]
    assert ctx.transport.calls == 0


@pytest.mark.parametrize("question", ["Under our method, what about 2026?", "Under our method, how did it go?",
                                      "Under our method, six months 2025 against six months 2026?"])
def test_a_question_with_no_one_period_is_not_guessed(ctx, question):
    answer = apply(ctx, question).result
    assert answer["status"] == "needs_period" and answer["house_money"] is None
    assert "Six Months Ended June 30, 2026" in answer["columns"]


def test_two_columns_fitting_a_period_are_not_guessed(ctx):
    answer = apply(ctx, "Under our method, three months 2026?").result
    assert answer["status"] == "needs_period"
    assert answer["columns"] == ["Three Months Ended March 31, 2026", "Three Months Ended June 30, 2026"]


@pytest.mark.parametrize("question", ["Under our method, the second half of 2025?", "Under our method, nine months 2026?"])
def test_a_period_the_filing_does_not_print_is_unavailable(ctx, question):
    answer = apply(ctx, question).result
    assert answer["status"] == "unavailable" and answer["house_money"] is None
    assert "no column for the period asked" in answer["answer"]


@pytest.mark.parametrize("question, expected", [
    ("six months ended June 30, 2026", ("six_months", 2026, {"june"})),
    ("H1 2026", ("six_months", 2026, {"june"})),
    ("first half of 2026", ("six_months", 2026, {"june"})),
    ("second half 2025", ("six_months", 2025, {"december"})),
    ("FY25", ("full_year", 2025, set())),
    ("fiscal year 2024", ("full_year", 2024, set())),
    ("Q3 2025", ("quarter", 2025, {"september"})),
    ("in 2026", None),
    ("six months and full year 2026", None),
    ("no period at all", None),
])
def test_reading_a_period(question, expected):
    assert house.read_period(question) == expected


def _cell(filing, page, printed):
    words = [w for w in filing.words(page) if w.text == printed]
    return words[0]


def _receipt_on(filing, page, printed, verdict="match"):
    w = _cell(filing, page, printed)
    return {"verdict": verdict, "page": page, "source_text": printed, "filing_sha256": filing.sha256,
            "value_text": "$1.6bn", "box": filing.points_to_grid(page, (w.x0, w.y0, w.x1, w.y1))}


def test_a_summary_table_without_the_row_is_unavailable_and_the_verdict_stays(s1, rules):
    receipt = _receipt_on(s1, 34, "1,618")
    notes = house.apply_to_receipts(rules, s1, [receipt])
    assert receipt["verdict"] == "match" and len(notes) == 1
    note = receipt["house"]
    assert note["status"] == "unavailable" and note["house_money"] is None and note["rule_id"] == "HR-1"
    assert "no row for stock-based compensation" in note["reason"]
    assert "Six Months Ended June 30, 2026" in note["reason"]


def test_a_wrong_number_on_the_row_still_gets_a_note_and_stays_wrong(s1, rules):
    receipt = _receipt_on(s1, BRIDGE, "1,618", "wrong")
    house.apply_to_receipts(rules, s1, [receipt])
    assert receipt["verdict"] == "wrong" and receipt["house"]["house_money"] == "-$567m"


def test_a_refused_receipt_gets_no_note(s1, rules):
    receipt = _receipt_on(s1, BRIDGE, "1,618", "refused")  # its cell may be in the wrong period
    assert house.apply_to_receipts(rules, s1, [receipt]) == [] and "house" not in receipt


def test_a_cell_on_another_row_gets_no_note(s1, rules):
    receipt = _receipt_on(s1, BRIDGE, "16,286")
    assert house.apply_to_receipts(rules, s1, [receipt]) == [] and "house" not in receipt


def test_a_box_that_is_not_the_cell_gets_no_note(s1, rules):
    receipt = {**_receipt_on(s1, BRIDGE, "1,618"), "box": [1, 2, 3, 4]}
    assert house.apply_to_receipts(rules, s1, [receipt]) == [] and "house" not in receipt


def _custom(rules, **changes):
    data = json.loads(json.dumps(rules.data))
    data["rules"][0].update(changes)
    return house.Rules(data, hash_of(data), "repo")


def test_a_rule_is_data_so_other_data_gives_another_interpretation(s1, rules):
    other = _custom(rules, id="HR-X", text="The legal settlement stays in", removes={
        "label": "the settlement", "names": ["Bartz settlement"]})
    answer = house.answer_question(other, s1, "Under our method, FY2025?")
    assert answer["status"] == "differs" and answer["inputs"][1]["printed"] == "1,500"
    assert "-$4,854m - $1,500m = -$6,354m" in answer["working"]


def test_a_printed_dash_is_not_treated_as_a_number(s1, rules):
    other = _custom(rules, removes={"label": "the settlement", "names": ["Bartz settlement"]})
    answer = house.answer_question(other, s1, METHOD)
    assert answer["status"] == "unavailable" and answer["house_money"] is None
    assert "prints a dash under Six Months Ended June 30, 2026" in answer["answer"]


def _pdf(tmp_path, rows, title="Acme Widgets Holdings", name="made.pdf", headers=("2025", "2026")) -> Filing:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 60), f"{title}. Reconciliation (in millions)")
    page.insert_text((300, 90), "Year Ended December 31,")
    for x, year in zip((300, 400), headers):
        page.insert_text((x, 105), year)
    for i, (label, *cells) in enumerate(rows):
        page.insert_text((72, 125 + i * 16), label)
        for x, cell in zip((300, 400), cells):
            if cell:
                page.insert_text((x, 125 + i * 16), cell)
    path = tmp_path / name
    path.write_bytes(doc.tobytes())
    return Filing(path)


GOOD_ROWS = [("Operating income", "100", "(50)"), ("Stock-based compensation", "40", "80"),
             ("Restructuring", "—", "5"), ("Adjusted Operating Income", "140", "35")]


def test_the_same_rule_works_on_a_filing_it_has_never_seen(tmp_path, rules):
    filing = _pdf(tmp_path, GOOD_ROWS)
    answer = house.answer_question(rules, filing, "Under our method, the year ended December 31, 2026?")
    assert answer["status"] == "differs" and answer["house_money"] == "-$45m"
    assert "$35m - $80m = -$45m" in answer["working"] and "-$50m + $5m = -$45m" in answer["working"]
    assert answer["gap"]["gap"] == "5"
    earlier = house.answer_question(rules, filing, "Under our method, the year ended December 31, 2025?")
    assert earlier["house_money"] == "$100m" and "$140m - $40m = $100m" in earlier["working"]


def test_a_table_that_does_not_add_up_gives_no_number(tmp_path, rules):
    filing = _pdf(tmp_path, [("Operating income", "100", "(50)"), ("Stock-based compensation", "40", "80"),
                             ("Adjusted Operating Income", "140", "999")])
    answer = house.answer_question(rules, filing, "Under our method, the year ended December 31, 2026?")
    assert answer["status"] == "unavailable" and answer["house_money"] is None and "do not add up" in answer["answer"]


def test_a_row_missing_from_the_asked_column_gives_no_number(tmp_path, rules):
    filing = _pdf(tmp_path, [("Operating income", "100", "(50)"), ("Stock-based compensation", "40", ""),
                             ("Adjusted Operating Income", "140", "35")])
    answer = house.answer_question(rules, filing, "Under our method, the year ended December 31, 2026?")
    assert answer["status"] == "unavailable" and answer["house_money"] is None
    assert "no row for stock-based compensation" in answer["answer"]  # 40 is the other column's, and is never used


def test_a_row_printed_as_a_deduction_is_not_taken_as_an_add_back(tmp_path, rules):
    filing = _pdf(tmp_path, [("Operating income", "100", "(50)"), ("Stock-based compensation", "40", "(80)"),
                             ("Adjusted Operating Income", "140", "(130)")])
    answer = house.answer_question(rules, filing, "Under our method, the year ended December 31, 2026?")
    assert answer["status"] == "unavailable" and "not an amount added back" in answer["answer"]


def test_a_longer_row_is_not_taken_for_the_rows_name(tmp_path, rules):
    filing = _pdf(tmp_path, [("Operating income", "100", "(50)"), ("Payroll tax on stock-based compensation", "40", "80"),
                             ("Adjusted Operating Income", "140", "30")])
    answer = house.answer_question(rules, filing, "Under our method, the year ended December 31, 2026?")
    assert answer["status"] == "unavailable" and "no row for stock-based compensation" in answer["answer"]


def test_a_table_with_no_starting_row_computes_and_says_the_gap_was_not_checked(tmp_path, rules):
    filing = _pdf(tmp_path, [("Stock-based compensation", "40", "80"), ("Adjusted Operating Income", "140", "35")])
    answer = house.answer_question(rules, filing, "Under our method, the year ended December 31, 2026?")
    assert answer["house_money"] == "-$45m" and answer["gap"] is None
    assert "the gap to it was not checked" in answer["working"]


def test_money_is_worked_in_decimal(tmp_path, rules):
    filing = _pdf(tmp_path, [("Operating income", "0.1", "0.3"), ("Stock-based compensation", "0.2", "0.1"),
                             ("Adjusted Operating Income", "0.3", "0.4")])
    answer = house.answer_question(rules, filing, "Under our method, the year ended December 31, 2026?")
    assert "$0.4m - $0.1m = $0.3m" in answer["working"] and answer["gap"]["gap"] == "0.0"  # binary floats would miss this
    assert Decimal(answer["gap"]["gap"]) == 0


# memo.check and memo.draft

def test_a_check_attaches_the_note_and_keeps_the_verdict(ctx, rules):
    out = check_corrected(ctx)  # asserts all six numbers still match
    result = out.result
    assert result["rules_sha256"] == rules.sha256 and result["rules_label"] == "ILLUSTRATIVE"
    marked = [r for r in result["receipts"] if "house" in r]
    assert [r["value_text"] for r in marked] == ["$1.6bn"] and marked[0]["verdict"] == "match"
    note = marked[0]["house"]
    assert (note["rule_id"], note["text"], note["status"], note["house_money"]) == ("HR-1", HR1_TEXT, "differs", "-$567m")
    assert [i["printed"] for i in note["inputs"]] == ["1,618", "2,185"]
    assert {"row", "printed", "box", "page"} <= set(note["inputs"][1])
    assert "$1,618m - $2,185m = -$567m" in note["working"]
    assert [n["house_money"] for n in result["house_notes"]] == ["-$567m"]
    assert result["record"]["holds"] is True


def test_the_rules_and_notes_sit_on_the_runs_record(ctx):
    out = check_corrected(ctx)
    lines = read_lines(ctx.ledger.runs_folder / out.run_id / "run.jsonl")
    line = next(e for e in lines if e["kind"] == "house")
    assert line["rules_sha256"] == out.result["rules_sha256"] and line["notes"] == out.result["house_notes"]
    # The receipts on the record are the filing's alone. The note sits beside them.
    assert not any("house" in e for e in lines if e["kind"] == "receipt")
    assert ctx.ledger.verify() == []


def test_a_saved_memo_still_replays_and_gets_the_rules(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    ctx = build(tmp_path)
    ctx.transport = None  # the replayed replies only
    out = dispatch("memo.check", {"memo": corrected_memo("anthropic"), "filing": "anthropic"}, "web", ctx)
    assert out.status == "done", out.why
    assert out.result["saved"] is True and len(out.result["rules_sha256"]) == 64
    assert out.result["house_notes"] == [n for n in out.result["house_notes"] if n["rule_id"] == "HR-1"]


class DraftingMuse(ScriptedMuse):
    """Stands in for Muse on memo.draft. It is not Muse."""

    def send(self, body: bytes) -> bytes:
        request = json.loads(body)
        if str(request["messages"][0]["content"]).startswith("You write a short memo"):
            self.calls += 1
            return _reply({"memo": CORRECTED_MEMO, "next_questions": []})
        return super().send(body)


def test_a_draft_records_the_rules_too(tmp_path, monkeypatch, rules):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    ctx = build(tmp_path)
    ctx.transport = DraftingMuse()
    out = dispatch("memo.draft", {"filing": "anthropic", "question": "How did the six months to June 30, 2026 go?"},
                   "web", ctx)
    assert out.status == "done", out.why
    assert out.result["rules_sha256"] == rules.sha256
    assert [n["house_money"] for n in out.result["house_notes"]] == ["-$567m"]
    lines = read_lines(ctx.ledger.runs_folder / out.run_id / "run.jsonl")
    assert sum(e["kind"] == "house" for e in lines) == 1


# Turning a rule off changes what the same cell means

def test_a_disabled_rule_gives_no_note_and_another_interpretation(ctx):
    on = check_corrected(ctx).result
    change(ctx, enabled=False)
    off = check_corrected(ctx).result
    cell = lambda result: next(r for r in result["receipts"] if r["value_text"] == "$1.6bn")  # noqa: E731
    assert cell(on)["source_text"] == cell(off)["source_text"] == "1,618"
    assert cell(on)["verdict"] == cell(off)["verdict"] == "match"
    assert cell(on)["house"]["house_money"] == "-$567m" and "house" not in cell(off)
    assert off["house_notes"] == [] and on["rules_sha256"] != off["rules_sha256"]


def test_a_disabled_rule_answers_and_forbids_nothing(ctx):
    change(ctx, enabled=False)
    answer = apply(ctx).result
    assert answer["status"] == "no_rule" and answer["house_money"] is None
    assert apply(ctx, "Add back stock compensation to the six months of 2026").status == "done"
    house.refuse_forbidden(ctx, "Add back stock compensation")  # does not raise


def test_turning_it_back_on_interprets_again_under_a_new_version(ctx, rules):
    change(ctx, enabled=False)
    back = change(ctx, enabled=True).result
    assert back["version"] == 3 and back["rules_sha256"] != rules.sha256
    assert apply(ctx).result["house_money"] == "-$567m"


# rules.change

def test_a_change_waits_for_a_person_and_binds_to_the_proposed_rules(ctx, rules):
    params = {"rule_id": "HR-1", "enabled": False}
    asked = dispatch("rules.change", params, "web", ctx)
    assert asked.status == "needs_approval"
    assert not house.state_file(ctx.state).exists() and house.load_rules(ctx.state).sha256 == rules.sha256
    proposed = json.loads(json.dumps(rules.data))
    proposed["rules"][0]["enabled"] = False
    proposed["version"] = 2
    assert asked.approval["subject_sha256"] == hash_of(proposed)
    assert dispatch("rules.change", params, "web", ctx, approval_id=asked.approval["id"]).status == "refused"
    assert not house.state_file(ctx.state).exists()


def test_a_model_or_web_actor_cannot_approve_a_change(ctx):
    from finguard.core.approvals import NotOperator
    asked = dispatch("rules.change", {"rule_id": "HR-1", "enabled": False}, "web", ctx)
    with pytest.raises(NotOperator):
        ctx.approvals.decide(asked.approval["id"], "approve", "model", "")


def test_a_change_goes_on_the_ledger_with_who_approved_it(ctx, rules):
    out = change(ctx, enabled=False)
    line = next(e for e in ctx.ledger.lines() if e["kind"] == "rules_changed")
    assert line["rule_id"] == "HR-1" and line["enabled"] is False and line["approved_by"] == "terminal"
    assert line["requested_by"] == "web" and line["previous_sha256"] == rules.sha256
    assert line["rules_sha256"] == out.result["rules_sha256"] == house.load_rules(ctx.state).sha256
    assert line["run_id"] == out.run_id and out.result["approved_by"] == "terminal"
    assert ctx.ledger.verify() == []


def test_an_approval_works_once(ctx):
    params = {"rule_id": "HR-1", "enabled": False}
    asked = dispatch("rules.change", params, "web", ctx)
    approve(ctx, asked.approval["id"])
    assert dispatch("rules.change", params, "web", ctx, approval_id=asked.approval["id"]).status == "done"
    again = dispatch("rules.change", params, "web", ctx, approval_id=asked.approval["id"])
    assert again.status == "refused"
    change(ctx, enabled=True)
    replayed = dispatch("rules.change", params, "web", ctx, approval_id=asked.approval["id"])
    assert replayed.status == "refused" and "already been used" in replayed.why
    assert sum(e["kind"] == "rules_changed" for e in ctx.ledger.lines()) == 2


def test_an_approval_is_void_if_the_rules_moved_on(ctx):
    params = {"rule_id": "HR-1", "enabled": False}
    stale = dispatch("rules.change", params, "web", ctx)
    approve(ctx, stale.approval["id"])
    change(ctx, enabled=False)
    change(ctx, enabled=True)
    out = dispatch("rules.change", params, "web", ctx, approval_id=stale.approval["id"])
    assert out.status == "refused" and "changed after it was approved" in out.why
    assert house.load_rules(ctx.state).data["rules"][0]["enabled"] is True


def test_an_approval_for_one_change_cannot_make_another(ctx):
    asked = dispatch("rules.change", {"rule_id": "HR-1", "enabled": False}, "web", ctx)
    approve(ctx, asked.approval["id"])
    change(ctx, enabled=False)
    out = dispatch("rules.change", {"rule_id": "HR-1", "enabled": True}, "web", ctx, approval_id=asked.approval["id"])
    assert out.status == "refused" and house.load_rules(ctx.state).data["rules"][0]["enabled"] is False


@pytest.mark.parametrize("params, words", [
    ({"rule_id": "HR-9", "enabled": False}, "No such rule"),
    ({"rule_id": "HR-1", "enabled": True}, "already on"),
    ({"rule_id": "HR-1", "enabled": "no"}, "true or false"),
    ({"rule_id": "../x", "enabled": False}, "right form"),
    ({"rule_id": "HR-1"}, "enabled is missing"),
])
def test_bad_changes_are_refused(ctx, params, words):
    out = dispatch("rules.change", params, "web", ctx)
    assert out.status == "refused" and words in out.why and ctx.approvals.all() == []


# memo.release

def _ask_release(ctx, run_id):
    return dispatch("memo.release", {"check_run_id": run_id}, "web", ctx)


def test_a_release_is_refused_after_a_rule_changed(ctx):
    run = check_corrected(ctx)
    change(ctx, enabled=False)
    out = _ask_release(ctx, run.run_id)
    assert out.status == "refused" and out.approval is None
    assert "Check it again under the current rules" in out.why


def test_an_approved_release_is_refused_if_a_rule_changes_before_it_runs(ctx):
    run = check_corrected(ctx)
    asked = _ask_release(ctx, run.run_id)
    approve(ctx, asked.approval["id"])
    change(ctx, enabled=False)
    out = dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx, approval_id=asked.approval["id"])
    assert out.status == "refused" and "Check it again" in out.why
    assert not list((ctx.state / "releases").glob("rel_*"))
    assert ctx.approvals.get(asked.approval["id"])["status"] == "approved"  # not spent on a refusal


def test_checking_again_under_the_new_rules_can_be_released(ctx):
    check_corrected(ctx)
    change(ctx, enabled=False)
    run = check_corrected(ctx)
    asked = _ask_release(ctx, run.run_id)
    assert asked.status == "needs_approval", asked.why
    approve(ctx, asked.approval["id"])
    out = dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx, approval_id=asked.approval["id"])
    assert out.status == "done" and out.result["manifest"]["house_notes"] == []


def test_house_notes_do_not_block_a_release_and_the_manifest_keeps_them(ctx):
    run = check_corrected(ctx)
    assert run.result["house_notes"]
    asked = _ask_release(ctx, run.run_id)
    assert asked.status == "needs_approval", asked.why
    approve(ctx, asked.approval["id"])
    out = dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx, approval_id=asked.approval["id"])
    assert out.status == "done", out.why
    manifest = out.result["manifest"]
    assert manifest["rules_sha256"] == run.result["rules_sha256"]
    assert manifest["house_notes"] == run.result["house_notes"]
    assert out.result["holds"] is True and verify_release(ctx, out.result["release_id"]) == []
    sealed = next(e for e in ctx.ledger.lines() if e["kind"] == "release_sealed")
    assert sealed["rules_sha256"] == run.result["rules_sha256"]
    # A later rule change does not undo what was released under the rules of the day.
    change(ctx, enabled=False)
    assert verify_release(ctx, out.result["release_id"]) == []


def test_a_run_with_no_rules_on_its_record_cannot_be_released(ctx):
    from tests.test_plugins_support import rechain
    run = check_corrected(ctx)
    folder = ctx.ledger.runs_folder / run.run_id

    def forget(entry):
        entry.pop("rules_sha256", None)

    rechain(folder / "run.jsonl", forget)
    out = _ask_release(ctx, run.run_id)
    assert out.status == "refused"


# Questions the rule forbids

@pytest.mark.parametrize("question", [
    "What is adjusted operating income if we add back stock compensation?",
    "Add back stock-based compensation for the six months of 2026",
    "Show the operating result with SBC added back",
    "adding back share-based comp, what do we get?",
    "What is the result excluding stock-based compensation?",
    "Under our method, what is it before stock comp?",
    "Under our rules, add back equity-based compensation for 2026",
    "ex-SBC, how did the six months of 2026 go?",
    "Please do not hesitate to add back SBC",
    "Without adding back rent, but adding back SBC, what do we get?",
])
def test_a_forbidden_question_is_refused_with_no_model_call(ctx, question):
    out = apply(ctx, question)
    assert out.status == "refused" and out.result is None
    assert out.why.startswith(f"HR-1: {HR1_TEXT}.") and "Nothing was sent to Muse" in out.why
    assert question not in out.why  # a question is data, and is not echoed back
    assert ctx.transport.calls == 0
    assert any(e["kind"] == "refused" and e["run_id"] == out.run_id and e["why"] == out.why for e in ctx.ledger.lines())
    with pytest.raises(Refused, match="HR-1"):
        house.refuse_forbidden(ctx, question)


@pytest.mark.parametrize("question", [
    "What was stock-based compensation expense in 2026?",
    "How much revenue was there in the six months of 2026?",
    "What is adjusted operating income?",
    "What did the company add to its cash?",
])
def test_an_ordinary_question_is_not_refused(ctx, question):
    house.refuse_forbidden(ctx, question)
    out = apply(ctx, question)
    assert out.status == "done" and out.result["status"] == "no_rule" and ctx.transport.calls == 0


@pytest.mark.parametrize("question", [
    "Under our method, without adding back stock compensation, what was the six months of 2026?",
    "Under our rules, not adding back SBC, H1 2026?",
    "Under our method, excluding the stock comp add-back, H1 2026?",
])
def test_asking_not_to_add_it_back_is_the_method_and_is_answered(ctx, question):
    assert apply(ctx, question).result["house_money"] == "-$567m"


@pytest.mark.parametrize("question", [METHOD, "Using our rules: H1 2026", "By our house rules, the six months of 2026?",
                                      "What is the house operating result for the first half of 2026?"])
def test_the_method_pattern_comes_from_the_data(ctx, question):
    assert apply(ctx, question).result["rule_id"] == "HR-1"


def test_an_applied_rule_is_on_the_ledger(ctx, rules):
    out = apply(ctx)
    line = next(e for e in ctx.ledger.lines() if e["kind"] == "rules_applied")
    assert line["run_id"] == out.run_id and line["rule_ids"] == ["HR-1"] and line["house_money"] == "-$567m"
    assert line["rules_sha256"] == rules.sha256 and line["filing"] == "anthropic" and line["status"] == "differs"
    done = next(e for e in ctx.ledger.lines() if e["kind"] == "done" and e["run_id"] == out.run_id)
    assert done["filing"] == "anthropic" and done["result_sha256"] == hash_of(out.result)
    assert ctx.ledger.verify() == []


def test_a_bad_filing_or_empty_question_is_refused(ctx):
    assert apply(ctx, METHOD, "nope").status == "refused"
    assert apply(ctx, "   ").status == "refused"
    assert apply(ctx, "x" * 501).status == "refused"


# rules.list, and through the door on the web

def test_rules_list(ctx, rules):
    out = dispatch("rules.list", {}, "web", ctx)
    assert out.status == "done" and out.result["label"] == "ILLUSTRATIVE" and out.result["source"] == "repo"
    assert out.result["rules_sha256"] == rules.sha256 and out.result["holds"] is True and out.result["version"] == 1
    assert out.result["rules"] == [{"id": "HR-1", "text": HR1_TEXT, "enabled": True,
                                    "result_label": "house operating result",
                                    "interprets": "adjusted operating income", "removes": "stock-based compensation"}]
    change(ctx, enabled=False)
    after = dispatch("rules.list", {}, "web", ctx).result
    assert after["source"] == "state" and after["rules"][0]["enabled"] is False and after["version"] == 2


def test_the_kinds_are_declared_as_asked(ctx):
    by_name = {r["name"]: r for r in ctx.registry.rules()}
    assert (by_name["rules.list"]["kind"], by_name["rules.list"]["approval"]) == ("read", "none")
    assert (by_name["rules.apply"]["kind"], by_name["rules.apply"]["approval"]) == ("read", "none")
    assert (by_name["rules.change"]["kind"], by_name["rules.change"]["approval"]) == ("act", "person")


def test_the_web_door_reaches_the_rules_with_no_new_route(ctx, monkeypatch):
    monkeypatch.setattr(web, "ctx", ctx)
    monkeypatch.delenv("FINGUARD_OPERATOR_TOKEN", raising=False)
    client = TestClient(web.app)
    listed = client.post("/api/do/rules.list", json={"params": {}}).json()
    assert listed["status"] == "done" and listed["result"]["label"] == "ILLUSTRATIVE"
    answered = client.post("/api/do/rules.apply", json={"params": {"filing": "anthropic", "question": METHOD}}).json()
    assert answered["result"]["house_money"] == "-$567m"
    asked = client.post("/api/do/rules.change", json={"params": {"rule_id": "HR-1", "enabled": False}}).json()
    assert asked["status"] == "needs_approval"
    assert not [r for r in web.app.routes if "rules." in getattr(r, "path", "")]


# A file changed outside rules.change

def _edit_rules(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["rules"][0]["enabled"] = not data["rules"][0]["enabled"]
    path.write_text(json.dumps(data), encoding="utf-8")


def test_a_tampered_state_file_is_detected_through_the_hash(ctx):
    check = check_corrected(ctx)
    change(ctx, enabled=False)
    run = check_corrected(ctx)
    _edit_rules(house.state_file(ctx.state))  # someone turns the rule back on by hand
    listed = dispatch("rules.list", {}, "web", ctx).result
    assert listed["holds"] is False and "do not match the last approved change" in listed["problem"]
    calls = ctx.transport.calls
    for out in (apply(ctx), _ask_release(ctx, run.run_id), _ask_release(ctx, check.run_id),
                dispatch("memo.check", {"memo": CORRECTED_MEMO, "filing": "anthropic"}, "web", ctx),
                dispatch("rules.change", {"rule_id": "HR-1", "enabled": False}, "web", ctx)):
        assert out.status == "refused" and "do not match the last approved change" in out.why
    assert ctx.transport.calls == calls  # no Muse call was spent on rules that do not hold


def test_a_rules_file_with_no_approval_behind_it_is_detected(ctx):
    path = house.state_file(ctx.state)
    path.parent.mkdir(parents=True)
    path.write_text((house.DEFAULT_RULES).read_text(encoding="utf-8"), encoding="utf-8")
    out = apply(ctx)
    assert out.status == "refused" and "no approved change" in out.why
    assert dispatch("rules.list", {}, "web", ctx).result["holds"] is False


def test_a_deleted_state_file_is_detected(ctx):
    change(ctx, enabled=False)
    house.state_file(ctx.state).unlink()
    assert apply(ctx).status == "refused"


def test_a_changed_repo_default_is_caught_when_releasing(ctx, tmp_path, monkeypatch):
    run = check_corrected(ctx)
    copy = tmp_path / "default.json"
    copy.write_text(house.DEFAULT_RULES.read_text(encoding="utf-8"), encoding="utf-8")
    _edit_rules(copy)
    monkeypatch.setattr(house, "DEFAULT_RULES", copy)
    out = _ask_release(ctx, run.run_id)
    assert out.status == "refused" and "Check it again" in out.why


def test_the_hash_ignores_layout_and_follows_content(tmp_path, rules):
    path = house.state_file(tmp_path)
    house.write_rules(tmp_path, rules.data)
    assert house.load_rules(tmp_path).sha256 == rules.sha256 and house.load_rules(tmp_path).source == "state"
    path.write_text(path.read_text(encoding="utf-8").replace("never added back", "sometimes added back"), encoding="utf-8")
    assert house.load_rules(tmp_path).sha256 != rules.sha256


@pytest.mark.parametrize("content", ["{not json", "[]", '{"version": 1, "label": "x"}',
                                     '{"version": 1, "label": "x", "rules": [{"id": "A"}]}'])
def test_a_broken_rules_file_is_refused_in_plain_words(tmp_path, content):
    house.state_file(tmp_path).parent.mkdir(parents=True)
    house.state_file(tmp_path).write_text(content, encoding="utf-8")
    with pytest.raises(Refused, match="house rules"):
        house.load_rules(tmp_path)


def test_a_rule_with_a_bad_pattern_or_a_clash_is_refused(tmp_path, rules):
    bad = json.loads(json.dumps(rules.data))
    bad["rules"][0]["refuse_patterns"] = ["("]
    house.write_rules(tmp_path, bad)
    with pytest.raises(Refused, match="cannot be read"):
        house.load_rules(tmp_path)
    clash = json.loads(json.dumps(rules.data))
    clash["rules"].append({**clash["rules"][0], "id": "HR-2"})
    house.write_rules(tmp_path, clash)
    with pytest.raises(Refused, match="same row"):
        house.load_rules(tmp_path)


# The rules are data

def test_the_rules_file_is_versioned_and_labelled_illustrative():
    data = json.loads(house.DEFAULT_RULES.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["label"] == "ILLUSTRATIVE" and "not any fund's real method" in data["note"]
    rule = data["rules"][0]
    assert rule["id"] == "HR-1" and rule["text"] == HR1_TEXT and rule["enabled"] is True
    assert rule["interprets"]["label"] == "adjusted operating income"
    assert rule["removes"]["label"] == "stock-based compensation"
    assert rule["refuse_patterns"]


def test_no_company_or_filing_figure_is_hard_coded():
    sources = [ROOT / "finguard" / "house.py", ROOT / "finguard" / "plugins" / "rules.py", house.DEFAULT_RULES]
    names = {FILINGS[key][1].lower() for key in FILINGS} | {"bartz", "claude", "s-1"}
    figures = {"1,618", "1618", "2,185", "2185", "-567", "16,286", "166"}
    for path in sources:
        text = path.read_text(encoding="utf-8").lower()
        assert not {n for n in names if n in text}, path
        assert not {f for f in figures if f in text}, path
    logic = (ROOT / "finguard" / "house.py").read_text(encoding="utf-8").lower()
    assert "hr-1" not in logic and "adjusted operating" not in logic and "stock-based" not in logic


def test_no_rule_text_reaches_muse(ctx):
    sent = []
    real = ctx.transport.send

    def spy(body):
        sent.append(body.decode("utf-8", "replace"))
        return real(body)

    ctx.transport.send = spy
    check_corrected(ctx)
    assert sent and not any(HR1_TEXT in body or "HR-1" in body or "-567" in body for body in sent)
