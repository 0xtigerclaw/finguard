"""The one door to Meta's Muse. Calls are recorded, failures are named, nothing retries here."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Protocol

import httpx

from .errors import MuseError, code_for_status
from .record import Run, sha256

BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3"
MAX_IMAGES = 50  # Meta's limit per request


class Transport(Protocol):
    def send(self, body: bytes) -> bytes: ...


class LiveTransport:
    def __init__(self, api_key: str | None = None, timeout: float = 120.0) -> None:
        self.key = api_key or os.environ.get("MODEL_API_KEY", "")
        self.timeout = timeout

    def send(self, body: bytes) -> bytes:
        if not self.key:
            raise MuseError("auth", "MODEL_API_KEY is not set")
        try:
            r = httpx.post(f"{BASE_URL}/chat/completions", content=body, timeout=self.timeout,
                           headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        except httpx.TimeoutException as e:
            raise MuseError("timeout", str(e)) from e
        except httpx.HTTPError as e:
            raise MuseError("unavailable", str(e)) from e
        if r.status_code != 200:
            raise MuseError(code_for_status(r.status_code), f"HTTP {r.status_code}: {r.text[:300]}")
        return r.content


class RecordingTransport:
    """Passes calls through and keeps each real reply, so tests can replay it with no API."""

    def __init__(self, inner: Transport, folder: Path) -> None:
        self.inner, self.folder = inner, Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def send(self, body: bytes) -> bytes:
        reply = self.inner.send(body)
        (self.folder / f"{sha256(body)}.json").write_bytes(reply)
        return reply


class ReplayTransport:
    """Answers only from saved replies. An unknown request fails loudly, and so does a saved reply nobody asked for."""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.unused = {p.stem for p in self.folder.glob("*.json")}

    def send(self, body: bytes) -> bytes:
        key = sha256(body)
        path = self.folder / f"{key}.json"
        if not path.exists():
            raise MuseError("no_recording", f"nothing saved for request {key[:12]}")
        self.unused.discard(key)
        return path.read_bytes()

    def assert_all_used(self) -> None:
        if self.unused:
            raise AssertionError(f"{len(self.unused)} saved replies were never asked for")


def image_part(png: bytes) -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}


class Muse:
    def __init__(self, transport: Transport, run: Run, model: str = DEFAULT_MODEL) -> None:
        self.transport, self.run, self.model = transport, run, model

    def ask(self, system: str, content: list[dict], schema_name: str, schema: dict) -> dict:
        """One bounded call. Returns the JSON Muse gave, or raises MuseError. Always recorded."""
        images = sum(1 for part in content if part.get("type") == "image_url")
        if images > MAX_IMAGES:
            raise MuseError("too_large", f"{images} images, the limit is {MAX_IMAGES}")
        body = json.dumps({
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
        }, sort_keys=True, ensure_ascii=False).encode("utf-8")
        try:
            reply = self.transport.send(body)
        except MuseError as e:
            self.run.write_call(body, None, e.code)
            raise
        self.run.write_call(body, reply, None)
        try:
            return json.loads(json.loads(reply)["choices"][0]["message"]["content"])
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise MuseError("bad_reply", f"not the JSON that was asked for: {e}") from e
