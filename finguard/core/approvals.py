"""Approval requests, bound to exact content. Only an operator can decide one."""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
from contextlib import contextmanager
from pathlib import Path

from .ledger import Ledger, now
from .plugin import Refused, hash_of

OPERATORS = frozenset({"terminal", "web_operator"})
DEMO_REVIEWER = "demo_reviewer"  # not an operator: it can only approve a release of a saved memo, or install a saved example
MAX_PENDING = 100  # anyone on the web can ask, so the queue must not grow without end
MAX_REASON = 500
_ID = re.compile(r"apr_[0-9a-f]{16}")


class NotOperator(Exception):
    """Someone who is not an operator tried to decide."""


class CannotDecide(Exception):
    """This decision cannot be made. `missing` says the request does not exist at all."""

    def __init__(self, why: str, missing: bool = False) -> None:
        super().__init__(why)
        self.missing = missing


class Approvals:
    def __init__(self, state: Path, ledger: Ledger) -> None:
        self.folder = Path(state) / "approvals"
        self.ledger = ledger

    @contextmanager
    def _locked(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        with (self.folder / ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _save(self, request: dict) -> None:
        spare = self.folder / f".{request['id']}.tmp"
        spare.write_text(json.dumps(request, sort_keys=True, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(spare, self.folder / f"{request['id']}.json")

    def get(self, approval_id: str) -> dict | None:
        if not isinstance(approval_id, str) or not _ID.fullmatch(approval_id):
            return None
        try:
            return json.loads((self.folder / f"{approval_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def all(self) -> list[dict]:
        """Every request, newest first."""
        if not self.folder.exists():
            return []
        found = [self.get(p.stem) for p in self.folder.glob("apr_*.json")]
        return sorted((r for r in found if r), key=lambda r: (r["created_at"], r["id"]), reverse=True)

    def create(self, capability: str, params: dict, params_sha256: str, subject_sha256: str,
               requested_by: str) -> dict:
        with self._locked():
            if sum(r["status"] == "pending" for r in self.all()) >= MAX_PENDING:
                raise Refused("Too many requests are waiting for a decision. Try again later.")
            request = {
                "id": f"apr_{secrets.token_hex(8)}", "capability": capability, "params": params,
                "params_sha256": params_sha256, "subject_sha256": subject_sha256, "status": "pending",
                "requested_by": requested_by, "decided_by": None, "reason": None,
                "created_at": now(), "decided_at": None,
            }
            self._save(request)
            return request

    def _kept_result(self, run_id: object, capabilities: tuple[str, ...]) -> dict | None:
        """A finished run's kept result, if it is of these capabilities and still hashes to what the ledger holds."""
        info = self.ledger.runs().get(run_id) if isinstance(run_id, str) else None  # only ids on the ledger reach the disk
        if info is None or info["status"] != "done" or info["capability"] not in capabilities:
            return None
        try:
            result = json.loads((self.ledger.runs_folder / run_id / "outcome.json").read_text(encoding="utf-8"))["result"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return result if hash_of(result) == info["result_sha256"] else None

    def _checked_from_saved(self, run_id: object) -> bool:
        """True when this run is a finished check whose kept result, as hashed on the ledger, says saved or saved pointers."""
        result = self._kept_result(run_id, ("memo.check", "memo.draft"))
        return result is not None and (result.get("saved") is True or result.get("saved_pointers") is True)

    def drafted_from_saved(self, definition_sha256: str) -> bool:
        """True when a finished plugins.create run replayed exactly this definition from a recorded gallery prompt."""
        for entry in self.ledger.lines():
            if entry.get("kind") == "plugin_drafted" and entry.get("definition_sha256") == definition_sha256:
                result = self._kept_result(entry.get("run_id"), ("plugins.create",))
                if result is not None and result.get("saved") is True and result.get("definition_sha256") == definition_sha256:
                    return True
        return False

    def _demo_problem(self, request: dict) -> str | None:
        if request["capability"] == "plugins.install":
            if not self.drafted_from_saved(request["subject_sha256"]):
                return "This plugin did not come from a saved example, so installing it needs an operator token."
            return None
        if request["capability"] != "memo.release":
            return "The demo reviewer can only release a saved memo or install a saved example. This request needs an operator token."
        if not self._checked_from_saved(request["params"].get("check_run_id")):
            return "This memo was checked live, so releasing it needs an operator token."
        return None

    def decide(self, approval_id: str, decision: str, by: str, reason: str = "") -> dict:
        """Approve or reject a pending request. Only an operator, or the demo reviewer within its limits, gets past here."""
        if by not in OPERATORS and by != DEMO_REVIEWER:
            raise NotOperator("Only an operator can decide.")
        if by == DEMO_REVIEWER and decision != "approve":
            raise NotOperator("The demo reviewer can only approve. Rejecting needs an operator token.")
        if decision not in ("approve", "reject"):
            raise CannotDecide('The decision must be "approve" or "reject".')
        reason = (reason or "").encode("utf-8", "replace").decode().strip()
        if decision == "reject" and not reason:
            raise CannotDecide("Say why it was rejected.")
        if len(reason) > MAX_REASON:
            raise CannotDecide(f"Keep the reason under {MAX_REASON} characters.")
        with self._locked():
            request = self.get(approval_id)
            if request is None:
                raise CannotDecide("No such approval.", missing=True)
            problem = self._demo_problem(request) if by == DEMO_REVIEWER else None
            if problem:
                raise NotOperator(problem)
            if request["status"] != "pending":
                raise CannotDecide(f"This request is already {request['status']}.")
            request.update(status="approved" if decision == "approve" else "rejected",
                           decided_by=by, reason=reason, decided_at=now())
            self._save(request)
        self.ledger.append("approval_decided", approval_id=request["id"], capability=request["capability"],
                           decision=decision, decided_by=by, reason=reason,
                           params_sha256=request["params_sha256"], subject_sha256=request["subject_sha256"])
        return request

    def use(self, approval_id: str) -> dict | None:
        """Mark an approved request used, once. Returns None if someone else got there first."""
        with self._locked():
            request = self.get(approval_id)
            if request is None or request["status"] != "approved":
                return None
            request["status"] = "used"
            self._save(request)
            return request
