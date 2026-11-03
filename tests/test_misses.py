"""Capture a miss: a flag becomes a case that replays, and changes no verdict."""
import json

import pytest

from finguard.cli import main as cli
from finguard.core.door import dispatch
from finguard.plugins import misses as plugin
from finguard.plugins.misses import all_cases, export_case, mark_fixed, replay_case
from tests.test_plugins_support import build, check_corrected


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setattr(plugin, "COMMITTED", tmp_path / "committed")
    return build(tmp_path)


def flag(ctx, run_id, index=0, should_be="wrong", reason="The filing's own note restates this."):
    return dispatch("memo.flag", {"check_run_id": run_id, "index": index, "should_be": should_be, "reason": reason}, "web", ctx)


def test_a_flag_saves_a_case_that_replays_on_the_ledger(ctx):
    run = check_corrected(ctx)
    out = flag(ctx, run.run_id)
    assert out.status == "done", out.why
    assert out.result["status"] == "open"
    case = all_cases(ctx)[0]
    assert case["verdict"] == "match" and case["should_be"] == "wrong" and case["flagged_by"] == "web"
    assert case["replies"] and case["filing"] == "anthropic"
    assert any(e["kind"] == "miss_flagged" and e["case_id"] == case["id"] for e in ctx.ledger.lines())
    assert ctx.ledger.verify() == []
    assert replay_case(case, ctx)["status"] == "open"


def test_a_flag_changes_no_verdict(ctx):
    before = check_corrected(ctx)
    flag(ctx, before.run_id)
    after = check_corrected(ctx)
    assert [r["verdict"] for r in after.result["receipts"]] == [r["verdict"] for r in before.result["receipts"]]
    assert dispatch("memo.release", {"check_run_id": after.run_id}, "web", ctx).status == "needs_approval"


def test_a_case_needs_a_reason_a_real_run_and_a_different_verdict(ctx):
    run = check_corrected(ctx)
    assert flag(ctx, run.run_id, reason="   ").status == "refused"
    assert flag(ctx, run.run_id, should_be="match").status == "refused"
    assert flag(ctx, run.run_id, index=999).status == "refused"
    assert flag(ctx, "run_0000000000000000").status == "refused"
    assert flag(ctx, run.run_id, should_be="invented").status == "refused"
    assert all_cases(ctx) == []


def test_the_same_flag_twice_is_one_case(ctx):
    run = check_corrected(ctx)
    assert flag(ctx, run.run_id).result["already_flagged"] is False
    assert flag(ctx, run.run_id).result["already_flagged"] is True
    assert len(all_cases(ctx)) == 1


def test_a_case_only_turns_fixed_when_the_replay_gives_what_the_person_said(ctx, monkeypatch):
    run = check_corrected(ctx)
    case_id = flag(ctx, run.run_id).result["case_id"]
    path = export_case(ctx, case_id)
    with pytest.raises(plugin.Refused, match="cannot be marked fixed"):
        mark_fixed(ctx, case_id, "abc123")
    assert json.loads(path.read_text())["fixed_in"] is None
    case = json.loads(path.read_text())
    monkeypatch.setitem(case, "should_be", "match")  # as if the code now gave what the person said
    path.write_text(json.dumps(case))
    assert replay_case(case, ctx)["status"] == "fixed"
    mark_fixed(ctx, case_id, "abc123")
    assert json.loads(path.read_text())["fixed_in"] == "abc123"


def test_a_case_goes_stale_rather_than_reading_a_missing_reply_as_a_verdict(ctx):
    run = check_corrected(ctx)
    flag(ctx, run.run_id)
    case = all_cases(ctx)[0]
    keys = list(case["replies"])
    stale = {**case, "replies": {k: case["replies"][k] for k in keys[:1]}, "should_be": "refused"}
    assert replay_case(stale, ctx)["status"] == "stale"
    assert replay_case({**case, "filing_sha256": "0" * 64}, ctx)["status"] == "stale"


def test_a_flag_on_an_edited_number_replays_from_saved_pointers(ctx):
    from finguard.core.context import corrected_memo
    memo = corrected_memo("anthropic").replace("16.3", "17.3", 1)
    run = dispatch("memo.check", {"memo": memo, "filing": "anthropic"}, "web", ctx)
    assert run.status == "done" and run.result["saved_pointers"], run.why
    i, r = next((i, r) for i, r in enumerate(run.result["receipts"]) if r["verdict"] == "wrong")
    out = flag(ctx, run.run_id, index=i, should_be="match", reason="I say it matches.")
    assert out.status == "done" and out.result["status"] == "open", (out.why, out.result)


def test_the_case_cap_holds(ctx, monkeypatch):
    run = check_corrected(ctx)
    monkeypatch.setattr(plugin, "MAX_CASES", 1)
    assert flag(ctx, run.run_id, index=0).status == "done"
    assert flag(ctx, run.run_id, index=1).status == "refused"


def test_the_terminal_lists_and_exports(ctx, monkeypatch, capsys):
    run = check_corrected(ctx)
    case_id = flag(ctx, run.run_id).result["case_id"]
    monkeypatch.setattr("finguard.cli._ctx", lambda: ctx)
    assert cli(["misses"]) == 0
    printed = capsys.readouterr().out
    assert case_id in printed and "open" in printed
    assert cli(["misses", "export", case_id]) == 0
    assert (plugin.COMMITTED / f"{case_id}.json").exists()
    assert cli(["misses", "fixed", case_id, "abc"]) == 1
