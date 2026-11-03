"""memo.check: every number gets a receipt, kept in the run's own folder."""
from __future__ import annotations

import os
from pathlib import Path

from .. import house
from ..check import MATCH, REFUSED, UNCHECKED, WRONG, check_memo
from ..core.context import MAX_MEMO, Context, corrected_memo, corrected_replies, saved_examples, saved_memo, saved_replies
from ..core.ledger import record_problems
from ..core.plugin import Capability, Refused, Registry
from ..figures import numbers_in_memo, read_figure
from ..muse import Muse, ReplayTransport, Transport
from ..page import Filing
from ..pointers import PointerMuse, SavedPointers, find_saved_pointers
from ..record import Run

MAX_DECIMALS = 12  # past this, comparing a figure with a cell runs out of decimal precision

MEMO_PARAMS = {"type": "object", "additionalProperties": False, "required": ["memo", "filing"], "properties": {
    "memo": {"type": "string", "description": f"Up to {MAX_MEMO:,} characters."},
    "filing": {"type": "string", "pattern": "^[a-z0-9-]{1,40}$", "description": "A filing id from filings.list."},
    "live": {"type": "boolean", "description": "Call Muse now, even for a memo that has a recording."},
}}


def place_receipts(memo: str, receipts: list[dict]) -> list[dict]:
    """Say where in the memo each number sits, so the page can mark it."""
    taken: list[tuple[int, int]] = []

    def free(start: int, end: int) -> bool:
        return not any(a < end and start < b for a, b in taken)

    for r in receipts:
        r["start"] = r["end"] = None
        if r["quote"]:
            at = memo.find(r["quote"])
            while at != -1:
                v = memo.find(r["value_text"], at, at + len(r["quote"]))
                if v != -1 and free(v, v + len(r["value_text"])):
                    r["start"], r["end"] = v, v + len(r["value_text"])
                    taken.append((v, r["end"]))
                    break
                at = memo.find(r["quote"], at + 1)
    spots = numbers_in_memo(memo)
    for r in receipts:
        if r["start"] is None:
            for start, end, text in spots:
                if text == r["value_text"] and free(start, end):
                    r["start"], r["end"] = start, end
                    taken.append((start, end))
                    break
    return receipts


def clean_memo(memo: str) -> str:
    """The memo as it will be checked, or a refusal in plain words."""
    memo = memo.strip()
    if not memo:
        raise Refused("Paste a memo first.")
    if len(memo) > MAX_MEMO:
        raise Refused(f"Keep the memo under {MAX_MEMO:,} characters.")
    return memo


def _refuse_too_fine(memo: str) -> None:
    """Refuse a number no comparison could finish on, before any call is spent on it."""
    for _, _, text in numbers_in_memo(memo):
        figure = read_figure(text)
        if figure and figure.decimals > MAX_DECIMALS:
            raise Refused(f"A number in this memo has more than {MAX_DECIMALS} decimal places. Shorten it and check again.")


def _share_images(folder: Path, store: Path) -> None:
    """Keep each page image once across runs, so repeat checks of a saved memo cannot fill the disk."""
    store.mkdir(parents=True, exist_ok=True)
    for png in (folder / "images").glob("*.png"):
        kept = store / png.name
        try:
            if kept.exists():
                spare = png.with_suffix(".tmp")
                os.link(kept, spare)
                os.replace(spare, png)
            else:
                os.link(png, kept)
        except OSError:
            continue  # a disk with no hard links just keeps the copies


def checked(ctx: Context, memo: str, name: str, filing: Filing, transport: Transport, run: Run, saved: bool,
            pointers: SavedPointers | None = None) -> dict:
    """Run the check on a run already open, and build the memo.check result. memo.draft uses this too."""
    rules = house.active_rules(ctx)  # before any Muse call, so rules that do not hold cost nothing
    muse = PointerMuse(transport, run, pointers) if pointers else Muse(transport, run)
    receipts = [r.to_dict() for r in check_memo(memo, filing, muse, run)]
    notes = house.apply_to_receipts(rules, filing, receipts)
    # On the run's own record, so a release reads which rules this check ran under from the hash chain.
    run.write("house", rules_sha256=rules.sha256, rules_label=rules.label, notes=notes)
    _share_images(run.folder, ctx.state / "images")
    problems = record_problems(run.folder)
    return {
        "receipts": place_receipts(memo, receipts),
        "saved": saved,
        "saved_pointers": pointers is not None,
        "base_memo": pointers.base_memo if pointers else None,
        "model": muse.model,
        "filing": name,
        "filing_sha256": filing.sha256,
        "memo": memo,
        "counts": {v: sum(r["verdict"] == v for r in receipts) for v in (MATCH, WRONG, REFUSED, UNCHECKED)},
        "record": {"lines": run.count, "last_hash": run.last, "holds": not problems},
        "rules_sha256": rules.sha256,
        "rules_label": rules.label,
        "house_notes": notes,
    }


def _check(ctx: Context, params: dict) -> dict:
    memo = clean_memo(params["memo"])
    filing = ctx.filings.get(params["filing"])
    if params.get("live"):
        _refuse_too_fine(memo)
        return checked(ctx, memo, params["filing"], filing, ctx.live_transport(), Run(ctx.run_folder), False)
    is_corrected = memo == corrected_memo(params["filing"])
    saved = is_corrected or memo == saved_memo(params["filing"])
    if saved:  # A memo identical to a saved example replays real replies, with no API call and no key.
        replies = corrected_replies(params["filing"]) if is_corrected else saved_replies(params["filing"])
        return checked(ctx, memo, params["filing"], filing, ReplayTransport(replies), Run(ctx.run_folder), True)
    _refuse_too_fine(memo)
    pointers = find_saved_pointers(memo, saved_examples(params["filing"]))
    if pointers:  # Only its numbers differ, so no live call is needed and none is spent.
        return checked(ctx, memo, params["filing"], filing, ReplayTransport(pointers.replies), Run(ctx.run_folder), False, pointers)
    transport = ctx.live_transport()
    return checked(ctx, memo, params["filing"], filing, transport, Run(ctx.run_folder), False)


def register(registry: Registry) -> None:
    registry.add(Capability(
        name="memo.check",
        summary="Every number in a memo gets a receipt from the filing, or a refusal.",
        kind="read",
        approval="none",
        verifies="Plain code reads the PDF's own text where Muse points and gives each verdict; Muse never decides.",
        params=MEMO_PARAMS,
        run=_check,
    ))
