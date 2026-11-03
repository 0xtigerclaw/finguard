"""The API and the page. Every capability goes through the same door as the CLI.

Limits, all plain code and all set by env vars with safe defaults:

- Rate: FINGUARD_RATE_LIMIT POST calls (default 60) per
  FINGUARD_RATE_WINDOW_SECONDS (default 60) from one IP. Over that, a POST
  gets 429 and a Retry-After header, and nothing reaches the door. The IP
  is the connection IP. Behind Cloud Run, FINGUARD_BEHIND_PROXY uses the last
  forwarded address, which the proxy adds. Earlier ones are the caller's own.
- Runs: FINGUARD_MAX_RUNS (default 1000) runs stored on the ledger. At the
  cap, POST /api/do gets 503 and nothing reaches the door.
- Threads: FINGUARD_MAX_THREADS (default 200) stored approval requests.
  In this file a thread means one stored approval request file. A POST
  that would ask for a new approval at the cap gets 503.

Both caps refuse new work. They never evict. Deleting the oldest run or
approval would leave ledger lines that point at a folder or request that
is gone, so the chain would no longer hold. Old runs and approvals stay
readable through the GET routes, and saved memos still replay with no key
while there is room.
"""
from __future__ import annotations

import hmac
import json
import math
import os
import threading
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .core.approvals import CannotDecide
from .core.context import FILINGS, open_filing, MAX_MEMO, ROOT, Context
from .core.door import dispatch
from .core.ledger import record_problems
from .core.plugin import LoadError, Refused
from .page import PDF_LOCK
from .plugins.draft import public_thread, read_thread
from .plugins.release import bundle_exists, read_bundle
from .record import read_lines

STATIC = Path(__file__).parent / "static"

try:
    ctx = Context.build()
except LoadError as e:
    raise SystemExit(f"Finguard cannot start: {e}") from None

app = FastAPI(title="Finguard", docs_url=None, redoc_url=None)


@app.exception_handler(RequestValidationError)
def plain_bad_request(request: Request, error: RequestValidationError) -> JSONResponse:
    """FastAPI's own message echoes the input, which could hold a token."""
    return JSONResponse({"detail": "That request is not in the right form."}, status_code=400)


DEFAULT_RATE_LIMIT = 60
DEFAULT_RATE_WINDOW_SECONDS = 60
DEFAULT_MAX_RUNS = 1000
DEFAULT_MAX_THREADS = 200
MAX_TRACKED_IPS = 10000


def _limit_from_env(name: str, default: int) -> int:
    """A whole number of 1 or more from the env, or the default if unset or bad."""
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value >= 1 else default


class _RateLimiter:
    """One sliding window of POST times per client IP, held in memory.

    The window restarts when the limits change, so a test that sets low
    limits does not inherit counts made under the old ones. The IP map is
    capped too: expired windows are dropped first, then the IP with the
    oldest last hit.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}
        self._config: tuple[int, int] | None = None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._config = None

    def check(self, ip: str, limit: int, window: int, now: float | None = None) -> int | None:
        """Record one POST and return None, or return seconds to wait and record nothing."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._config != (limit, window):
                self._hits.clear()
                self._config = (limit, window)
            hits = self._hits.get(ip)
            if hits is None:
                if len(self._hits) >= MAX_TRACKED_IPS:
                    self._drop_old(now, window)
                hits = self._hits.setdefault(ip, deque())
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return max(1, math.ceil(hits[0] + window - now))
            hits.append(now)
            return None

    def _drop_old(self, now: float, window: int) -> None:
        """Make room for one new IP, dropping only whole windows."""
        for ip in [ip for ip, hits in self._hits.items() if not hits or hits[-1] <= now - window]:
            del self._hits[ip]
        while len(self._hits) >= MAX_TRACKED_IPS:
            oldest = min(self._hits, key=lambda ip: self._hits[ip][-1] if self._hits[ip] else 0.0)
            del self._hits[oldest]


_rate_limiter = _RateLimiter()


def _reset_rate_limit() -> None:
    """Clear all rate counts. Tests use this to start from a clean window."""
    _rate_limiter.reset()


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """Throttle every POST route by client IP before it can grow the ledger."""
    if request.method != "POST":
        return await call_next(request)
    limit = _limit_from_env("FINGUARD_RATE_LIMIT", DEFAULT_RATE_LIMIT)
    window = _limit_from_env("FINGUARD_RATE_WINDOW_SECONDS", DEFAULT_RATE_WINDOW_SECONDS)
    ip = _client_ip(request)
    wait = _rate_limiter.check(ip, limit, window)
    if wait is not None:
        return JSONResponse({"detail": "Too many requests. Try again later."},
                            status_code=429, headers={"Retry-After": str(wait)})
    return await call_next(request)


def _storage_refusal(capability: str, approval_id: str | None) -> str | None:
    """Why a POST /api/do must not reach the door, or None if there is room.

    The check is deterministic: stored runs are counted on the ledger in
    ledger order, threads are the stored approval requests, and at either
    cap the new call is refused. Nothing is deleted.
    """
    if len(ctx.ledger.runs()) >= _limit_from_env("FINGUARD_MAX_RUNS", DEFAULT_MAX_RUNS):
        return "Run storage is full. No new runs are being taken."
    cap = ctx.registry.get(capability)
    if cap is not None and cap.approval == "person" and approval_id is None:
        if len(ctx.approvals.all()) >= _limit_from_env("FINGUARD_MAX_THREADS", DEFAULT_MAX_THREADS):
            return "Approval storage is full. No new requests are being taken."
    return None


class DoRequest(BaseModel):
    params: dict = {}
    approval_id: str | None = Field(default=None, pattern=r"^apr_[0-9a-f]{16}$")


class DecideRequest(BaseModel):
    decision: str = ""
    reason: str = ""
    operator_token: str = ""


def _client_ip(request: Request) -> str:
    """Who is calling. Behind Cloud Run every request arrives from its proxy, which appends the real
    address last. Earlier entries are whatever the caller sent, so they do not count."""
    if os.environ.get("FINGUARD_BEHIND_PROXY"):
        forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
        if forwarded:
            return forwarded[-1]
    return request.client.host if request.client else "unknown"


def _load_score() -> dict | None:
    """The final frozen test is the headline. The earlier frozen test and the repeat run travel with it."""
    def summary(name: str) -> dict | None:
        try:
            return json.loads((ROOT / "eval" / f"results_{name}.json").read_text())["summary"]
        except (OSError, ValueError, KeyError):
            return None

    score = summary("roundfinal")
    if score:
        score["repeat"] = summary("round2")
        score["earlier"] = summary("roundfresh")
    return score


@app.on_event("startup")
def _warm_saved_pages() -> None:
    """Read the saved demos' tables once at start. The first read of a page is slow, then it is cached."""
    if not os.environ.get("FINGUARD_WARM"):
        return
    for pdf, *_ in FILINGS.values():
        filing = open_filing(ROOT / "data" / pdf)
        for pinned in sorted((ROOT / "data" / "pinned_pages" / filing.sha256[:16]).glob("*.png")):
            filing.text(int(pinned.stem))
            filing.cells_on(int(pinned.stem))


def _token_is_right(given: str) -> bool:
    """Compared in constant time. With no token configured, nobody can decide on the web."""
    expected = os.environ.get("FINGUARD_OPERATOR_TOKEN", "")
    return bool(expected) and hmac.compare_digest(given.encode("utf-8", "replace"), expected.encode("utf-8", "replace"))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/page/{name}/{page}.png")
def page_image(name: str, page: int) -> Response:
    try:
        filing = ctx.filings.get(name)
    except Refused:
        raise HTTPException(404, "No such filing.") from None
    with PDF_LOCK:
        pages = filing.pages
    if not 0 <= page < pages:
        raise HTTPException(404, "No such page.")
    return Response(filing.image(page), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/start")
def start() -> dict:
    return {
        "filings": ctx.filings.listing(),
        "live": bool(os.environ.get("MODEL_API_KEY")),
        "max_memo": MAX_MEMO,
        "operator_required": bool(os.environ.get("FINGUARD_OPERATOR_TOKEN")),
        "score": _load_score(),
    }


@app.get("/api/rules")
def rules() -> dict:
    return {"capabilities": ctx.registry.rules()}


@app.post("/api/do/{capability}")
def do(capability: str, req: DoRequest) -> dict:
    refusal = _storage_refusal(capability, req.approval_id)
    if refusal is not None:
        raise HTTPException(503, refusal)
    return dispatch(capability, req.params, "web", ctx, approval_id=req.approval_id).to_dict()


@app.get("/api/runs")
def runs() -> dict:
    found = list(ctx.ledger.runs().values())[::-1][:200]
    return {"runs": [{k: r[k] for k in ("run_id", "capability", "status", "created_at", "counts", "filing")} for r in found]}


@app.get("/api/runs/{run_id}")
def run(run_id: str) -> dict:
    info = ctx.ledger.runs().get(run_id)  # only ids on the ledger reach the disk
    if info is None:
        raise HTTPException(404, "No such run.")
    folder = ctx.ledger.runs_folder / run_id
    result = None
    try:
        result = json.loads((folder / "outcome.json").read_text(encoding="utf-8"))["result"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    record = {"lines": 0, "last_hash": "", "holds": True}
    if (folder / "run.jsonl").exists():
        try:
            lines = read_lines(folder / "run.jsonl")
        except ValueError:
            lines = []
        last = lines[-1].get("hash", "") if lines else ""
        holds = not record_problems(folder) and bool(lines) and (not info["last_hash"] or info["last_hash"] == last)
        record = {"lines": len(lines), "last_hash": last, "holds": holds}
    return {"run_id": run_id, "capability": info["capability"], "status": info["status"], "result": result, "record": record}


@app.get("/api/threads/{thread_id}")
def thread(thread_id: str) -> dict:
    found = read_thread(ctx, thread_id)  # only a well formed id reaches the disk
    if found is None:
        raise HTTPException(404, "No such conversation.")
    return public_thread(found)


@app.get("/api/approvals")
def approvals() -> dict:
    return {"approvals": ctx.approvals.all()}


@app.post("/api/approvals/{approval_id}/decide")
def decide(approval_id: str, req: DecideRequest) -> dict:
    if not _token_is_right(req.operator_token):
        raise HTTPException(403, "Only an operator can decide, and that needs a valid operator token.")
    try:
        return ctx.approvals.decide(approval_id, req.decision, "web_operator", req.reason)
    except CannotDecide as e:
        raise HTTPException(404 if e.missing else 409, str(e)) from None


@app.get("/api/ledger")
def ledger(limit: int = Query(100, ge=1, le=1000)) -> dict:
    """The chain and each run's record. `finguard ledger verify` also rebuilds every request from its images."""
    problems = ctx.ledger.verify(deep=False)
    return {"lines": ctx.ledger.lines(limit=limit), "holds": not problems, "problems": problems}


@app.get("/api/releases/{release_id}")
def release(release_id: str) -> dict:
    if not bundle_exists(ctx, release_id):
        raise HTTPException(404, "No such release.")
    return read_bundle(ctx, release_id)
