"""The plugin the README shows must really load, run and obey the load-time rules."""
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from finguard.core.context import Context, corrected_memo, saved_memo
from finguard.core.door import dispatch
from finguard.core.plugin import Capability, LoadError, Registry

EXAMPLE = Path(__file__).parent.parent / "examples" / "plugins" / "net_debt.py"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    package = tmp_path / "myplugins"
    package.mkdir()
    (package / "__init__.py").write_text("")
    shutil.copy(EXAMPLE, package / "net_debt.py")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield Context.build(tmp_path / "state", package="myplugins")
    sys.modules.pop("myplugins", None)
    sys.modules.pop("myplugins.net_debt", None)


def test_dropped_in_plugin_loads_and_works_out_net_debt(ctx):
    out = dispatch("desk.net_debt", {"filing": "nebius", "page": 5, "as_of": "June 30, 2026"}, "test", ctx)
    assert out.status == "done" and out.result["net_debt_in_millions"] == "503.6"
    assert [c["printed"] for c in out.result["read"]] == ["46.7", "8,499.0", "8,042.1"]


def test_it_refuses_rather_than_guesses(ctx):
    out = dispatch("desk.net_debt", {"filing": "nebius", "page": 6, "as_of": "June 30, 2026"}, "test", ctx)
    assert out.status == "refused" and "nothing was worked out" in out.why


def test_a_plugin_that_acts_with_no_approval_cannot_load():
    with pytest.raises(LoadError, match="must approve"):
        Registry().add(Capability(name="desk.wire", summary="s", kind="act", approval="none", verifies="v",
                                  params={"type": "object"}, run=lambda c, p: {}))


# The written workflow

PACK = Path(__file__).parent.parent / "examples" / "plugins" / "memo_pack.py"
METHOD = "Under our method, what was the operating result for the six months ended June 30, 2026?"


@pytest.fixture
def pack_ctx(tmp_path, monkeypatch):
    """The real plugins, plus the example, as if it had been copied into finguard/plugins/."""
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    spec = importlib.util.spec_from_file_location("memo_pack_example", PACK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ctx = Context.build(tmp_path / "state")
    module.register(ctx.registry)
    return ctx


def test_the_workflow_loads_under_the_load_time_rules(tmp_path, monkeypatch):
    package = tmp_path / "packplugins"
    package.mkdir()
    (package / "__init__.py").write_text("")
    shutil.copy(PACK, package / "memo_pack.py")
    monkeypatch.syspath_prepend(str(tmp_path))
    ctx = Context.build(tmp_path / "state", package="packplugins")
    cap = ctx.registry.get("desk.memo_pack")
    assert cap.kind == "read" and cap.approval == "none" and cap.verifies
    sys.modules.pop("packplugins", None)
    sys.modules.pop("packplugins.memo_pack", None)


def test_the_workflow_checks_applies_the_method_and_stops_at_needs_approval(pack_ctx):
    params = {"memo": corrected_memo("anthropic"), "filing": "anthropic", "method_question": METHOD}
    out = dispatch("desk.memo_pack", params, "web", pack_ctx)
    assert out.status == "done", out.why
    result = out.result
    assert result["counts"]["match"] > 0 and result["counts"]["wrong"] == 0
    assert result["method"]["status"] == "differs" and result["method"]["rules_label"] == "ILLUSTRATIVE"
    assert result["release"]["status"] == "needs_approval" and result["release"]["approval_id"]
    approval = pack_ctx.approvals.get(result["release"]["approval_id"])
    assert approval["capability"] == "memo.release" and approval["status"] == "pending" and approval["decided_by"] is None
    assert not list((pack_ctx.state / "releases").glob("rel_*")) if (pack_ctx.state / "releases").exists() else True
    kinds = [e["kind"] for e in pack_ctx.ledger.lines()]
    assert "approval_created" in kinds and "release_sealed" not in kinds
    assert pack_ctx.ledger.verify() == []


def test_the_workflow_cannot_release_until_a_person_approves(pack_ctx):
    out = dispatch("desk.memo_pack", {"memo": corrected_memo("anthropic"), "filing": "anthropic"}, "web", pack_ctx)
    approval_id, run_id = out.result["release"]["approval_id"], out.result["check_run_id"]
    early = dispatch("memo.release", {"check_run_id": run_id}, "web", pack_ctx, approval_id=approval_id)
    assert early.status == "refused" and "not been approved" in early.why
    pack_ctx.approvals.decide(approval_id, "approve", "terminal", "Looks right.")
    done = dispatch("memo.release", {"check_run_id": run_id}, "web", pack_ctx, approval_id=approval_id)
    assert done.status == "done" and done.result["release_id"]


def test_the_workflow_stops_at_a_memo_with_a_wrong_number(pack_ctx):
    out = dispatch("desk.memo_pack", {"memo": saved_memo("anthropic"), "filing": "anthropic"}, "web", pack_ctx)
    assert out.status == "done" and out.result["release"]["status"] == "refused"
    assert out.result["release"]["approval_id"] is None and "Only a memo where every number matches" in out.result["release"]["why"]
