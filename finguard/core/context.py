"""What a capability receives: the state folder, the filings, and the way to reach Muse."""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..muse import LiveTransport, Transport
from ..page import PDF_LOCK, Filing
from .approvals import Approvals
from .ledger import Ledger
from .plugin import LoadError, Refused, Registry, discover

ROOT = Path(__file__).parent.parent.parent
MAX_MEMO = 4000
DEFAULT_LIVE_LIMIT = 20  # live memos per instance, to protect the credits

FILINGS = {
    "anthropic": ("ANTH-S1-2026.pdf", "Anthropic", "Form S-1, filed 26 October 2026"),
    "nebius": ("NBIS-Q2-2026.pdf", "Nebius", "Q2 2026 results"),
    "asml": ("ASML-Q3-2026.pdf", "ASML", "Q3 2026 financial statements"),
}
# A saved run replays real Muse replies, so a demo memo opens at once and costs nothing.
SAVED = {
    "anthropic": ("anthropic_memo.txt", "anthropic_memo"),
    "nebius": ("nebius_memo.txt", "nebius_memo"),
    "asml": ("asml_memo.txt", "asml_memo"),
}
# The same memo with its errors fixed, so the release flow can be shown from saved replies.
CORRECTED = {"anthropic": ("anthropic_memo_corrected.txt", "anthropic_memo_corrected")}
_ADDED_ID = re.compile(r"f-[0-9a-f]{12}")
_open: dict[Path, Filing] = {}  # one Filing per file for the whole process, since it holds a PDF and its caches
_open_lock = threading.Lock()


def saved_memo(name: str) -> str | None:
    """The saved example for a filing, if there is one and its replies exist."""
    if name not in SAVED:
        return None
    path = ROOT / "examples" / SAVED[name][0]
    return path.read_text().strip() if path.exists() and saved_replies(name).exists() else None


def saved_replies(name: str) -> Path:
    return ROOT / "tests" / "recordings" / SAVED[name][1]


def corrected_memo(name: str) -> str | None:
    if name not in CORRECTED:
        return None
    path = ROOT / "examples" / CORRECTED[name][0]
    return path.read_text().strip() if path.exists() and corrected_replies(name).exists() else None


def corrected_replies(name: str) -> Path:
    return ROOT / "tests" / "recordings" / CORRECTED[name][1]


def draft_replies(name: str) -> Path:
    return ROOT / "tests" / "recordings" / f"draft_{name}"


def saved_conversation(name: str) -> list[dict]:
    """The saved turns for a filing, in order, if they and their replies exist."""
    path = ROOT / "examples" / f"draft_{name}.json"
    if not (path.exists() and draft_replies(name).exists()):
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))["turns"]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def open_filing(path: Path) -> Filing:
    with _open_lock:
        if path not in _open:
            with PDF_LOCK:
                _open[path] = Filing(path)
        return _open[path]


class Filings:
    """The allowlist: the three built in, plus any added after a person approved it."""

    def __init__(self, state: Path) -> None:
        self.folder = Path(state) / "filings"

    def _added(self, name: str) -> dict | None:
        if not _ADDED_ID.fullmatch(name):
            return None
        try:
            return json.loads((self.folder / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def get(self, name: str) -> Filing:
        if name in FILINGS:
            return open_filing(ROOT / "data" / FILINGS[name][0])
        if self._added(name) is None:
            raise Refused("No such filing.")
        return open_filing(self.folder / f"{name}.pdf")

    def listing(self) -> list[dict]:
        found = [(k, v[1], v[2]) for k, v in FILINGS.items()]
        if self.folder.exists():
            for path in sorted(self.folder.glob("f-*.json")):
                meta = self._added(path.stem)
                if meta:
                    found.append((path.stem, meta["company"], meta["document"]))
        rows = []
        for name, company, document in found:
            filing = self.get(name)
            with PDF_LOCK:
                pages = filing.pages
            rows.append({"id": name, "company": company, "document": document, "sha256": filing.sha256,
                         "pages": pages, "memo": saved_memo(name), "corrected": corrected_memo(name),
                         "conversation": [turn["question"] for turn in saved_conversation(name)]})
        return rows


class LiveBudget:
    """How many live memos this instance has left."""

    def __init__(self, limit: int) -> None:
        self.limit, self.used = limit, 0
        self.lock = threading.Lock()

    def take(self) -> bool:
        with self.lock:
            if self.used >= self.limit:
                return False
            self.used += 1
            return True


@dataclass
class Context:
    state: Path
    registry: Registry
    ledger: Ledger
    approvals: Approvals
    filings: Filings
    live: LiveBudget
    transport: Transport | None = None  # tests only: stands in for Muse
    http: httpx.BaseTransport | None = None  # tests only: stands in for the network
    run_id: str = ""  # the door fills in these three for each call
    actor: str = ""
    approval: dict | None = None

    @classmethod
    def build(cls, state: Path | str | None = None, package: str = "finguard.plugins", **extra) -> "Context":
        """Load the plugins and open the state folder. A broken plugin raises LoadError."""
        folder = Path(state or os.environ.get("FINGUARD_STATE") or "state")
        try:
            limit = int(os.environ.get("FINGUARD_LIVE_LIMIT", DEFAULT_LIVE_LIMIT))
        except ValueError:
            raise LoadError("FINGUARD_LIVE_LIMIT must be a whole number.") from None
        ledger = Ledger(folder)
        ctx = cls(state=folder, registry=discover(package), ledger=ledger, approvals=Approvals(folder, ledger),
                  filings=Filings(folder), live=LiveBudget(limit), **extra)
        for start in ctx.registry.at_start:  # a plugin may add capabilities kept in the state folder
            start(ctx)
        return ctx

    @property
    def run_folder(self) -> Path:
        return self.ledger.runs_folder / self.run_id

    def live_transport(self) -> Transport:
        """The way to reach Muse for one live memo. Refuses with no key or past the limit."""
        if self.transport is not None:
            return self.transport
        if not os.environ.get("MODEL_API_KEY"):
            raise Refused("Live checks are off here. Saved examples still work.")
        if not self.live.take():
            raise Refused("Live checks have reached their limit on this server. Saved examples still work.")
        return LiveTransport(timeout=240)
