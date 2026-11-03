"""memo.flag: a person says a verdict is wrong, and it becomes a case the score replays.

A case changes nothing by itself. No verdict reads it. It holds the memo, what the person said is right, and the
replies Muse gave, so it can be replayed with no API. It stays open until a change to the code makes the replay give
what the person said. A case flagged on a server lives in that server's state folder until it is exported to
eval/misses/, where the gate replays it.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

from ..check import MATCH, REFUSED, WRONG, check_memo
from ..core.context import ROOT, Context, saved_examples
from ..core.ledger import now, record_problems
from ..core.plugin import Capability, Refused, Registry
from ..errors import MuseError
from ..muse import Muse
from ..pointers import PointerMuse, find_saved_pointers
from ..record import _DATA_URL, Run, read_lines, sha256
from .memos import place_receipts
from .release import _read_record

MAX_CASES = 200
MAX_REASON = 500
MAX_RIGHT_VALUE = 64
COMMITTED = ROOT / "eval" / "misses"
_PARKED = re.compile(r"image:[0-9a-f]{64}")
_CASE_ID = re.compile(r"mis_[0-9a-f]{16}")


def _key(request: str) -> str:
    """A request with its page images taken out. A replay on another system renders pages a little differently."""
    return sha256(_PARKED.sub("image", request).encode())


class CaseTransport:
    """Answers only from the case's own replies, and remembers if it was asked something new."""

    def __init__(self, replies: dict[str, str]) -> None:
        self.replies, self.missed = replies, 0

    def send(self, body: bytes) -> bytes:
        key = sha256(_DATA_URL.sub("image", body.decode("utf-8")).encode())
        if key not in self.replies:
            self.missed += 1
            raise MuseError("no_recording", "this request is not in the case")
        return self.replies[key].encode("utf-8")


def replay_case(case: dict, ctx: Context) -> dict:
    """What the code says today about the flagged number: open, fixed, or stale if it cannot be replayed."""
    def stale(why: str) -> dict:
        return {"status": "stale", "verdict": None, "why": why}

    try:
        filing = ctx.filings.get(case["filing"])
    except Refused:
        return stale("The filing is not on this server.")
    if filing.sha256 != case["filing_sha256"]:
        return stale("The filing has changed.")
    transport = CaseTransport(case["replies"])
    with tempfile.TemporaryDirectory(prefix="finguard-case-") as folder:
        run = Run(Path(folder))
        muse = Muse(transport, run)
        if case["saved_pointers"]:
            pointers = find_saved_pointers(case["memo"], saved_examples(case["filing"]))
            if pointers is None:
                return stale("The saved example this memo was built from has changed.")
            muse = PointerMuse(transport, run, pointers)
        try:
            receipts = place_receipts(case["memo"], [r.to_dict() for r in check_memo(case["memo"], filing, muse, run)])
        except MuseError as e:
            return stale(f"The code now asks Muse something the case did not save. {e}")
    if transport.missed:  # a pointing call that failed reads as a refusal, which would look like a real verdict
        return stale("The code now asks Muse something the case did not save.")
    index = case["index"]
    same = index < len(receipts) and receipts[index]["value_text"] == case["value_text"]
    if not same:
        return stale("The receipts no longer line up with this number.")
    verdict = receipts[index]["verdict"]
    return {"status": "fixed" if verdict == case["should_be"] else "open", "verdict": verdict, "why": receipts[index]["why"]}


def _folder(ctx: Context) -> Path:
    return Path(ctx.state) / "misses"


def _read(path: Path) -> dict | None:
    try:
        case = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return case if isinstance(case, dict) and _CASE_ID.fullmatch(str(case.get("id"))) else None


def _cases_in(folder: Path) -> list[dict]:
    return [c for c in (_read(p) for p in sorted(folder.glob("mis_*.json"))) if c] if folder.exists() else []


def committed_cases() -> list[dict]:
    return _cases_in(COMMITTED)


def all_cases(ctx: Context) -> list[dict]:
    """Committed cases first. A case exported from this server is shown once."""
    found = {c["id"]: c for c in _cases_in(_folder(ctx))}
    found.update({c["id"]: {**c, "committed": True} for c in committed_cases()})
    return sorted(found.values(), key=lambda c: c.get("flagged_at", ""), reverse=True)


def public_case(case: dict) -> dict:
    return {k: v for k, v in case.items() if k not in ("replies", "memo")}


def _flagged_receipts(ctx: Context, run_id: str) -> tuple[Path, str, list[dict], list[dict]]:
    info = ctx.ledger.runs().get(run_id)  # only ids on the ledger reach the disk
    folder = ctx.ledger.runs_folder / run_id
    if info is None or info["capability"] not in ("memo.check", "memo.draft") or not (folder / "run.jsonl").exists():
        raise Refused("No such check run.")
    if info["status"] != "done" or not info["last_hash"] or record_problems(folder):
        raise Refused("That run's record does not hold, so it cannot become a case.")
    memo, receipts, lines = _read_record((folder / "run.jsonl").read_bytes())
    if not memo or not lines or lines[-1].get("hash") != info["last_hash"]:
        raise Refused("That run's record does not hold, so it cannot become a case.")
    return folder, memo, place_receipts(memo, receipts), lines


def _flag(ctx: Context, params: dict) -> dict:
    reason = " ".join(params["reason"].split())
    if not reason:
        raise Refused("Say why the verdict is wrong.")
    folder, memo, receipts, lines = _flagged_receipts(ctx, params["check_run_id"])
    index = params["index"]
    if not 0 <= index < len(receipts):
        raise Refused("That run has no such number.")
    receipt = receipts[index]
    if params["should_be"] == receipt["verdict"]:
        raise Refused(f"That is already the verdict: {receipt['verdict']}.")
    start = next(e for e in lines if e.get("kind") == "start")
    identity = json.dumps([memo, start["filing_sha256"], index, params["should_be"]], ensure_ascii=False)
    case_id = "mis_" + hashlib.sha256(identity.encode()).hexdigest()[:16]
    path = _folder(ctx) / f"{case_id}.json"
    if path.exists():
        return {"case_id": case_id, "status": _read(path).get("status_when_flagged"), "already_flagged": True}
    if len(list(_folder(ctx).glob("mis_*.json"))) >= MAX_CASES:
        raise Refused("Case storage is full. No new cases are being taken.")
    filing = ctx.ledger.runs()[params["check_run_id"]].get("filing")
    case = {
        "schema": 1, "id": case_id, "flagged_at": now(), "flagged_by": ctx.actor, "flag_run_id": ctx.run_id,
        "check_run_id": params["check_run_id"], "filing": filing, "filing_sha256": start["filing_sha256"], "memo": memo,
        "saved_pointers": any(e.get("kind") == "saved_pointers" for e in lines),
        "index": index, "value_text": receipt["value_text"], "metric": receipt.get("metric", ""), "verdict": receipt["verdict"],
        "why": receipt.get("why", ""), "page": receipt.get("page"), "row": receipt.get("row", ""), "column": receipt.get("column", ""),
        "should_be": params["should_be"], "right_value": " ".join(params.get("right_value", "").split()), "reason": reason,
        "replies": {_key(e["request"]): e["reply"] for e in lines if e.get("kind") == "muse_call" and e.get("reply") is not None},
        "fixed_in": None,
    }
    replayed = replay_case(case, ctx)
    case["status_when_flagged"] = replayed["status"]
    _folder(ctx).mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    ctx.ledger.append("miss_flagged", run_id=ctx.run_id, case_id=case_id, check_run_id=params["check_run_id"],
                      case_sha256=sha256(path.read_bytes()))
    return {"case_id": case_id, "status": replayed["status"], "already_flagged": False}


def export_case(ctx: Context, case_id: str) -> Path:
    """Copy a case into the regression set, where the gate replays it."""
    path = _folder(ctx) / f"{case_id}.json"
    if not _CASE_ID.fullmatch(case_id) or not path.exists():
        raise Refused("No such case on this server.")
    COMMITTED.mkdir(parents=True, exist_ok=True)
    (COMMITTED / path.name).write_bytes(path.read_bytes())
    return COMMITTED / path.name


def mark_fixed(ctx: Context, case_id: str, commit: str) -> Path:
    """Only a replay can say a case is fixed. Once marked, the gate fails if it comes back."""
    path = COMMITTED / f"{case_id}.json"
    case = _read(path) if _CASE_ID.fullmatch(case_id) else None
    if case is None:
        raise Refused("No such case in the regression set.")
    replayed = replay_case(case, ctx)
    if replayed["status"] != "fixed":
        raise Refused(f"The replay says {replayed['status']}, so it cannot be marked fixed. {replayed['why']}")
    case["fixed_in"] = commit
    path.write_text(json.dumps(case, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def register(registry: Registry) -> None:
    registry.add(Capability(
        name="memo.flag",
        summary="A person says a verdict is wrong. It becomes a case the score replays, and changes no verdict.",
        kind="read",  # no verdict, release or approval reads a case
        approval="none",
        verifies="The case comes from the run's hash chained record and replays with no API. It stays open until the replay gives what the person said.",
        params={"type": "object", "additionalProperties": False, "required": ["check_run_id", "index", "should_be", "reason"], "properties": {
            "check_run_id": {"type": "string", "maxLength": 64},
            "index": {"type": "integer", "minimum": 0, "maximum": 1000},
            "should_be": {"type": "string", "enum": [MATCH, WRONG, REFUSED]},
            "right_value": {"type": "string", "maxLength": MAX_RIGHT_VALUE},
            "reason": {"type": "string", "maxLength": MAX_REASON},
        }},
        run=_flag,
    ))
