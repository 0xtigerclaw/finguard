"""The eval gate fails when the score gets worse, and the bounds say what the sample can support."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "eval"))

from gate import FLOORS, case_problems, round_problems  # noqa: E402
from score import limits, upper_95  # noqa: E402
from trials import compare  # noqa: E402

GOOD = {"false_passed": 0, "new_false_passed": 0, "absent_passed": 0, "memo_path_false_passed": 0, "failed_calls": 0,
        "true_exact": {"match": 19, "refused": 6, "wrong": 0}}


def test_a_good_round_holds():
    assert round_problems("final", GOOD, 19) == []


@pytest.mark.parametrize("field", ["false_passed", "new_false_passed", "absent_passed", "memo_path_false_passed", "failed_calls"])
def test_any_false_pass_or_missing_reply_fails(field):
    assert len(round_problems("final", {**GOOD, field: 1}, 19)) == 1


def test_a_true_number_called_wrong_fails():
    assert round_problems("final", {**GOOD, "true_exact": {"match": 19, "refused": 5, "wrong": 1}}, 19)


def test_a_drop_in_matches_fails():
    assert round_problems("final", {**GOOD, "true_exact": {"match": 18, "refused": 7, "wrong": 0}}, 19)


def test_the_floors_match_the_saved_results():
    here = FLOORS.parent
    for name, floor in json.loads(FLOORS.read_text()).items():
        assert json.loads((here / f"results_round{name}.json").read_text())["summary"]["true_exact"]["match"] == floor


def test_an_open_case_is_reported_and_a_fixed_case_coming_back_fails():
    assert case_problems({"id": "m", "fixed_in": None}, {"status": "open", "why": ""}) == []
    assert case_problems({"id": "m", "fixed_in": "abc"}, {"status": "fixed", "why": ""}) == []
    assert case_problems({"id": "m", "fixed_in": "abc"}, {"status": "open", "why": ""})
    assert case_problems({"id": "m", "fixed_in": "abc"}, {"status": "stale", "why": ""})


@pytest.mark.parametrize("hits,n,bound", [(0, 19, 0.1459), (0, 25, 0.1129), (0, 644, 0.0046), (1, 20, 0.2161), (400, 1896, 0.227)])
def test_upper_bounds(hits, n, bound):
    assert upper_95(hits, n) == pytest.approx(bound, abs=0.0002)


def test_no_answers_has_no_bound():
    assert upper_95(0, 0) is None


def row(false="refused"):
    return {"false": {"a": false}, "new_false": {"b": "refused"}}


def test_limits_on_the_final_test_shape():
    out = limits([row()] * 25, {"match": 19, "refused": 6, "wrong": 0}, 644, 0)
    assert out["coverage"] == {"answered": 19, "of": 25, "rate": 0.76}
    assert out["risk"]["of"] == 19 and out["risk"]["upper_95"] == pytest.approx(0.1459, abs=0.0002)
    assert out["false_pass"]["upper_95"] == pytest.approx(0.0046, abs=0.0002)
    assert out["false_pass"]["upper_95_counting_each_source_once"] == pytest.approx(0.1129, abs=0.0002)


def test_trials_count_a_number_that_changed():
    def run(verdicts):
        return {"summary": {"round": "x", "true_exact": {}, "false_passed": 0, "new_false_passed": 0, "false_numbers": 1, "new_false_numbers": 1,
                            "absent_passed": 0, "estimated_cost_usd": 0, "engine_sha256": "e"},
                "present": [{"metric": "m", "period": "p", "printed": "1", "true": {"exact": v}} for v in verdicts]}
    out = compare([run(["match", "match"]), run(["match", "refused"])])
    assert out["summary"]["all_trials_agree"] == 1 and out["summary"]["matched_every_time"] == 1 and out["summary"]["pass_k"] == 0.5
    assert [n["agree"] for n in out["numbers"]] == [True, False]
