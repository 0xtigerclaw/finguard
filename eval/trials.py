"""Repeat trials: the same frozen test run live again, and whether each number got the same verdict every time.

Temperature 0 does not promise the same reply, so this measures it. Run `score.py --live --round final --trial N`
first. The first run stays in its own files and counts as one trial. This only reads results.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent


def compare(runs: list[dict]) -> dict:
    """Per number across runs. `pass^k` is the share of numbers that matched in all k runs."""
    numbers = []
    for rows in zip(*(r["present"] for r in runs), strict=True):
        verdicts = [row["true"]["exact"] for row in rows]
        numbers.append({"metric": rows[0]["metric"], "period": rows[0]["period"], "printed": rows[0]["printed"], "verdicts": verdicts,
                        "agree": len(set(verdicts)) == 1})
    n, k = len(numbers), len(runs)
    matched = sum(all(v == "match" for v in x["verdicts"]) for x in numbers)
    summaries = [r["summary"] for r in runs]
    return {
        "summary": {
            "runs": k, "numbers": n,
            "all_trials_agree": sum(x["agree"] for x in numbers),
            "matched_every_time": matched,
            "pass_k": round(matched / n, 4) if n else None,
            "by_run": [{"round": s["round"], **s["true_exact"], "altered_passed": s["false_passed"] + s["new_false_passed"],
                        "altered": s["false_numbers"] + s["new_false_numbers"], "absent_passed": s["absent_passed"], "cost_usd": s["estimated_cost_usd"]}
                       for s in summaries],
            "note": "Run live on the code named by each run's engine_sha256. The first run was on the frozen commit bf56ab0.",
            "engines": sorted({s["engine_sha256"] for s in summaries}),
        },
        "numbers": numbers,
    }


def main() -> None:
    paths = [HERE / "results_roundfinal.json", *sorted(HERE.glob("results_roundfinal_trial*.json"))]
    runs = [json.loads(p.read_text()) for p in paths]
    out = compare(runs)
    (HERE / "results_trials_final.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(out["summary"], indent=1))
    for x in out["numbers"]:
        if not x["agree"]:
            print("DIFFERS", x["metric"], x["period"], x["printed"], x["verdicts"])


if __name__ == "__main__":
    main()
