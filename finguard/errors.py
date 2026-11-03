"""One set of failures. A failed call is never mistaken for an answer."""
from __future__ import annotations


class MuseError(Exception):
    """A model call that failed. `code` is one of the names below; the raw provider error stays in `detail`."""

    CODES = ("auth", "rate_limited", "too_large", "timeout", "unavailable", "bad_request", "bad_reply", "no_recording")

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in self.CODES:
            raise ValueError(f"unknown failure code: {code}")
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def code_for_status(status: int) -> str:
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limited"
    if status == 413:
        return "too_large"
    if status >= 500:
        return "unavailable"
    return "bad_request"
