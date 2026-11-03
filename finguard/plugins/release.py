"""memo.release: seal a fully matched memo as a bundle anyone can check later."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path

from ..check import MATCH, REFUSED, UNCHECKED, WRONG
from ..core.context import Context
from ..core.ledger import chain_problems, now, record_problems
from ..core.plugin import Capability, Refused, Registry, hash_of
from .memos import place_receipts

_RELEASE_ID = re.compile(r"rel_[0-9a-f]{16}")
_CHAIN_FIELDS = {"schema", "seq", "prev", "kind", "hash"}
FILES = ("memo.txt", "receipts.json", "run.jsonl")


def _read_record(raw: bytes) -> tuple[str | None, list[dict], list[dict]]:
    """The memo, the receipts and all the lines, taken from a record's bytes."""
    lines = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    starts = [e for e in lines if e.get("kind") == "start"]
    memo = starts[0].get("memo") if len(starts) == 1 else None
    receipts = [{k: v for k, v in e.items() if k not in _CHAIN_FIELDS} for e in lines if e.get("kind") == "receipt"]
    return memo, receipts, lines


def _subject(memo: str, receipts: list[dict]) -> str:
    return hash_of({"memo": memo, "receipts": receipts})


def _releasable(ctx: Context, run_id: str) -> dict:
    """The memo and receipts of a check run, or a refusal. Reads only the hash chained record."""
    info = ctx.ledger.runs().get(run_id)  # only ids on the ledger reach the disk
    folder = ctx.ledger.runs_folder / run_id
    if info is None or not (folder / "run.jsonl").exists():
        raise Refused("No such check run.")
    if info["status"] != "done" or not info["last_hash"]:
        raise Refused("That run did not finish a check.")
    if record_problems(folder):
        raise Refused("The check run's record does not hold, so it cannot be released.")
    raw = (folder / "run.jsonl").read_bytes()
    try:
        memo, receipts, lines = _read_record(raw)
    except (ValueError, AttributeError):
        raise Refused("The check run's record does not hold, so it cannot be released.") from None
    if not lines or lines[-1].get("hash") != info["last_hash"] or not memo:
        raise Refused("The check run's record does not hold, so it cannot be released.")
    if not receipts:
        raise Refused("That check holds no numbers, so there is nothing to release.")
    counts = {v: sum(r.get("verdict") == v for r in receipts) for v in (MATCH, WRONG, REFUSED, UNCHECKED)}
    if counts[MATCH] != len(receipts):
        raise Refused(f"Only a memo where every number matches can be released. This one has "
                      f"{counts[WRONG]} wrong, {counts[REFUSED]} refused and {counts[UNCHECKED]} unchecked.")
    return {"memo": memo, "receipts": receipts, "raw": raw, "last_hash": info["last_hash"], "counts": counts,
            "filing": info["filing"], "filing_sha256": next((e.get("filing_sha256") for e in lines if e.get("kind") == "start"), None),
            "model": next((e.get("model") for e in lines if e.get("kind") == "start"), None)}


def _release_subject(ctx: Context, params: dict) -> str:
    found = _releasable(ctx, params["check_run_id"])
    return _subject(found["memo"], found["receipts"])


def _release(ctx: Context, params: dict) -> dict:
    found = _releasable(ctx, params["check_run_id"])
    subject = _subject(found["memo"], found["receipts"])
    if ctx.approval is None or ctx.approval["subject_sha256"] != subject:
        raise Refused("Releasing needs an approval for exactly this memo and these receipts.")
    release_id = f"rel_{secrets.token_hex(8)}"
    contents = {
        "memo.txt": found["memo"].encode("utf-8"),
        "receipts.json": json.dumps(found["receipts"], sort_keys=True, ensure_ascii=False, indent=1).encode("utf-8"),
        "run.jsonl": found["raw"],
    }
    manifest = {
        "release_id": release_id, "check_run_id": params["check_run_id"], "filing": found["filing"],
        "filing_sha256": found["filing_sha256"], "model": found["model"], "counts": found["counts"],
        "subject_sha256": subject, "run_last_hash": found["last_hash"], "approval_id": ctx.approval["id"],
        "approved_by": ctx.approval["decided_by"], "released_at": now(),
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=1).encode("utf-8")
    home = _home(ctx)
    spare = home / f".{release_id}"
    spare.mkdir(parents=True)
    for name, data in {**contents, "manifest.json": manifest_bytes}.items():
        (spare / name).write_bytes(data)
    os.replace(spare, home / release_id)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    ctx.ledger.append("release_sealed", run_id=ctx.run_id, release_id=release_id, manifest_sha256=manifest_sha256,
                      subject_sha256=subject, approval_id=ctx.approval["id"])
    return {"release_id": release_id, "check_run_id": params["check_run_id"], "manifest": manifest,
            "manifest_sha256": manifest_sha256, "filing": found["filing"], "counts": found["counts"],
            "holds": not verify_release(ctx, release_id)}


def _home(ctx: Context) -> Path:
    return Path(ctx.state) / "releases"


def bundle_exists(ctx: Context, release_id: str) -> bool:
    return bool(_RELEASE_ID.fullmatch(release_id)) and (_home(ctx) / release_id).is_dir()


def verify_release(ctx: Context, release_id: str) -> list[str]:
    """Everything wrong with a bundle. Empty means every file, the chain and the ledger agree."""
    if not bundle_exists(ctx, release_id):
        return ["No such release."]
    folder = _home(ctx) / release_id
    try:
        raw_manifest = (folder / "manifest.json").read_bytes()
        manifest = json.loads(raw_manifest)
        declared = manifest["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return ["manifest.json is missing or cannot be read"]
    problems = []
    if manifest.get("release_id") != release_id:
        problems.append("the manifest names another release")
    if set(declared) != set(FILES):
        problems.append("the manifest does not list the right files")
    if {p.name for p in folder.iterdir()} != {*FILES, "manifest.json"}:
        problems.append("the bundle holds the wrong set of files")
    for name in FILES:
        try:
            if hashlib.sha256((folder / name).read_bytes()).hexdigest() != declared.get(name):
                problems.append(f"{name} was changed")
        except OSError:
            problems.append(f"{name} is missing")
    try:
        memo, receipts, lines = _read_record((folder / "run.jsonl").read_bytes())
        problems += [f"run.jsonl {p}" for p in chain_problems(lines)]
        if lines and lines[-1].get("hash") != manifest.get("run_last_hash"):
            problems.append("run.jsonl does not end where the manifest says")
        if memo != (folder / "memo.txt").read_text(encoding="utf-8"):
            problems.append("memo.txt is not the memo that was checked")
        if receipts != json.loads((folder / "receipts.json").read_text(encoding="utf-8")):
            problems.append("receipts.json is not what the check found")
        if not receipts or any(r.get("verdict") != MATCH for r in receipts):
            problems.append("not every number in this bundle matches")
        if memo is None or _subject(memo, receipts) != manifest.get("subject_sha256"):
            problems.append("the memo and receipts are not what was approved")
    except (OSError, ValueError, AttributeError):
        problems.append("the bundle's files cannot be read")
    sealed = [e for e in ctx.ledger.lines() if e.get("kind") == "release_sealed" and e.get("release_id") == release_id]
    if not sealed:
        problems.append("the ledger has no record of this release")
    elif sealed[-1].get("manifest_sha256") != hashlib.sha256(raw_manifest).hexdigest():
        problems.append("manifest.json was changed after it was sealed")
    return problems


def read_bundle(ctx: Context, release_id: str) -> dict:
    """The bundle's contents as best they can be read, with whether it holds."""
    folder = _home(ctx) / release_id

    def load(name: str, parse):
        try:
            return parse((folder / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    memo, receipts = load("memo.txt", str), load("receipts.json", json.loads)
    try:
        receipts = place_receipts(memo, receipts)
    except (KeyError, TypeError, AttributeError):
        pass  # a damaged bundle is still shown, and `holds` says so
    return {"manifest": load("manifest.json", json.loads), "memo": memo, "receipts": receipts,
            "holds": not verify_release(ctx, release_id)}


def register(registry: Registry) -> None:
    registry.add(Capability(
        name="memo.release",
        summary="Release a checked memo as a sealed bundle, once a person approves.",
        kind="act",
        approval="person",
        verifies="Code refuses unless the run's record holds and every receipt is match, then hashes every file in the bundle.",
        params={"type": "object", "additionalProperties": False, "required": ["check_run_id"], "properties": {
            "check_run_id": {"type": "string", "maxLength": 64, "description": "The run_id of a finished check."},
        }},
        run=_release,
        subject=_release_subject,
    ))
