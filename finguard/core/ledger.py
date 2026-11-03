"""The workspace ledger: append only and hash chained, by the same rule as a run's record.

The chain catches a changed or removed line. A separate head file catches lines cut from
the end. Someone who can rewrite every file in the state folder can still forge it all.
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..record import GENESIS, SCHEMA_VERSION, _line_hash, read_lines, verify as verify_record
from .plugin import hash_of

_ENDINGS = {"done": "done", "refused": "refused", "failed": "failed", "approval_created": "needs_approval"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def chain_problems(entries: list[dict]) -> list[str]:
    """Everything wrong with a hash chain, looking at the lines alone."""
    problems, prev = [], GENESIS
    for i, entry in enumerate(entries):
        body = {k: v for k, v in entry.items() if k != "hash"}
        if entry.get("seq") != i:
            problems.append(f"line {i}: out of sequence")
        if entry.get("prev") != prev:
            problems.append(f"line {i}: does not follow the line before it")
        if _line_hash(body) != entry.get("hash"):
            problems.append(f"line {i}: changed after it was written")
        prev = entry.get("hash")
    return problems


def record_problems(folder: Path, deep: bool = True) -> list[str]:
    """What is wrong with a run's record. Deep also rebuilds every request from its page images."""
    try:
        if deep:
            return verify_record(folder)
        return chain_problems(read_lines(Path(folder) / "run.jsonl"))
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        return ["the record cannot be read"]


class Ledger:
    def __init__(self, state: Path) -> None:
        self.state = Path(state)
        self.path = self.state / "ledger.jsonl"
        self.head = self.state / "ledger.head"
        self.runs_folder = self.state / "runs"

    @contextmanager
    def _locked(self):
        """One writer at a time, across threads and across the CLI and web processes."""
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / "ledger.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        entries = []
        for i, raw in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                entry = None
            entries.append(entry if isinstance(entry, dict) else {"kind": "unreadable", "seq": i})
        return entries

    def _tail(self) -> tuple[int, str]:
        try:
            head = json.loads(self.head.read_text())
            return head["count"], head["last"]
        except (OSError, ValueError, KeyError, TypeError):
            entries = self._read()
            return len(entries), entries[-1].get("hash", "unreadable") if entries else GENESIS

    def append(self, kind: str, **fields) -> dict:
        with self._locked():
            count, last = self._tail()
            entry = {"schema": SCHEMA_VERSION, "seq": count, "prev": last, "kind": kind, "at": now(), **fields}
            entry["hash"] = _line_hash(entry)
            with self.path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
                out.flush()
                os.fsync(out.fileno())
            spare = self.head.with_suffix(".tmp")
            spare.write_text(json.dumps({"count": count + 1, "last": entry["hash"]}))
            os.replace(spare, self.head)
            return entry

    def lines(self, limit: int | None = None) -> list[dict]:
        with self._locked():
            entries = self._read()
        return entries[-limit:] if limit else entries

    def runs(self) -> dict[str, dict]:
        """Each run the ledger knows, oldest first, as its lines left it."""
        return self._runs_from(self.lines())

    @staticmethod
    def _runs_from(entries: list[dict]) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for entry in entries:
            run_id = entry.get("run_id")
            if not run_id or not isinstance(run_id, str):
                continue
            if entry.get("kind") == "requested":
                found[run_id] = {"run_id": run_id, "capability": entry.get("capability"), "status": "running",
                                 "created_at": entry.get("at"), "counts": None, "filing": None,
                                 "last_hash": None, "result_sha256": None}
            info = found.get(run_id)
            if info is None:
                continue
            ending = _ENDINGS.get(entry.get("kind"))
            if ending:
                info["status"] = ending
            if entry.get("kind") == "done":
                info.update(counts=entry.get("counts"), filing=entry.get("filing"),
                            last_hash=entry.get("run_last_hash"), result_sha256=entry.get("result_sha256"))
        return found

    def verify(self, deep: bool = True) -> list[str]:
        """Everything wrong with the ledger and every run folder. Empty means they hold.

        A run still in flight is skipped, since its record is being written right now.
        """
        folders = sorted(p for p in self.runs_folder.iterdir() if p.is_dir()) if self.runs_folder.exists() else []
        with self._locked():
            entries = self._read()
            count, last = self._tail() if self.head.exists() else (0, GENESIS)
        problems = [f"ledger {p}" for p in chain_problems(entries)]
        final = entries[-1].get("hash") if entries else GENESIS
        if (count, last) != (len(entries), final):
            problems.append("ledger: lines were cut from the end, or added by hand")
        runs = self._runs_from(entries)
        for run_id, info in runs.items():
            folder = self.runs_folder / run_id
            if info["last_hash"]:
                problems += _run_ending_problems(folder, run_id, info["last_hash"])
            if info["result_sha256"] and (folder / "outcome.json").exists():
                try:
                    kept = json.loads((folder / "outcome.json").read_text(encoding="utf-8"))["result"]
                except (ValueError, KeyError, OSError, TypeError):
                    kept = None
                if hash_of(kept) != info["result_sha256"]:
                    problems.append(f"run {run_id}: its saved result was changed")
        for folder in folders:
            info = runs.get(folder.name)
            if info is None:
                problems.append(f"run {folder.name}: not on the ledger")
            elif info["status"] != "running" and (folder / "run.jsonl").exists():
                problems += [f"run {folder.name} {p}" for p in record_problems(folder, deep)]
        return problems


def _run_ending_problems(folder: Path, run_id: str, last_hash: str) -> list[str]:
    try:
        lines = read_lines(folder / "run.jsonl")
    except (ValueError, OSError):
        return [f"run {run_id}: its record is missing or cannot be read"]
    if not lines or lines[-1].get("hash") != last_hash:
        return [f"run {run_id}: its record does not end where the ledger says"]
    return []
