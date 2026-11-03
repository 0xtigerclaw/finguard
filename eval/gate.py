"""The eval gate: replay every round with no key, and fail if the score got worse.

It fails on any false pass, on any true number called wrong, on matches falling below the floor, on a saved
reply going missing, on a gallery method leaving its hand-read answer key, and on a case someone marked fixed coming back. An open case from use is reported and does
not fail the gate, since a person's flag should not block every later change. It stays open and visible.
Nothing here writes a results file.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from finguard.core.context import Context
from finguard.plugins.misses import committed_cases, replay_case

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from plugins import score_plugins  # noqa: E402
from score import score_round  # noqa: E402

FLOORS = HERE / "gate_floors.json"


def round_problems(name: str, summary: dict, floor: int) -> list[str]:
    """Everything that makes this round worse than it is allowed to be. Empty means it holds."""
    found = [f"round {name}: {summary[field]} {what}" for field, what in (
        ("false_passed", "altered numbers passed as true"),
        ("new_false_passed", "harder altered numbers passed as true"),
        ("absent_passed", "metrics not in the filing passed as true"),
        ("memo_path_false_passed", "altered numbers passed through the memo path"),
        ("failed_calls", "calls had no saved reply or failed"),
    ) if summary[field]]
    if summary["true_exact"]["wrong"]:
        found.append(f"round {name}: {summary['true_exact']['wrong']} true numbers called wrong")
    if summary["true_exact"]["match"] < floor:
        found.append(f"round {name}: {summary['true_exact']['match']} true numbers matched, below the floor of {floor}")
    return found


def case_problems(case: dict, replayed: dict) -> list[str]:
    if case.get("fixed_in") and replayed["status"] != "fixed":
        return [f"case {case['id']}: marked fixed in {case['fixed_in']}, and now {replayed['status']}: {replayed['why']}"]
    return []


def main() -> int:
    floors = json.loads(FLOORS.read_text())
    problems: list[str] = []
    for name, floor in floors.items():
        summary = score_round(name)["summary"]
        problems += round_problems(name, summary, floor)
        print(f"round {name}: {summary['true_exact']['match']} matched (floor {floor}), {summary['true_exact']['refused']} refused, "
              f"{summary['true_exact']['wrong']} wrong, {summary['false_passed'] + summary['new_false_passed']} of "
              f"{summary['false_pass']['of']} altered passed")
    gallery = score_plugins()
    problems += gallery["problems"]
    print(f"plugins: {gallery['summary']['computed']} computed, {gallery['summary']['refused']} refused, of {gallery['summary']['results']}")
    with tempfile.TemporaryDirectory(prefix="finguard-gate-") as state:
        ctx = Context.build(state)
        cases = committed_cases()
        for case in cases:
            replayed = replay_case(case, ctx)
            problems += case_problems(case, replayed)
            print(f"case {case['id']}: {replayed['status']}, {case['value_text']} is {replayed['verdict']}, a person said {case['should_be']}")
        if not cases:
            print("cases from use: none yet")
    for problem in problems:
        print("FAIL", problem)
    print("Gate failed." if problems else "Gate holds.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
