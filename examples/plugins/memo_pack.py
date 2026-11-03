"""A written workflow: check a memo, apply a named method, then ask for release. Copy into finguard/plugins/ and restart.

Every step goes through the door, so each one is on the ledger and keeps its own rules. This plugin only asks for the
release. A person still has to approve it, and nothing here can do that.
"""
from finguard.core.door import dispatch
from finguard.core.plugin import Capability, Refused, Registry


def _step(name, params, ctx):
    return dispatch(name, params, ctx.actor, ctx, caller=ctx.caller)


def _memo_pack(ctx, params):
    check = _step("memo.check", {"memo": params["memo"], "filing": params["filing"]}, ctx)
    if check.status != "done":
        raise Refused(f"The memo could not be checked, so the pack stopped. {check.why}")

    method = None
    if params.get("method_question"):  # a question under the house method, answered in code from the filing's cells
        applied = _step("rules.apply", {"filing": params["filing"], "question": params["method_question"]}, ctx)
        if applied.status != "done":
            raise Refused(f"The method could not be applied, so the pack stopped. {applied.why}")
        method = {"run_id": applied.run_id, "status": applied.result["status"], "rules_label": applied.result["rules_label"],
                  "answer": applied.result["answer"]}

    release = _step("memo.release", {"check_run_id": check.run_id}, ctx)
    return {
        "filing": params["filing"],
        "check_run_id": check.run_id,
        "counts": check.result["counts"],
        "method": method,
        "release": {"status": release.status, "why": release.why,
                    "approval_id": release.approval["id"] if release.approval else None},
    }


def register(registry: Registry) -> None:
    registry.add(Capability(
        name="desk.memo_pack",
        summary="Check a memo, apply a named method, and ask a person to approve its release.",
        kind="read",  # it never releases anything itself: memo.release stops at needs_approval
        approval="none",
        verifies="Each step runs through the door under its own checks. The release stops at needs_approval until a person decides.",
        params={"type": "object", "additionalProperties": False, "required": ["memo", "filing"], "properties": {
            "memo": {"type": "string", "maxLength": 4000},
            "filing": {"type": "string", "maxLength": 64},
            "method_question": {"type": "string", "maxLength": 500,
                                "description": "A question under the house method, such as: Under our method, what was the "
                                               "operating result for the six months ended June 30, 2026?"},
        }},
        run=_memo_pack,
    ))
