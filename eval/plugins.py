"""The plugin eval: every gallery method on every filing, replayed with no API, against an answer key read by hand.

This is separate from the measured score. It has 15 results on 3 filings, and it says which refusals were misses.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pymupdf

from finguard.core.context import FILINGS, ROOT, Context
from finguard.core.door import dispatch

HERE = Path(__file__).parent
REFUSALS = ("right_to_refuse", "miss")


def _problems(name: str, want: dict, got: dict, pdf) -> list[str]:
    label = f"{want['method']} on {name}"
    if want["judged"] in REFUSALS:
        return [] if got["status"] == "unavailable" else [f"{label}: the key expects a refusal, and it gave {got.get('display')}"]
    if got["status"] != "computed" or got.get("display") != want["display"]:
        return [f"{label}: the key says {want['display']}, and it gave {got.get('display') or got['status']}"]
    read = sorted((i["printed"], i["page"] + 1) for i in got["inputs"])
    found = [f"{label}: the key's cells differ from the cells code read"] if read != sorted(map(tuple, want["cells"])) else []
    return found + [f"{label}: {printed} is not on page {page}" for printed, page in want["cells"] if printed not in pdf(name)[page - 1].get_text()]


def score_plugins() -> dict:
    key = json.loads((HERE / "answer_key_plugins.json").read_text())
    prompts = {p["title"]: p["prompt"] for p in json.loads((ROOT / "examples" / "creator_prompts.json").read_text())["prompts"]}
    opened: dict = {}

    def pdf(name: str):
        return opened.setdefault(name, pymupdf.open(ROOT / "data" / FILINGS[name][0]))

    rows, problems = [], []
    with tempfile.TemporaryDirectory(prefix="finguard-plugins-") as state:
        ctx = Context.build(state)
        for method in key["methods"]:
            params = {"prompt": prompts[method["name"]]}
            if "assumption" in method:
                params["assumption"] = method["assumption"]
            out = dispatch("plugins.create", params, "eval", ctx)
            if out.status != "done":
                problems.append(f"{method['name']}: {out.status}: {out.why}")
                continue
            for got in out.result["results"]:
                want = {**method["results"][got["filing"]], "method": method["name"]}
                problems += _problems(got["filing"], want, got, pdf)
                rows.append({"method": method["name"], "filing": got["filing"], "status": got["status"], "display": got.get("display"),
                             "judged": want["judged"], "note": want.get("note", ""), "reason": got.get("reason", "")})
    judged = [r["judged"] for r in rows]
    return {"summary": {
        "methods": len(key["methods"]), "filings": len({r["filing"] for r in rows}), "results": len(rows),
        "computed": judged.count("right"), "computed_right": judged.count("right"),
        "refused": judged.count("right_to_refuse") + judged.count("miss"),
        "right_to_refuse": judged.count("right_to_refuse"), "missed": judged.count("miss"),
        "dropped": key["dropped"], "note": key["note"]}, "rows": rows, "problems": problems}


def main() -> int:
    out = score_plugins()
    (HERE / "results_plugins.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(out["summary"], indent=1, ensure_ascii=False))
    for problem in out["problems"]:
        print("FAIL", problem)
    return 1 if out["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
