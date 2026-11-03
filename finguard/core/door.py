"""The one door. The CLI and the web page both come through dispatch(), so they cannot differ."""
from __future__ import annotations

import json
import logging
import secrets
from dataclasses import asdict, dataclass, replace

from ..errors import MuseError, plainly
from .context import Context
from .plugin import Capability, Failed, Refused, hash_of, problem_with_params

log = logging.getLogger("finguard.door")
MAX_NAME = 64
MAX_KEPT_RESULT = 256 * 1024  # a page image is returned to the caller but not stored with the run


@dataclass
class Outcome:
    status: str  # done, needs_approval, refused or failed
    run_id: str
    result: dict | None = None
    approval: dict | None = None
    why: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _plain_failure(name: str, error: Exception) -> str:
    """A reason safe to show. Raw messages can hold URLs or provider text, so they stay out."""
    if isinstance(error, Failed):
        return str(error)
    if isinstance(error, MuseError):
        return f"Muse could not answer: {plainly(error)}."
    log.error("%s stopped on %s", name, type(error).__name__)
    return "This capability stopped on an internal error."


def _approval_problem(request: dict | None, cap: Capability, params_sha256: str) -> str | None:
    if request is None:
        return "No such approval."
    if request["capability"] != cap.name:
        return "This approval is for another capability."
    if request["status"] != "approved":
        return {"pending": "This request has not been approved yet.",
                "rejected": "This request was rejected.",
                "used": "This approval has already been used."}.get(request["status"], "This approval is not valid.")
    if request["params_sha256"] != params_sha256:
        return "This approval is for different params."
    return None


def _keep_result(ctx: Context, result: dict) -> None:
    text = json.dumps({"result": result}, sort_keys=True, ensure_ascii=False)
    if len(text) <= MAX_KEPT_RESULT:
        ctx.run_folder.mkdir(parents=True, exist_ok=True)
        (ctx.run_folder / "outcome.json").write_text(text, encoding="utf-8")


def dispatch(name: str, params: dict | None, actor: str, ctx: Context, approval_id: str | None = None) -> Outcome:
    """Refuse, ask for approval, or run. Every step goes on the ledger."""
    run_id = f"run_{secrets.token_hex(8)}"
    name = str(name)[:MAX_NAME]
    params = {} if params is None else params
    try:
        params_sha256 = hash_of(params)
    except (TypeError, ValueError):
        params_sha256 = ""
    ctx.ledger.append("requested", run_id=run_id, capability=name, actor=actor, params_sha256=params_sha256,
                      approval_id=approval_id)

    def refuse(why: str, approval: dict | None = None) -> Outcome:
        ctx.ledger.append("refused", run_id=run_id, capability=name, actor=actor, why=why)
        return Outcome("refused", run_id, approval=approval, why=why)

    def fail(error: Exception) -> Outcome:
        why = _plain_failure(name, error)
        ctx.ledger.append("failed", run_id=run_id, capability=name, actor=actor, why=why)
        return Outcome("failed", run_id, why=why)

    cap = ctx.registry.get(name)
    if cap is None:
        return refuse("No such capability.")
    if not params_sha256:
        return refuse("The params are not plain JSON.")
    problem = problem_with_params(cap.params, params)
    if problem:
        return refuse(problem)

    call = replace(ctx, run_id=run_id, actor=actor, approval=None)
    if cap.approval == "person":
        try:
            subject_now = cap.subject(call, params)
        except Refused as e:
            return refuse(str(e))
        except Exception as e:
            return fail(e)
        if approval_id is None:
            try:
                request = ctx.approvals.create(name, params, params_sha256, subject_now, actor)
            except Refused as e:
                return refuse(str(e))
            ctx.ledger.append("approval_created", run_id=run_id, capability=name, actor=actor,
                              approval_id=request["id"], params_sha256=params_sha256, subject_sha256=subject_now)
            return Outcome("needs_approval", run_id, approval=request)
        request = ctx.approvals.get(approval_id)
        problem = _approval_problem(request, cap, params_sha256)
        if problem:
            return refuse(problem, request)
        if request["subject_sha256"] != subject_now:
            return refuse("The content changed after it was approved. Ask again.", request)
        used = ctx.approvals.use(approval_id)
        if used is None:
            return refuse("This approval has already been used.", ctx.approvals.get(approval_id))
        ctx.ledger.append("approval_used", run_id=run_id, capability=name, actor=actor, approval_id=approval_id,
                          subject_sha256=subject_now)
        call.approval = used

    try:
        result = cap.run(call, params)
        if not isinstance(result, dict):
            raise TypeError("a capability must return a dict")
        result_sha256 = hash_of(result)
    except Refused as e:
        return refuse(str(e))
    except Exception as e:
        return fail(e)

    fields = {"result_sha256": result_sha256}
    if isinstance(result.get("record"), dict) and result["record"].get("last_hash"):
        fields["run_last_hash"] = result["record"]["last_hash"]
    for key in ("counts", "filing"):
        if key in result:
            fields[key] = result[key]
    _keep_result(call, result)
    ctx.ledger.append("done", run_id=run_id, capability=name, actor=actor, **fields)
    return Outcome("done", run_id, result=result, approval=call.approval)
