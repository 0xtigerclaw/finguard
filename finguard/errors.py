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


_PLAIN = {
    "auth": "the Muse key was not accepted",
    "rate_limited": "Muse is busy right now",
    "too_large": "the request was too large for Muse",
    "timeout": "Muse took too long",
    "unavailable": "Muse could not be reached",
    "bad_request": "Muse did not accept the request",
    "bad_reply": "Muse's reply could not be read",
    "no_recording": "no saved reply matches this request",
}


def plainly(error: MuseError) -> str:
    """A failure in plain words, for a person to read. The code itself stays on the record."""
    return _PLAIN.get(error.code, "Muse could not answer")
