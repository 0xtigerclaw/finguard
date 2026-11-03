"""The API and the page. Every capability goes through the same door as the CLI."""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .core.approvals import CannotDecide
from .core.context import MAX_MEMO, ROOT, Context
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


class DoRequest(BaseModel):
    params: dict = {}
    approval_id: str | None = Field(default=None, pattern=r"^apr_[0-9a-f]{16}$")


class DecideRequest(BaseModel):
    decision: str = ""
    reason: str = ""
    operator_token: str = ""


def _load_score() -> dict | None:
    """The held-out result is the headline. The repeat run travels with it."""
    def summary(name: str) -> dict | None:
        try:
            return json.loads((ROOT / "eval" / f"results_{name}.json").read_text())["summary"]
        except (OSError, ValueError, KeyError):
            return None

    score = summary("roundheldout")
    if score:
        score["repeat"] = summary("round2")
    return score


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
