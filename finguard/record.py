"""If the model saw it, it is a record.

Each run is an append-only file. Every line carries the hash of the line before it, so a
changed or removed line breaks the chain. Each page image is kept once by its hash, which
lets a request be rebuilt byte for byte and compared with the hash taken when it was sent.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

GENESIS = "0" * 64
SCHEMA_VERSION = 1
_DATA_URL = re.compile(r"data:image/png;base64,([A-Za-z0-9+/=]+)")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _line_hash(entry: dict) -> str:
    return sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False).encode())


class Run:
    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        (self.folder / "images").mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "run.jsonl"
        self.last, self.count = GENESIS, 0
        if self.path.exists():
            lines = read_lines(self.path)
            self.count, self.last = len(lines), lines[-1]["hash"] if lines else GENESIS

    def write(self, kind: str, **fields) -> dict:
        entry = {"schema": SCHEMA_VERSION, "seq": self.count, "prev": self.last, "kind": kind, **fields}
        entry["hash"] = _line_hash(entry)
        with self.path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
        self.last, self.count = entry["hash"], self.count + 1
        return entry

    def write_call(self, body: bytes, reply: bytes | None, error: str | None) -> dict:
        """Record exactly what Muse was sent and what came back, failures included."""
        def park(m: re.Match) -> str:
            raw = base64.b64decode(m.group(1))
            name = sha256(raw)
            (self.folder / "images" / f"{name}.png").write_bytes(raw)
            return f"image:{name}"

        return self.write(
            "muse_call",
            request_sha256=sha256(body),
            request=_DATA_URL.sub(park, body.decode("utf-8")),
            reply=reply.decode("utf-8", "replace") if reply is not None else None,
            reply_sha256=sha256(reply) if reply is not None else None,
            error=error,
        )


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def rebuild_request(entry: dict, folder: Path) -> bytes:
    """The exact bytes sent to Muse, rebuilt from the record."""
    def unpark(m: re.Match) -> str:
        raw = (Path(folder) / "images" / f"{m.group(1)}.png").read_bytes()
        return "data:image/png;base64," + base64.b64encode(raw).decode()

    return re.sub(r"image:([0-9a-f]{64})", unpark, entry["request"]).encode("utf-8")


def verify(folder: Path) -> list[str]:
    """Everything wrong with a run's record. Empty means the chain and every request hold."""
    folder = Path(folder)
    problems, prev = [], GENESIS
    for i, entry in enumerate(read_lines(folder / "run.jsonl")):
        body = {k: v for k, v in entry.items() if k != "hash"}
        if entry.get("seq") != i:
            problems.append(f"line {i}: out of sequence")
        if entry.get("prev") != prev:
            problems.append(f"line {i}: does not follow the line before it")
        if _line_hash(body) != entry.get("hash"):
            problems.append(f"line {i}: changed after it was written")
        if entry.get("kind") == "muse_call":
            try:
                if sha256(rebuild_request(entry, folder)) != entry["request_sha256"]:
                    problems.append(f"line {i}: request does not rebuild to what was sent")
            except OSError:
                problems.append(f"line {i}: a page image is missing")
        prev = entry.get("hash")
    return problems
