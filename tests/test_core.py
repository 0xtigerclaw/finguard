"""The door, the load-time rules, approvals and the ledger, with small made-up capabilities."""
import json
import threading
from dataclasses import replace

import pytest

from finguard.core.approvals import MAX_PENDING, CannotDecide, NotOperator
from finguard.core.context import Context
from finguard.core.door import dispatch
from finguard.core.plugin import Capability, Failed, LoadError, Refused, Registry, discover, hash_of, problem_with_params
from finguard.errors import MuseError

OPEN = {"type": "object", "additionalProperties": False, "properties": {"n": {"type": "integer", "minimum": 0}}}


def _read(**kw):
    base = dict(name="a.read", summary="s", kind="read", approval="none", verifies="v", params=OPEN, run=lambda c, p: {"ok": True})
    return Capability(**{**base, **kw})


def _act(**kw):
    base = dict(name="a.act", kind="act", approval="person", subject=lambda c, p: "s" * 64)
    return _read(**{**base, **kw})


@pytest.fixture
def ctx(tmp_path):
    c = Context.build(state=tmp_path / "state")
    c.registry = Registry()
    return c


# Load-time rules

@pytest.mark.parametrize("bad, words", [
    (_act(approval="none"), "must approve"),
    (_act(subject=None), "subject"),
    (_read(verifies=""), "verified"),
    (_read(verifies="   "), "verified"),
    (_read(name="noarea"), "area.verb"),
    (_read(name="A.Read"), "area.verb"),
    (_read(name="a.b.c"), "area.verb"),
    (_read(kind="write"), "kind"),
    (_read(approval="maybe"), "approval"),
    (_read(approval="person"), "subject"),
    (_read(summary=""), "summary"),
    (_read(params={"type": "string"}), "object"),
    (_read(params={"type": "object", "oneOf": []}), "oneOf"),
    (_read(params={"type": "object", "properties": {"x": {"format": "uri"}}}), "format"),
])
def test_each_load_rule_stops_the_load_in_plain_words(bad, words):
    with pytest.raises(LoadError, match=words):
        Registry().add(bad)


def test_a_duplicate_name_stops_the_load():
    registry = Registry()
    registry.add(_read())
    with pytest.raises(LoadError, match="twice"):
        registry.add(_read())


def _package(tmp_path, monkeypatch, name, **modules):
    root = tmp_path / name
    root.mkdir()
    (root / "__init__.py").write_text("")
    for module, code in modules.items():
        (root / f"{module}.py").write_text(code)
    monkeypatch.syspath_prepend(str(tmp_path))
    return name


GOOD = """
from finguard.core.plugin import Capability
def register(registry):
    registry.add(Capability(name="x.go", summary="s", kind="read", approval="none", verifies="v",
                            params={"type": "object"}, run=lambda c, p: {}))
"""


def test_discovery_loads_any_module_with_register_and_skips_helpers(tmp_path, monkeypatch):
    package = _package(tmp_path, monkeypatch, "plugs_good", dropped_in=GOOD, helper="VALUE = 1\n")
    registry = discover(package)
    assert list(registry.capabilities) == ["x.go"]


def test_discovery_of_the_real_plugins_finds_every_contract_capability():
    names = set(discover().capabilities)
    assert {"filings.list", "filings.page", "filings.add", "memo.check", "memo.release"} <= names


@pytest.mark.parametrize("code, words", [
    ("raise RuntimeError('boom')\n", "could not load"),
    ("def register(registry):\n    raise RuntimeError('boom')\n", "could not register"),
    ("from finguard.core.plugin import Capability\ndef register(registry):\n"
     "    registry.add(Capability('x.go', 's', 'act', 'none', 'v', {'type': 'object'}, lambda c, p: {}))\n", "must approve"),
])
def test_a_broken_plugin_stops_discovery(tmp_path, monkeypatch, code, words):
    package = _package(tmp_path, monkeypatch, f"plugs_{abs(hash(code))}", broken=code)
    with pytest.raises(LoadError, match=words):
        discover(package)


# The door

def _kinds(ctx):
    return [e["kind"] for e in ctx.ledger.lines()]


def test_an_unknown_capability_is_refused_and_on_the_ledger(ctx):
    out = dispatch("no.such", {}, "web", ctx)
    assert out.status == "refused" and out.why == "No such capability."
    assert _kinds(ctx) == ["requested", "refused"]


@pytest.mark.parametrize("params, words", [
    ({"n": "seven"}, "whole number"),
    ({"n": -1}, "or more"),
    ({"n": True}, "whole number"),
    ({"other": 1}, "unknown param"),
])
def test_bad_params_are_refused_and_nothing_runs(ctx, params, words):
    ran = []
    ctx.registry.add(_read(run=lambda c, p: ran.append(1) or {}))
    out = dispatch("a.read", params, "web", ctx)
    assert out.status == "refused" and words in out.why and not ran


def test_params_that_are_not_json_are_refused(ctx):
    ctx.registry.add(_read())
    assert dispatch("a.read", {"n": {1, 2}}, "web", ctx).status == "refused"


def test_the_schema_checker_covers_what_plugins_use():
    schema = {"type": "object", "required": ["a"], "properties": {
        "a": {"type": "string", "minLength": 2, "maxLength": 3, "pattern": "^x", "enum": ["xy", "xyz", "zz", "x", "xyzz"]},
        "b": {"type": "array", "items": {"type": "number", "maximum": 5}}, "c": {"type": "boolean"}}}
    assert problem_with_params(schema, {"a": "xy", "b": [1, 2.5], "c": False}) is None
    assert "missing" in problem_with_params(schema, {})
    assert "one of" in problem_with_params(schema, {"a": "qq"})
    assert "short" in problem_with_params(schema, {"a": "x"})
    assert "long" in problem_with_params(schema, {"a": "xyzz"})
    assert "right form" in problem_with_params(schema, {"a": "zz"})
    assert "or less" in problem_with_params(schema, {"a": "xy", "b": [9]})
    assert "true or false" in problem_with_params(schema, {"a": "xy", "c": 1})


def test_a_read_runs_straight_through(ctx):
    ctx.registry.add(_read(run=lambda c, p: {"n": p["n"], "run": c.run_id, "actor": c.actor}))
    out = dispatch("a.read", {"n": 3}, "web", ctx)
    assert out.status == "done" and out.result == {"n": 3, "run": out.run_id, "actor": "web"}
    assert _kinds(ctx) == ["requested", "done"]


def test_an_act_needs_approval_and_does_not_run(ctx):
    ran = []
    ctx.registry.add(_act(run=lambda c, p: ran.append(1) or {}))
    out = dispatch("a.act", {"n": 1}, "web", ctx)
    assert out.status == "needs_approval" and not ran and out.result is None
    a = out.approval
    assert a["status"] == "pending" and a["capability"] == "a.act" and a["params"] == {"n": 1}
    assert a["params_sha256"] == hash_of({"n": 1}) and a["subject_sha256"] == "s" * 64
    assert a["requested_by"] == "web" and a["decided_by"] is None
    assert set(a) == {"id", "capability", "params", "params_sha256", "subject_sha256", "status", "requested_by",
                      "decided_by", "reason", "created_at", "decided_at"}
    assert _kinds(ctx) == ["requested", "approval_created"]
    assert json.loads((ctx.state / "approvals" / f"{a['id']}.json").read_text())["status"] == "pending"


def _approved(ctx, params=None):
    request = dispatch("a.act", params or {"n": 1}, "web", ctx).approval
    ctx.approvals.decide(request["id"], "approve", "terminal", "fine")
    return request["id"]


def test_an_approved_act_runs_once_and_says_who_approved(ctx):
    seen = []
    ctx.registry.add(_act(run=lambda c, p: seen.append(c.approval["decided_by"]) or {"ok": True}))
    approval = _approved(ctx)
    out = dispatch("a.act", {"n": 1}, "web", ctx, approval_id=approval)
    assert out.status == "done" and seen == ["terminal"] and out.approval["status"] == "used"
    assert _kinds(ctx) == ["requested", "approval_created", "approval_decided", "requested", "approval_used", "done"]


def test_an_approval_cannot_be_used_twice(ctx):
    ran = []
    ctx.registry.add(_act(run=lambda c, p: ran.append(1) or {}))
    approval = _approved(ctx)
    assert dispatch("a.act", {"n": 1}, "web", ctx, approval_id=approval).status == "done"
    again = dispatch("a.act", {"n": 1}, "web", ctx, approval_id=approval)
    assert again.status == "refused" and "already been used" in again.why and len(ran) == 1


def test_two_racing_calls_cannot_both_use_one_approval(ctx):
    ran = []
    ctx.registry.add(_act(run=lambda c, p: ran.append(1) or {}))
    approval = _approved(ctx)
    gate, out = threading.Barrier(8), []

    def go():
        gate.wait()
        out.append(dispatch("a.act", {"n": 1}, "web", ctx, approval_id=approval).status)

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ran) == 1 and sorted(out) == ["done"] + ["refused"] * 7
    assert ctx.ledger.verify() == []


def test_an_approval_for_changed_content_is_refused(ctx):
    content = {"subject": "a" * 64}
    ran = []
    ctx.registry.add(_act(subject=lambda c, p: content["subject"], run=lambda c, p: ran.append(1) or {}))
    approval = _approved(ctx)
    content["subject"] = "b" * 64
    out = dispatch("a.act", {"n": 1}, "web", ctx, approval_id=approval)
    assert out.status == "refused" and "changed" in out.why and not ran
    assert ctx.approvals.get(approval)["status"] == "approved"


def test_an_approval_for_other_params_or_another_capability_is_refused(ctx):
    ran = []
    ctx.registry.add(_act(run=lambda c, p: ran.append(1) or {}))
    ctx.registry.add(_act(name="a.other", run=lambda c, p: ran.append(1) or {}))
    approval = _approved(ctx)
    assert "different params" in dispatch("a.act", {"n": 2}, "web", ctx, approval_id=approval).why
    assert "another capability" in dispatch("a.other", {"n": 1}, "web", ctx, approval_id=approval).why
    assert not ran


def test_pending_rejected_and_unknown_approvals_are_refused(ctx):
    ran = []
    ctx.registry.add(_act(run=lambda c, p: ran.append(1) or {}))
    pending = dispatch("a.act", {"n": 1}, "web", ctx).approval["id"]
    assert "not been approved" in dispatch("a.act", {"n": 1}, "web", ctx, approval_id=pending).why
    ctx.approvals.decide(pending, "reject", "terminal", "no")
    assert "rejected" in dispatch("a.act", {"n": 1}, "web", ctx, approval_id=pending).why
    assert dispatch("a.act", {"n": 1}, "web", ctx, approval_id="apr_0000000000000000").why == "No such approval."
    assert dispatch("a.act", {"n": 1}, "web", ctx, approval_id="../../etc/passwd").why == "No such approval."
    assert not ran


def test_a_subject_can_refuse_before_any_approval_exists(ctx):
    def no(c, p):
        raise Refused("Not this one.")
    ctx.registry.add(_act(subject=no))
    out = dispatch("a.act", {"n": 1}, "web", ctx)
    assert out.status == "refused" and out.why == "Not this one." and ctx.approvals.all() == []


@pytest.mark.parametrize("error, words", [
    (ValueError("/secret/path key=abc"), "internal error"),
    (Failed("The server answered 500."), "answered 500"),
    (MuseError("timeout", "raw provider text"), "too long"),
])
def test_an_exception_becomes_failed_with_a_plain_reason(ctx, error, words):
    def boom(c, p):
        raise error
    ctx.registry.add(_read(run=boom))
    out = dispatch("a.read", {}, "web", ctx)
    assert out.status == "failed" and words in out.why
    assert "Traceback" not in out.why and "secret" not in out.why and "raw provider" not in out.why
    assert _kinds(ctx) == ["requested", "failed"]


def test_a_capability_returning_the_wrong_thing_fails(ctx):
    ctx.registry.add(_read(run=lambda c, p: "not a dict"))
    assert dispatch("a.read", {}, "web", ctx).status == "failed"


def test_a_capability_can_refuse_while_running(ctx):
    def no(c, p):
        raise Refused("Nope.")
    ctx.registry.add(_read(run=no))
    out = dispatch("a.read", {}, "web", ctx)
    assert out.status == "refused" and out.why == "Nope."


# Who can decide

@pytest.mark.parametrize("who", ["model", "web", "muse", "", "memo.draft"])
def test_only_an_operator_can_decide(ctx, who):
    ctx.registry.add(_act())
    request = dispatch("a.act", {"n": 1}, "model", ctx).approval
    with pytest.raises(NotOperator):
        ctx.approvals.decide(request["id"], "approve", who, "because")
    assert ctx.approvals.get(request["id"])["status"] == "pending"


def test_no_capability_can_decide():
    assert not [n for n in discover().capabilities if n.startswith("approval") or "decide" in n or "approve" in n]


def test_a_decision_is_on_the_ledger_and_final(ctx):
    ctx.registry.add(_act())
    request = dispatch("a.act", {"n": 1}, "web", ctx).approval
    decided = ctx.approvals.decide(request["id"], "reject", "terminal", "too risky")
    assert decided["status"] == "rejected" and decided["decided_by"] == "terminal" and decided["decided_at"]
    line = ctx.ledger.lines()[-1]
    assert line["kind"] == "approval_decided" and line["decision"] == "reject" and line["reason"] == "too risky"
    with pytest.raises(CannotDecide, match="already rejected"):
        ctx.approvals.decide(request["id"], "approve", "terminal")


def test_a_rejection_needs_a_reason_and_a_decision_must_be_real(ctx):
    ctx.registry.add(_act())
    request = dispatch("a.act", {"n": 1}, "web", ctx).approval
    with pytest.raises(CannotDecide, match="why"):
        ctx.approvals.decide(request["id"], "reject", "terminal", "  ")
    with pytest.raises(CannotDecide, match="approve"):
        ctx.approvals.decide(request["id"], "maybe", "terminal")
    with pytest.raises(CannotDecide) as e:
        ctx.approvals.decide("apr_0000000000000000", "approve", "terminal")
    assert e.value.missing


def test_the_waiting_queue_cannot_grow_without_end(ctx, monkeypatch):
    monkeypatch.setattr("finguard.core.approvals.MAX_PENDING", 2)
    ctx.registry.add(_act())
    assert [dispatch("a.act", {"n": i}, "web", ctx).status for i in range(3)] == ["needs_approval", "needs_approval", "refused"]
    assert MAX_PENDING > 2


# The ledger

def _filled(ctx):
    ctx.registry.add(_read())
    for _ in range(3):
        dispatch("a.read", {}, "web", ctx)
    return ctx.ledger.path


def test_a_clean_ledger_holds(ctx):
    _filled(ctx)
    assert ctx.ledger.verify() == []
    lines = ctx.ledger.lines()
    assert [e["seq"] for e in lines] == list(range(6))
    assert all(e["prev"] == lines[i - 1]["hash"] for i, e in enumerate(lines) if i)
    assert ctx.ledger.lines(limit=2) == lines[-2:]


def test_a_changed_line_is_caught(ctx):
    path = _filled(ctx)
    path.write_text(path.read_text().replace('"actor": "web"', '"actor": "terminal"', 1))
    assert any("changed after" in p for p in ctx.ledger.verify())


def test_a_removed_line_is_caught(ctx):
    path = _filled(ctx)
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    assert ctx.ledger.verify()


def test_lines_cut_from_the_end_are_caught(ctx):
    path = _filled(ctx)
    path.write_text("\n".join(path.read_text().splitlines()[:-2]) + "\n")
    assert any("cut from the end" in p for p in ctx.ledger.verify())


def test_a_garbled_line_is_caught_and_not_a_crash(ctx):
    path = _filled(ctx)
    path.write_text(path.read_text() + "not json\n")
    assert ctx.ledger.verify()
    assert ctx.ledger.lines()[-1]["kind"] == "unreadable"


def test_a_run_folder_nobody_logged_is_caught(ctx):
    _filled(ctx)
    (ctx.ledger.runs_folder / "run_forged").mkdir(parents=True)
    assert any("not on the ledger" in p for p in ctx.ledger.verify())


def test_a_changed_saved_result_is_caught(ctx):
    _filled(ctx)
    outcome = next(ctx.ledger.runs_folder.glob("*/outcome.json"))
    outcome.write_text(json.dumps({"result": {"ok": False}}))
    assert any("saved result" in p for p in ctx.ledger.verify())


def test_many_writers_keep_the_chain_whole(ctx):
    threads = [threading.Thread(target=lambda: [ctx.ledger.append("note") for _ in range(10)]) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert ctx.ledger.verify() == [] and len(ctx.ledger.lines()) == 80


def test_a_second_process_view_continues_the_same_chain(ctx, tmp_path):
    ctx.ledger.append("note")
    other = Context.build(state=ctx.state)
    other.ledger.append("note")
    assert ctx.ledger.verify() == [] and len(ctx.ledger.lines()) == 2


def test_a_run_still_in_flight_is_not_called_broken(ctx):
    ctx.ledger.append("requested", run_id="run_flight", capability="memo.check", actor="web")
    folder = ctx.ledger.runs_folder / "run_flight"
    folder.mkdir(parents=True)
    (folder / "run.jsonl").write_text('{"half a li')
    assert ctx.ledger.verify() == []
    ctx.ledger.append("failed", run_id="run_flight", capability="memo.check", actor="web", why="x")
    assert ctx.ledger.verify()


def test_runs_read_back_from_the_ledger(ctx):
    ctx.registry.add(_read(run=lambda c, p: {"counts": {"match": 1}, "filing": "anthropic"}))
    ctx.registry.add(_act())
    done = dispatch("a.read", {}, "web", ctx)
    waiting = dispatch("a.act", {"n": 1}, "web", ctx)
    runs = ctx.ledger.runs()
    assert runs[done.run_id]["status"] == "done" and runs[done.run_id]["counts"] == {"match": 1}
    assert runs[done.run_id]["filing"] == "anthropic" and runs[waiting.run_id]["status"] == "needs_approval"


def test_context_stops_on_a_bad_live_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("FINGUARD_LIVE_LIMIT", "lots")
    with pytest.raises(LoadError, match="whole number"):
        Context.build(state=tmp_path)


def test_state_defaults_to_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("FINGUARD_STATE", str(tmp_path / "here"))
    assert Context.build().state == tmp_path / "here"
    assert replace(Context.build(), run_id="r").run_folder.name == "r"
