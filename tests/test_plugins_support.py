"""Shared by the harness tests: a scripted Muse for the Anthropic memo with every number corrected."""
from __future__ import annotations

import json
from pathlib import Path

from finguard.check import Claim, candidate_pages
from finguard.core.context import Context
from finguard.core.context import open_filing
from finguard.core.door import dispatch

DATA = Path(__file__).parent.parent / "data"
BRIDGE = 166
CORRECTED_MEMO = (
    "Anthropic's S-1 shows a business with a large loss. In the six months to June 30, 2026, revenue reached "
    "$16.3bn. Adjusted operating income was $1.6bn. The company made an operating loss of $693m. Net loss was "
    "$80.7bn. Stock-based compensation expense was $2.2bn. Adjusted operating margin was 10%."
)
REVENUE_PAGE = 158  # plain text search does not offer the bridge page for revenue
_LINES = [  # quote, value, metric, direction, printed cell, row label
    ("revenue reached $16.3bn.", "$16.3bn", "Revenue", "neutral", "16,286", "Revenue"),
    ("Adjusted operating income was $1.6bn.", "$1.6bn", "Adjusted operating income", "gain", "1,618", "Adjusted Operating Income (Loss)"),
    ("The company made an operating loss of $693m.", "$693m", "Operating loss", "loss", "(693)", "Operating loss"),
    ("Net loss was $80.7bn.", "$80.7bn", "Net loss", "loss", "(80,748)", "Net loss"),
    ("Stock-based compensation expense was $2.2bn.", "$2.2bn", "Stock-based compensation expense", "neutral", "2,185", "Stock-based compensation expense"),
    ("Adjusted operating margin was 10%.", "10%", "Adjusted operating margin", "neutral", "10 %", "Adjusted Operating Margin"),
]


def _reply(content: dict) -> bytes:
    return json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode()


class ScriptedMuse:
    """Stands in for Muse on the corrected memo. It is not Muse. Calls overlap, so it answers by metric."""

    def __init__(self) -> None:
        self.filing = open_filing(DATA / "ANTH-S1-2026.pdf")
        self.calls = 0

    def send(self, body: bytes) -> bytes:
        self.calls += 1
        request = json.loads(body)
        text = request["messages"][1]["content"][0]["text"]
        if "Metric:" not in text:
            return _reply({"claims": [
                {"quote": q, "value_text": v, "metric": m, "period_kind": "six_months", "period_year": 2026, "direction": d}
                for q, v, m, d, _, _ in _LINES]})
        metric = text.split("\n")[0].removeprefix("Metric: ")
        line = next(row for row in _LINES if row[2] == metric)
        pages = candidate_pages(self.filing, Claim("x", "x", metric, "six_months", 2026, "neutral"))
        page = REVENUE_PAGE if metric == "Revenue" else BRIDGE
        r = self.filing.doc[page].search_for(line[4])[0]
        box = self.filing.points_to_grid(page, (r.x0, r.y0, r.x1, r.y1))
        return _reply({"found": True, "page": pages.index(page), "box": box, "row_label": line[5]})


def build(tmp_path, **extra) -> Context:
    return Context.build(state=tmp_path / "state", transport=ScriptedMuse(), **extra)


def check_corrected(ctx: Context):
    outcome = dispatch("memo.check", {"memo": CORRECTED_MEMO, "filing": "anthropic"}, "terminal", ctx)
    assert outcome.status == "done", outcome.why
    assert outcome.result["counts"] == {"match": 6, "wrong": 0, "refused": 0, "unchecked": 0}, outcome.result["receipts"]
    return outcome


def rechain(path: Path, change) -> None:
    """Rewrite a record's lines with `change` and fix every hash, as a careful forger would."""
    from finguard.record import GENESIS, _line_hash
    prev, out = GENESIS, []
    for raw in path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(raw)
        entry.pop("hash")
        change(entry)
        entry["prev"] = prev
        entry["hash"] = prev = _line_hash(entry)
        out.append(json.dumps(entry, sort_keys=True, ensure_ascii=False))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
