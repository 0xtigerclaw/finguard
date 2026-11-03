"""filings.list, filings.page and filings.add. An added file is untrusted data until a person says otherwise."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import socket
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from ..core.context import Context
from ..core.plugin import Capability, Failed, Refused, Registry, hash_of
from ..page import PDF_LOCK, Filing

MAX_BYTES = 30 * 1024 * 1024
MAX_PAGES = 3000
MAX_REDIRECTS = 3
TIMEOUT = 60.0
DEADLINE = 180.0  # a server that drips bytes must not hold a worker for ever
FILING = {"type": "string", "pattern": "^[a-z0-9-]{1,40}$", "description": "A filing id from filings.list."}


def _list(ctx: Context, params: dict) -> dict:
    return {"filings": ctx.filings.listing()}


def _page(ctx: Context, params: dict) -> dict:
    filing = ctx.filings.get(params["filing"])
    with PDF_LOCK:
        pages = filing.pages
    if params["page"] >= pages:
        raise Refused("No such page.")
    png = filing.image(params["page"])
    return {"filing": params["filing"], "page": params["page"], "pages": pages, "filing_sha256": filing.sha256,
            "media_type": "image/png", "image_sha256": hashlib.sha256(png).hexdigest(),
            "png_base64": base64.b64encode(png).decode()}


def _check_url(url: str) -> str:
    """The plain rules for a URL. Returns its host."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise Refused("That URL could not be read.") from None
    if parts.scheme != "https":
        raise Refused("Only https URLs can be added.")
    if not host or parts.username or parts.password:
        raise Refused("That URL could not be read.")
    return host


def _check_host(host: str) -> None:
    """Refuse an address inside our own network, so a URL cannot be used to reach it."""
    try:
        found = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise Failed("That host could not be found.") from None
    if not found or not all(ipaddress.ip_address(row[4][0]).is_global for row in found):
        raise Refused("That URL points at a private address.")


def _fetch(url: str, ctx: Context) -> bytes:
    """Download with no trust: https at every hop, a size cap, and a deadline."""
    started = time.monotonic()
    try:
        with httpx.Client(transport=ctx.http, timeout=TIMEOUT, follow_redirects=False,
                          headers={"User-Agent": "Finguard filings.add"}) as client:
            for _ in range(MAX_REDIRECTS + 1):
                _check_host(_check_url(url))
                with client.stream("GET", url) as reply:
                    if reply.is_redirect:
                        url = urljoin(url, reply.headers.get("location", ""))
                        continue
                    if reply.status_code != 200:
                        raise Failed(f"The server answered {reply.status_code}.")
                    data = bytearray()
                    for chunk in reply.iter_bytes():
                        data += chunk
                        if len(data) > MAX_BYTES:
                            raise Refused("That file is over the 30 MB limit.")
                        if time.monotonic() - started > DEADLINE:
                            raise Failed("The download took too long.")
                    return bytes(data)
    except httpx.HTTPError as e:
        raise Failed(f"The file could not be fetched: {type(e).__name__}.") from None
    raise Failed("The URL redirected too many times.")


def _read_pdf(path: Path) -> int:
    """Open the file as a PDF and count its pages. It runs no scripts, and any trouble is a refusal."""
    try:
        with PDF_LOCK:
            filing = Filing(path)
            try:
                if filing.doc.needs_pass:
                    raise Refused("That PDF is locked with a password.")
                return filing.pages
            finally:
                filing.doc.close()
    except Refused:
        raise
    except Exception:
        raise Refused("That file could not be read as a PDF.") from None


def _add_subject(ctx: Context, params: dict) -> str:
    """Nothing is fetched before approval, so a request cannot make this server download anything.

    The approval binds to the URL and the labels, and to the file's hash if the operator gave one.
    """
    _check_url(params["url"])
    return hash_of({k: params.get(k, "") for k in ("url", "sha256", "company", "document")})


def _add(ctx: Context, params: dict) -> dict:
    if ctx.approval is None:
        raise Refused("Adding a filing needs an approved request.")
    data = _fetch(params["url"], ctx)
    if not data.startswith(b"%PDF-"):
        raise Refused("That file is not a PDF.")
    sha = hashlib.sha256(data).hexdigest()
    if params.get("sha256") and params["sha256"] != sha:
        raise Refused("The file does not have the hash that was approved.")
    name = f"f-{sha[:12]}"
    ctx.filings.folder.mkdir(parents=True, exist_ok=True)
    spare = ctx.filings.folder / f".{ctx.run_id}.pdf"  # the name comes from the hash, never from the server
    spare.write_bytes(data)
    try:
        pages = _read_pdf(spare)
        if pages < 1 or pages > MAX_PAGES:
            raise Refused(f"That PDF has {pages} pages. The limit is {MAX_PAGES}.")
        os.replace(spare, ctx.filings.folder / f"{name}.pdf")
    finally:
        spare.unlink(missing_ok=True)
    meta = {"id": name, "company": params.get("company") or "Added filing", "url": params["url"],
            "document": params.get("document") or "Added by URL", "sha256": sha, "pages": pages,
            "approval_id": ctx.approval["id"]}
    (ctx.filings.folder / f"{name}.json").write_text(json.dumps(meta, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    ctx.ledger.append("filing_added", run_id=ctx.run_id, filing=name, sha256=sha, url=params["url"], bytes=len(data))
    return {**meta, "filing": name}


def register(registry: Registry) -> None:
    registry.add(Capability(
        name="filings.list",
        summary="The filings, with fingerprints.",
        kind="read",
        approval="none",
        verifies="Each fingerprint is the sha256 of the PDF's own bytes.",
        params={"type": "object", "additionalProperties": False, "properties": {}},
        run=_list,
    ))
    registry.add(Capability(
        name="filings.page",
        summary="One page image.",
        kind="read",
        approval="none",
        verifies="Code renders the page from the fingerprinted PDF, and page numbers are bounds checked.",
        params={"type": "object", "additionalProperties": False, "required": ["filing", "page"], "properties": {
            "filing": FILING,
            "page": {"type": "integer", "minimum": 0, "description": "Zero-based page number."},
        }},
        run=_page,
    ))
    registry.add(Capability(
        name="filings.add",
        summary="Add a PDF from an https URL, once a person approves.",
        kind="act",
        approval="person",
        verifies="Code checks https, the PDF header and the 30 MB limit, then records the file's sha256.",
        params={"type": "object", "additionalProperties": False, "required": ["url"], "properties": {
            "url": {"type": "string", "maxLength": 2000, "description": "An https URL."},
            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$", "description": "The hash the file must have."},
            "company": {"type": "string", "maxLength": 60},
            "document": {"type": "string", "maxLength": 80},
        }},
        run=_add,
        subject=_add_subject,
    ))
