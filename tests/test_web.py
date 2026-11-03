"""Every API endpoint, against a real door and a throwaway state folder."""
import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from finguard import web
from finguard.core.context import saved_memo
from finguard.plugins.memos import place_receipts
from finguard.web import app
from tests.test_plugins_support import CORRECTED_MEMO, ScriptedMuse, build

client = TestClient(app)
MEMO = saved_memo("anthropic")
TOKEN = "a-test-operator-token"


@pytest.fixture(autouse=True)
def ctx(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("FINGUARD_OPERATOR_TOKEN", raising=False)
    made = build(tmp_path)
    made.transport = None
    monkeypatch.setattr(web, "ctx", made)
    return made


def do(name, params=None, **extra):
    return client.post(f"/api/do/{name}", json={"params": params or {}, **extra}).json()


def decide(approval_id, **body):
    return client.post(f"/api/approvals/{approval_id}/decide", json=body)


def corrected_release_request(ctx):
    ctx.transport = ScriptedMuse()
    run = do("memo.check", {"memo": CORRECTED_MEMO, "filing": "anthropic"})
    assert run["status"] == "done", run["why"]
    asked = do("memo.release", {"check_run_id": run["run_id"]})
    assert asked["status"] == "needs_approval"
    return run, asked["approval"]


# The page and start data

def test_the_page_loads():
    assert client.get("/").status_code == 200


def test_start_has_the_contract_shape(monkeypatch):
    start = client.get("/api/start").json()
    assert set(start) == {"filings", "live", "max_memo", "operator_required", "score"}
    assert {f["id"] for f in start["filings"]} == {"anthropic", "nebius", "asml"}
    row = next(f for f in start["filings"] if f["id"] == "anthropic")
    assert set(row) == {"id", "company", "document", "sha256", "pages", "memo", "corrected", "conversation"} and row["memo"] == MEMO
    assert start["max_memo"] == 4000 and start["live"] is False and start["operator_required"] is False
    monkeypatch.setenv("MODEL_API_KEY", "not-used")
    monkeypatch.setenv("FINGUARD_OPERATOR_TOKEN", TOKEN)
    start = client.get("/api/start").json()
    assert start["live"] is True and start["operator_required"] is True


def test_start_reads_the_score_if_present(monkeypatch, tmp_path):
    (tmp_path / "eval").mkdir()
    monkeypatch.setattr(web, "ROOT", tmp_path)
    assert client.get("/api/start").json()["score"] is None
    (tmp_path / "eval" / "results_roundheldout.json").write_text("half written")
    assert client.get("/api/start").json()["score"] is None
    (tmp_path / "eval" / "results_roundheldout.json").write_text(json.dumps({"summary": {"round": "heldout"}}))
    assert client.get("/api/start").json()["score"] == {"round": "heldout", "repeat": None}


def test_page_images_are_served_with_bounds():
    good = client.get("/page/nebius/0.png")
    assert good.status_code == 200 and good.headers["content-type"] == "image/png"
    assert client.get("/page/anthropic/99999.png").status_code == 404
    assert client.get("/page/anthropic/-1.png").status_code == 404
    assert client.get("/page/nope/1.png").status_code == 404
    assert client.get("/page/..%2F..%2Fetc/1.png").status_code == 404
    assert client.get("/page/anthropic/abc.png").status_code == 400


def test_the_old_check_route_is_gone():
    assert client.post("/api/check", json={"memo": "x", "filing": "anthropic"}).status_code == 404


def test_rules_lists_capabilities():
    rules = client.get("/api/rules").json()["capabilities"]
    by_name = {r["name"]: r for r in rules}
    assert {"filings.list", "filings.page", "filings.add", "memo.check", "memo.release"} <= set(by_name)
    assert set(by_name["memo.release"]) == {"name", "summary", "kind", "approval", "verifies", "params"}
    assert by_name["memo.release"]["kind"] == "act" and by_name["memo.release"]["approval"] == "person"


def test_a_load_error_stops_the_server_in_plain_words():
    env = {**os.environ, "FINGUARD_LIVE_LIMIT": "lots"}
    done = subprocess.run([sys.executable, "-c", "import finguard.web"], env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 1 and "Traceback" not in done.stderr
    assert "Finguard cannot start: FINGUARD_LIVE_LIMIT must be a whole number." in done.stderr


# The door over the web

def test_do_has_the_contract_shape():
    body = do("filings.list")
    assert set(body) == {"status", "run_id", "result", "approval", "why"}
    assert body["status"] == "done" and body["result"]["filings"]


def test_the_saved_memo_replays_with_no_key():
    body = do("memo.check", {"memo": MEMO, "filing": "anthropic"})
    assert body["status"] == "done" and body["result"]["saved"] and body["result"]["record"]["holds"]
    verdicts = {r["value_text"]: r["verdict"] for r in body["result"]["receipts"]}
    assert verdicts["$8.7bn"] == "wrong" and verdicts["$16.3bn"] == "match"
    for r in body["result"]["receipts"]:
        assert MEMO[r["start"]:r["end"]] == r["value_text"]


def test_a_new_memo_needs_a_key():
    body = do("memo.check", {"memo": "Revenue was $5m.", "filing": "anthropic"})
    assert body["status"] == "refused" and "Live checks are off" in body["why"]


def test_live_checks_stop_at_the_limit(ctx, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "not-used")
    ctx.live.used = ctx.live.limit
    assert "limit" in do("memo.check", {"memo": "Revenue was $5m.", "filing": "anthropic"})["why"]


@pytest.mark.parametrize("params", [{"memo": "x", "filing": "../etc"}, {"memo": "  ", "filing": "anthropic"},
                                    {"memo": "x" * 4001, "filing": "anthropic"}, {"memo": "x", "filing": "nope"},
                                    {"memo": "x"}, {"memo": 5, "filing": "anthropic"}])
def test_bad_input_is_turned_away(params):
    assert do("memo.check", params)["status"] == "refused"


def test_unknown_capability_is_refused():
    assert do("no.such")["why"] == "No such capability."


@pytest.mark.parametrize("body", [{"params": "not an object"}, {"params": {}, "approval_id": "../../x"}, [1]])
def test_a_malformed_body_gets_a_plain_400(body):
    r = client.post("/api/do/filings.list", json=body)
    assert r.status_code == 400 and r.json() == {"detail": "That request is not in the right form."}


def test_an_acting_capability_only_asks():
    body = do("filings.add", {"url": "https://x.example.com/a.pdf"})
    assert body["status"] == "needs_approval" and body["approval"]["status"] == "pending"
    assert body["result"] is None


# Deciding

@pytest.fixture
def request_id():
    return do("filings.add", {"url": "https://x.example.com/a.pdf"})["approval"]["id"]


def test_with_no_token_configured_nobody_can_decide(request_id, ctx):
    assert decide(request_id, decision="approve").status_code == 403
    assert decide(request_id, decision="approve", operator_token="").status_code == 403
    assert decide(request_id, decision="approve", operator_token="guess").status_code == 403
    assert ctx.approvals.get(request_id)["status"] == "pending"


def test_an_empty_configured_token_still_means_nobody(request_id, monkeypatch):
    monkeypatch.setenv("FINGUARD_OPERATOR_TOKEN", "")
    assert decide(request_id, decision="approve", operator_token="").status_code == 403


def test_a_caller_with_the_wrong_or_no_token_cannot_decide(request_id, ctx, monkeypatch):
    monkeypatch.setenv("FINGUARD_OPERATOR_TOKEN", TOKEN)
    assert decide(request_id, decision="approve").status_code == 403
    assert decide(request_id, decision="approve", operator_token="wrong").status_code == 403
    assert decide(request_id, decision="approve", operator_token="éé").status_code == 403
    r = decide(request_id, decision="approve", operator_token="wrong")
    assert "operator" in r.json()["detail"] and ctx.approvals.get(request_id)["status"] == "pending"


def test_the_right_token_decides_and_returns_the_approval(request_id, ctx, monkeypatch):
    monkeypatch.setenv("FINGUARD_OPERATOR_TOKEN", TOKEN)
    r = decide(request_id, decision="approve", reason="Fine.", operator_token=TOKEN)
    assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["decided_by"] == "web_operator"
    line = ctx.ledger.lines()[-1]
    assert line["kind"] == "approval_decided" and TOKEN not in json.dumps(line)
    assert decide(request_id, decision="reject", reason="Too late.", operator_token=TOKEN).status_code == 409


def test_a_token_is_never_kept_or_echoed(request_id, ctx, monkeypatch):
    monkeypatch.setenv("FINGUARD_OPERATOR_TOKEN", TOKEN)
    decide(request_id, decision="approve", operator_token=TOKEN)
    assert not [p for p in ctx.state.rglob("*") if p.is_file() and TOKEN.encode() in p.read_bytes()]
    bad = client.post(f"/api/approvals/{request_id}/decide", json={"decision": 5, "operator_token": "mine-secret"})
    assert bad.status_code == 400 and "mine-secret" not in bad.text


def test_deciding_bad_requests(request_id, monkeypatch):
    monkeypatch.setenv("FINGUARD_OPERATOR_TOKEN", TOKEN)
    assert decide("apr_0000000000000000", decision="approve", operator_token=TOKEN).status_code == 404
    assert decide("..%2Fx", decision="approve", operator_token=TOKEN).status_code == 404
    assert decide(request_id, decision="maybe", operator_token=TOKEN).status_code == 409
    assert decide(request_id, decision="reject", operator_token=TOKEN).status_code == 409


def test_rejected_on_the_web_cannot_run(request_id, monkeypatch):
    monkeypatch.setenv("FINGUARD_OPERATOR_TOKEN", TOKEN)
    assert decide(request_id, decision="reject", reason="No.", operator_token=TOKEN).json()["status"] == "rejected"
    out = do("filings.add", {"url": "https://x.example.com/a.pdf"}, approval_id=request_id)
    assert out["status"] == "refused" and "rejected" in out["why"]


def test_the_approvals_list(request_id):
    rows = client.get("/api/approvals").json()["approvals"]
    assert [r["id"] for r in rows] == [request_id]
    assert rows[0]["status"] == "pending" and rows[0]["params"] == {"url": "https://x.example.com/a.pdf"}


# The record

def test_runs_list_and_detail():
    check = do("memo.check", {"memo": MEMO, "filing": "anthropic"})
    refused = do("no.such")
    rows = client.get("/api/runs").json()["runs"]
    assert [r["run_id"] for r in rows] == [refused["run_id"], check["run_id"]]
    assert set(rows[1]) == {"run_id", "capability", "status", "created_at", "counts", "filing"}
    assert rows[1]["capability"] == "memo.check" and rows[1]["filing"] == "anthropic" and rows[1]["counts"]["wrong"] == 2
    detail = client.get(f"/api/runs/{check['run_id']}").json()
    assert set(detail) == {"run_id", "capability", "status", "result", "record"}
    assert detail["status"] == "done" and detail["result"]["counts"]["match"] == 4
    assert detail["record"] == {"lines": check["result"]["record"]["lines"],
                                "last_hash": check["result"]["record"]["last_hash"], "holds": True}


def test_a_run_detail_shows_a_tampered_record(ctx):
    check = do("memo.check", {"memo": MEMO, "filing": "anthropic"})
    path = ctx.ledger.runs_folder / check["run_id"] / "run.jsonl"
    path.write_text(path.read_text().replace("$16.3bn", "$99.9bn"))
    assert client.get(f"/api/runs/{check['run_id']}").json()["record"]["holds"] is False


def test_an_unknown_run_is_404():
    assert client.get("/api/runs/run_0000000000000000").status_code == 404
    assert client.get("/api/runs/..%2F..%2Fetc").status_code == 404


def test_a_run_with_no_record_still_has_the_shape():
    detail = client.get(f"/api/runs/{do('filings.list')['run_id']}").json()
    assert detail["record"] == {"lines": 0, "last_hash": "", "holds": True} and detail["result"]["filings"]


def test_the_ledger_endpoint_and_a_tampered_ledger(ctx):
    do("filings.list")
    good = client.get("/api/ledger").json()
    assert set(good) == {"lines", "holds", "problems"} and good["holds"] is True and good["problems"] == []
    assert [e["kind"] for e in good["lines"]] == ["requested", "done"]
    assert len(client.get("/api/ledger?limit=1").json()["lines"]) == 1
    assert client.get("/api/ledger?limit=0").status_code == 400
    assert client.get("/api/ledger?limit=100000").status_code == 400
    ctx.ledger.path.write_text(ctx.ledger.path.read_text().replace('"web"', '"terminal"', 1))
    bad = client.get("/api/ledger").json()
    assert bad["holds"] is False and any("changed after" in p for p in bad["problems"])


def test_the_whole_flow_over_the_web(ctx, monkeypatch):
    monkeypatch.setenv("FINGUARD_OPERATOR_TOKEN", TOKEN)
    run, approval = corrected_release_request(ctx)
    assert decide(approval["id"], decision="approve", operator_token=TOKEN).status_code == 200
    out = do("memo.release", {"check_run_id": run["run_id"]}, approval_id=approval["id"])
    assert out["status"] == "done", out["why"]
    release_id = out["result"]["release_id"]
    shown = client.get(f"/api/releases/{release_id}").json()
    assert set(shown) == {"manifest", "memo", "receipts", "holds"}
    assert shown["holds"] is True and shown["memo"] == CORRECTED_MEMO
    assert shown["manifest"]["release_id"] == release_id and len(shown["receipts"]) == 6
    assert any(e.get("release_id") == release_id for e in client.get("/api/ledger").json()["lines"])
    (ctx.state / "releases" / release_id / "memo.txt").write_text("Changed.")
    assert client.get(f"/api/releases/{release_id}").json()["holds"] is False


def test_a_wrong_memo_cannot_be_released_over_the_web():
    run = do("memo.check", {"memo": MEMO, "filing": "anthropic"})
    out = do("memo.release", {"check_run_id": run["run_id"]})
    assert out["status"] == "refused" and "2 wrong" in out["why"]


def test_an_unknown_release_is_404():
    assert client.get("/api/releases/rel_0000000000000000").status_code == 404
    assert client.get("/api/releases/..%2F..%2Fledger").status_code == 404


# Marking numbers in the memo

def test_the_same_number_twice_gets_two_marks():
    memo = "Sales were $5m. Costs were $5m."
    rows = [{"quote": "Sales were $5m.", "value_text": "$5m"}, {"quote": "Costs were $5m.", "value_text": "$5m"}]
    placed = place_receipts(memo, rows)
    assert [r["start"] for r in placed] == [11, 27]


def test_an_unchecked_number_is_placed_too():
    placed = place_receipts("Margin was 10%.", [{"quote": "", "value_text": "10%"}])
    assert placed[0]["start"] == 11


def test_the_corrected_memo_replays_with_every_number_matching(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    memo = next(f for f in client.get("/api/start").json()["filings"] if f["id"] == "anthropic")["corrected"]
    out = client.post("/api/do/memo.check", json={"params": {"memo": memo, "filing": "anthropic"}}).json()
    assert out["status"] == "done" and out["result"]["saved"]
    assert out["result"]["counts"] == {"match": 6, "wrong": 0, "refused": 0, "unchecked": 0}
