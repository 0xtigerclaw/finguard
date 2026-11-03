"""The real capabilities: filings, memo.check and memo.release, with no API and no network."""
import base64
import hashlib
import json
import shutil
import socket
from dataclasses import replace

import httpx
import pymupdf
import pytest

from finguard.core.context import MAX_MEMO, Context, saved_memo
from finguard.core.door import dispatch
from finguard.core.plugin import Refused
from finguard.errors import MuseError
from finguard.plugins import filings as filings_plugin
from finguard.plugins.release import read_bundle, verify_release
from finguard.record import read_lines
from tests.test_plugins_support import CORRECTED_MEMO, ScriptedMuse, build, check_corrected, rechain


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    return build(tmp_path)


def approve(ctx, approval_id):
    ctx.approvals.decide(approval_id, "approve", "terminal", "looks right")


def released(ctx):
    run = check_corrected(ctx)
    asked = dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx)
    assert asked.status == "needs_approval", asked.why
    approve(ctx, asked.approval["id"])
    out = dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx, approval_id=asked.approval["id"])
    assert out.status == "done", out.why
    return run, asked.approval, out


# filings.list and filings.page

def test_the_list_has_fingerprints(ctx):
    out = dispatch("filings.list", {}, "web", ctx)
    rows = {f["id"]: f for f in out.result["filings"]}
    assert set(rows) == {"anthropic", "nebius", "asml"}
    assert len(rows["anthropic"]["sha256"]) == 64 and rows["anthropic"]["pages"] > 300
    assert rows["anthropic"]["memo"] == saved_memo("anthropic")
    sources = json.loads((ctx.filings.get("anthropic").path.parent / "sources.json").read_text())
    assert rows["anthropic"]["sha256"] == sources["ANTH-S1-2026.pdf"]["sha256"]


def test_a_page_comes_back_as_an_image(ctx):
    out = dispatch("filings.page", {"filing": "nebius", "page": 0}, "web", ctx)
    png = base64.b64decode(out.result["png_base64"])
    assert png.startswith(b"\x89PNG") and hashlib.sha256(png).hexdigest() == out.result["image_sha256"]
    assert not (ctx.ledger.runs_folder / out.run_id / "outcome.json").exists()  # too big to keep


@pytest.mark.parametrize("params", [{"filing": "nebius", "page": 99999}, {"filing": "nope", "page": 0},
                                    {"filing": "../etc", "page": 0}, {"filing": "nebius", "page": -1},
                                    {"filing": "nebius"}])
def test_bad_page_requests_are_refused(ctx, params):
    assert dispatch("filings.page", params, "web", ctx).status == "refused"


# filings.add

def _pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Revenue was 5 in 2026.")
    return doc.tobytes()


def _net(responses: dict):
    """A stand-in for the network, and a public address for every host."""
    def handler(request):
        reply = responses[str(request.url)]
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, content=reply)
    return httpx.MockTransport(handler)


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])


URL = "https://filings.example.com/a.pdf"


def _add(ctx, params=None):
    params = params or {"url": URL, "company": "Example", "document": "Annual report"}
    asked = dispatch("filings.add", params, "web", ctx)
    assert asked.status == "needs_approval", asked.why
    approve(ctx, asked.approval["id"])
    return dispatch("filings.add", params, "web", ctx, approval_id=asked.approval["id"])


def test_a_pdf_is_added_only_after_approval(ctx, public_dns):
    fetched = []
    data = _pdf()

    def handler(request):
        fetched.append(str(request.url))
        return httpx.Response(200, content=data)

    ctx.http = httpx.MockTransport(handler)
    asked = dispatch("filings.add", {"url": URL}, "web", ctx)
    assert asked.status == "needs_approval" and not fetched and not ctx.filings.folder.exists()
    approve(ctx, asked.approval["id"])
    out = dispatch("filings.add", {"url": URL}, "web", ctx, approval_id=asked.approval["id"])
    assert out.status == "done" and fetched == [URL]
    sha = hashlib.sha256(data).hexdigest()
    assert out.result["sha256"] == sha and out.result["id"] == f"f-{sha[:12]}" and out.result["pages"] == 1
    assert any(e["kind"] == "filing_added" and e["sha256"] == sha for e in ctx.ledger.lines())
    rows = {f["id"] for f in dispatch("filings.list", {}, "web", ctx).result["filings"]}
    assert out.result["id"] in rows
    assert dispatch("filings.page", {"filing": out.result["id"], "page": 0}, "web", ctx).status == "done"
    assert dispatch("filings.add", {"url": URL}, "web", ctx, approval_id=asked.approval["id"]).status == "refused"
    assert ctx.ledger.verify() == []


@pytest.mark.parametrize("url", ["http://filings.example.com/a.pdf", "file:///etc/passwd", "ftp://x/a.pdf", "not a url",
                                 "https://user:pw@filings.example.com/a.pdf", "https:///a.pdf"])
def test_only_plain_https_urls_can_even_ask(ctx, url):
    out = dispatch("filings.add", {"url": url}, "web", ctx)
    assert out.status == "refused" and ctx.approvals.all() == []


def test_not_a_pdf_is_refused(ctx, public_dns):
    ctx.http = _net({URL: b"<html>hello</html>"})
    out = _add(ctx)
    assert out.status == "refused" and "not a PDF" in out.why and not list(ctx.filings.folder.glob("*"))


def test_a_pdf_header_on_junk_is_refused(ctx, public_dns):
    ctx.http = _net({URL: b"%PDF-1.7 and then nothing useful"})
    out = _add(ctx)
    assert out.status == "refused" and not list(ctx.filings.folder.glob("*"))


def test_over_the_size_limit_is_refused(ctx, public_dns, monkeypatch):
    monkeypatch.setattr(filings_plugin, "MAX_BYTES", 1000)
    ctx.http = _net({URL: b"%PDF-" + b"x" * 2000})
    out = _add(ctx)
    assert out.status == "refused" and "30 MB" in out.why


def test_the_real_limit_is_30_mb():
    assert filings_plugin.MAX_BYTES == 30 * 1024 * 1024


def test_a_pinned_hash_must_match(ctx, public_dns):
    ctx.http = _net({URL: _pdf()})
    out = _add(ctx, {"url": URL, "sha256": "0" * 64})
    assert out.status == "refused" and "hash" in out.why and not list(ctx.filings.folder.glob("*"))


def test_a_private_address_is_refused_even_after_approval(ctx, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("169.254.169.254", 443))])
    ctx.http = _net({URL: _pdf()})
    out = _add(ctx)
    assert out.status == "refused" and "private" in out.why


def test_a_redirect_to_http_is_refused(ctx, public_dns):
    ctx.http = _net({URL: httpx.Response(302, headers={"location": "http://evil.example.com/a.pdf"})})
    out = _add(ctx)
    assert out.status == "refused" and "https" in out.why


def test_a_good_redirect_is_followed(ctx, public_dns):
    other = "https://cdn.example.com/b.pdf"
    ctx.http = _net({URL: httpx.Response(302, headers={"location": other}), other: _pdf()})
    assert _add(ctx).status == "done"


def test_a_redirect_loop_fails(ctx, public_dns):
    ctx.http = _net({URL: httpx.Response(302, headers={"location": URL})})
    out = _add(ctx)
    assert out.status == "failed" and "redirected" in out.why


def test_a_server_error_fails_plainly(ctx, public_dns):
    ctx.http = _net({URL: httpx.Response(500)})
    out = _add(ctx)
    assert out.status == "failed" and "500" in out.why


def test_a_network_failure_fails_plainly(ctx, public_dns):
    def handler(request):
        raise httpx.ConnectError("secret detail")
    ctx.http = httpx.MockTransport(handler)
    out = _add(ctx)
    assert out.status == "failed" and "secret" not in out.why


def test_a_filename_from_the_server_is_never_used(ctx, public_dns):
    ctx.http = _net({"https://x.example.com/..%2F..%2Fevil.pdf": _pdf()})
    out = _add(ctx, {"url": "https://x.example.com/..%2F..%2Fevil.pdf"})
    assert out.status == "done" and sorted(p.suffix for p in ctx.filings.folder.iterdir()) == [".json", ".pdf"]
    assert out.result["id"].startswith("f-")


# memo.check

def test_the_saved_memo_replays_with_no_key_and_no_api(ctx):
    ctx.transport = None  # would raise if a live call were tried, as no key is set
    out = dispatch("memo.check", {"memo": saved_memo("anthropic"), "filing": "anthropic"}, "web", ctx)
    assert out.status == "done" and out.result["saved"] is True
    assert {r["value_text"]: r["verdict"] for r in out.result["receipts"]} == {
        "$16.3bn": "match", "$1.6bn": "match", "$693m": "wrong", "$8.7bn": "wrong", "$2.2bn": "match", "10%": "match"}
    assert out.result["counts"] == {"match": 4, "wrong": 2, "refused": 0, "unchecked": 0}
    assert out.result["record"]["holds"] and out.result["filing"] == "anthropic"
    for r in out.result["receipts"]:
        assert out.result["memo"][r["start"]:r["end"]] == r["value_text"]
        assert {"verdict", "why", "value_text", "quote", "metric", "filing_sha256", "page", "box", "muse_box",
                "source_text", "source_money", "row", "column", "start", "end"} <= set(r)
    folder = ctx.ledger.runs_folder / out.run_id
    assert (folder / "run.jsonl").exists() and read_lines(folder / "run.jsonl")[-1]["hash"] == out.result["record"]["last_hash"]
    line = next(e for e in ctx.ledger.lines() if e["kind"] == "done" and e["run_id"] == out.run_id)
    assert line["run_last_hash"] == out.result["record"]["last_hash"]
    assert ctx.ledger.verify() == []


def test_repeat_runs_keep_each_page_image_once_and_still_verify(ctx):
    params = {"memo": saved_memo("anthropic"), "filing": "anthropic"}
    a = dispatch("memo.check", params, "web", ctx)
    b = dispatch("memo.check", params, "web", ctx)
    one, two = [ctx.ledger.runs_folder / o.run_id / "images" for o in (a, b)]
    names = sorted(p.name for p in one.glob("*.png"))
    assert names and names == sorted(p.name for p in two.glob("*.png"))
    assert all((one / n).stat().st_ino == (two / n).stat().st_ino for n in names)
    assert ctx.ledger.verify() == []


@pytest.mark.parametrize("memo, words", [("   ", "Paste"), ("x" * (MAX_MEMO + 1), "under 4,000")])
def test_memo_limits(ctx, memo, words):
    out = dispatch("memo.check", {"memo": memo, "filing": "anthropic"}, "web", ctx)
    assert out.status == "refused" and words in out.why


def test_a_memo_at_the_cap_is_allowed_through_to_the_model(ctx):
    calls = ctx.transport
    calls.send = lambda body: json.dumps({"choices": [{"message": {"content": json.dumps({"claims": []})}}]}).encode()
    assert dispatch("memo.check", {"memo": "x" * MAX_MEMO, "filing": "anthropic"}, "web", ctx).status == "done"


def test_an_unknown_filing_is_refused(ctx):
    out = dispatch("memo.check", {"memo": "Revenue was $5m.", "filing": "nope"}, "web", ctx)
    assert out.status == "refused" and "No such filing" in out.why


def test_a_new_memo_needs_a_key(ctx):
    ctx.transport = None
    out = dispatch("memo.check", {"memo": "Revenue was $5m.", "filing": "anthropic"}, "web", ctx)
    assert out.status == "refused" and "Live is off" in out.why


def test_live_checks_stop_at_the_limit(ctx, monkeypatch):
    ctx.transport = None
    monkeypatch.setenv("MODEL_API_KEY", "not-used")
    ctx.live.used = ctx.live.limit
    out = dispatch("memo.check", {"memo": "Revenue was $5m.", "filing": "anthropic"}, "web", ctx)
    assert out.status == "refused" and "limit" in out.why


def test_the_limit_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("FINGUARD_LIVE_LIMIT", "3")
    assert Context.build(state=tmp_path).live.limit == 3


def test_a_failed_model_call_is_failed_not_a_verdict(ctx):
    def down(body):
        raise MuseError("unavailable", "raw provider text")
    ctx.transport.send = down
    out = dispatch("memo.check", {"memo": "Revenue was $5m.", "filing": "anthropic"}, "web", ctx)
    assert out.status == "failed" and "could not be reached" in out.why and "raw provider" not in out.why
    assert dispatch("memo.release", {"check_run_id": out.run_id}, "web", ctx).status == "refused"


# memo.release

def test_a_wrong_receipt_is_refused_before_any_approval(ctx):
    run = dispatch("memo.check", {"memo": saved_memo("anthropic"), "filing": "anthropic"}, "web", ctx)
    out = dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx)
    assert out.status == "refused" and "2 wrong" in out.why and ctx.approvals.all() == []


def test_an_unchecked_receipt_is_refused(ctx):
    memo = CORRECTED_MEMO + " Headcount rose by 37."  # Muse does not list this number, so it comes back unchecked
    run = dispatch("memo.check", {"memo": memo, "filing": "anthropic"}, "web", ctx)
    assert run.result["counts"]["unchecked"] == 1
    out = dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx)
    assert out.status == "refused" and "1 unchecked" in out.why and ctx.approvals.all() == []


def test_a_refused_receipt_is_refused(ctx):
    answer = ScriptedMuse.send

    def not_found_for_revenue(body):
        if b"Metric: Revenue" in body:
            return json.dumps({"choices": [{"message": {"content": json.dumps(
                {"found": False, "page": 0, "box": [], "row_label": ""})}}]}).encode()
        return answer(ctx.transport, body)

    ctx.transport.send = not_found_for_revenue
    run = dispatch("memo.check", {"memo": CORRECTED_MEMO, "filing": "anthropic"}, "web", ctx)
    assert run.result["counts"]["refused"] == 1
    out = dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx)
    assert out.status == "refused" and "1 refused" in out.why and ctx.approvals.all() == []


def test_a_check_with_no_numbers_has_nothing_to_release(ctx):
    ctx.transport.send = lambda body: json.dumps({"choices": [{"message": {"content": json.dumps({"claims": []})}}]}).encode()
    run = dispatch("memo.check", {"memo": "No numbers here.", "filing": "anthropic"}, "web", ctx)
    assert dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx).status == "refused"


@pytest.mark.parametrize("run_id", ["run_0000000000000000", "../../etc", "x" * 65, ""])
def test_a_run_that_does_not_exist_is_refused(ctx, run_id):
    assert dispatch("memo.release", {"check_run_id": run_id}, "web", ctx).status == "refused"


def test_a_run_that_is_not_a_check_is_refused(ctx):
    other = dispatch("filings.list", {}, "web", ctx)
    assert dispatch("memo.release", {"check_run_id": other.run_id}, "web", ctx).status == "refused"


def test_the_whole_flow_check_ask_approve_release_verify(ctx):
    run, approval, out = released(ctx)
    assert ctx.approvals.get(approval["id"])["status"] == "used"
    release_id = out.result["release_id"]
    assert out.result["holds"] is True and verify_release(ctx, release_id) == []
    folder = ctx.state / "releases" / release_id
    assert sorted(p.name for p in folder.iterdir()) == ["manifest.json", "memo.txt", "receipts.json", "run.jsonl"]
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["release_id"] == release_id and manifest["check_run_id"] == run.run_id
    assert manifest["approved_by"] == "terminal" and manifest["subject_sha256"] == approval["subject_sha256"]
    for name in ("memo.txt", "receipts.json", "run.jsonl"):
        assert manifest["files"][name] == hashlib.sha256((folder / name).read_bytes()).hexdigest()
    assert (folder / "memo.txt").read_text() == CORRECTED_MEMO
    assert all(r["verdict"] == "match" for r in json.loads((folder / "receipts.json").read_text()))
    assert ctx.ledger.verify() == []
    shown = read_bundle(ctx, release_id)
    assert shown["holds"] and shown["memo"] == CORRECTED_MEMO
    assert all(CORRECTED_MEMO[r["start"]:r["end"]] == r["value_text"] for r in shown["receipts"])


def test_a_release_approval_cannot_run_twice(ctx):
    run, approval, _ = released(ctx)
    again = dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx, approval_id=approval["id"])
    assert again.status == "refused" and "already been used" in again.why
    assert len(list((ctx.state / "releases").iterdir())) == 1


def test_a_rejected_release_does_not_run(ctx):
    run = check_corrected(ctx)
    asked = dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx)
    ctx.approvals.decide(asked.approval["id"], "reject", "terminal", "not yet")
    out = dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx, approval_id=asked.approval["id"])
    assert out.status == "refused" and not (ctx.state / "releases").exists()


def _flip_receipt_to_wrong(entry):
    if entry["kind"] == "receipt" and entry["value_text"] == "$693m":
        entry["verdict"] = "wrong"


def _change_memo(entry):
    if entry["kind"] == "start":
        entry["memo"] = entry["memo"].replace("$16.3bn", "$99.9bn")


def _change_receipt_why(entry):
    if entry["kind"] == "receipt":
        entry["why"] = "Changed."


@pytest.mark.parametrize("change", [_flip_receipt_to_wrong, _change_memo, _change_receipt_why])
def test_an_approval_dies_if_the_memo_or_receipts_change_even_with_a_fixed_chain(ctx, change):
    run = check_corrected(ctx)
    asked = dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx)
    approve(ctx, asked.approval["id"])
    rechain(ctx.ledger.runs_folder / run.run_id / "run.jsonl", change)
    out = dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx, approval_id=asked.approval["id"])
    assert out.status == "refused" and not (ctx.state / "releases").exists()
    assert ctx.approvals.get(asked.approval["id"])["status"] == "approved"


def test_a_simple_edit_to_the_record_is_refused(ctx):
    run = check_corrected(ctx)
    path = ctx.ledger.runs_folder / run.run_id / "run.jsonl"
    path.write_text(path.read_text().replace("$16.3bn", "$99.9bn"))
    out = dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx)
    assert out.status == "refused" and "does not hold" in out.why


def test_a_cut_record_is_refused(ctx):
    run = check_corrected(ctx)
    path = ctx.ledger.runs_folder / run.run_id / "run.jsonl"
    path.write_text("\n".join(path.read_text().splitlines()[:-1]) + "\n")
    assert dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx).status == "refused"
    assert any("does not end" in p for p in ctx.ledger.verify())


def test_the_run_step_refuses_again_even_if_the_door_was_passed(ctx):
    """The second check, inside memo.release itself, with the door's check stepped around."""
    run = check_corrected(ctx)
    asked = dispatch("memo.release", {"check_run_id": run.run_id}, "web", ctx)
    approve(ctx, asked.approval["id"])
    cap = ctx.registry.get("memo.release")
    # The record goes bad after the door's subject check and before run().
    rechain(ctx.ledger.runs_folder / run.run_id / "run.jsonl", _flip_receipt_to_wrong)
    call = replace(ctx, run_id="run_x", actor="web", approval=ctx.approvals.get(asked.approval["id"]))
    with pytest.raises(Refused):
        cap.run(call, {"check_run_id": run.run_id})
    with pytest.raises(Refused):  # and no approval at all
        cap.run(replace(ctx, run_id="run_x", actor="web", approval=None), {"check_run_id": run.run_id})


@pytest.mark.parametrize("name", ["memo.txt", "receipts.json", "run.jsonl", "manifest.json"])
def test_a_changed_bundle_file_is_caught(ctx, name):
    _, _, out = released(ctx)
    release_id = out.result["release_id"]
    path = ctx.state / "releases" / release_id / name
    path.write_bytes(path.read_bytes().replace(b"$16.3bn", b"$99.9bn").replace(b'"approved_by"', b'"approved_By"'))
    assert verify_release(ctx, release_id)


@pytest.mark.parametrize("name", ["memo.txt", "receipts.json", "run.jsonl", "manifest.json"])
def test_a_missing_bundle_file_is_caught(ctx, name):
    _, _, out = released(ctx)
    (ctx.state / "releases" / out.result["release_id"] / name).unlink()
    assert verify_release(ctx, out.result["release_id"])


def test_a_bundle_rebuilt_with_fresh_hashes_is_still_caught(ctx):
    _, _, out = released(ctx)
    release_id = out.result["release_id"]
    folder = ctx.state / "releases" / release_id
    (folder / "memo.txt").write_text(CORRECTED_MEMO.replace("$16.3bn", "$99.9bn"))
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["files"]["memo.txt"] = hashlib.sha256((folder / "memo.txt").read_bytes()).hexdigest()
    (folder / "manifest.json").write_text(json.dumps(manifest))
    problems = verify_release(ctx, release_id)
    assert any("not the memo" in p for p in problems) and any("after it was sealed" in p for p in problems)


def test_an_extra_file_in_a_bundle_is_caught(ctx):
    _, _, out = released(ctx)
    (ctx.state / "releases" / out.result["release_id"] / "extra.txt").write_text("x")
    assert verify_release(ctx, out.result["release_id"])


def test_a_copy_of_a_bundle_under_a_new_id_is_caught(ctx):
    _, _, out = released(ctx)
    home = ctx.state / "releases"
    shutil.copytree(home / out.result["release_id"], home / "rel_0000000000000000")
    assert verify_release(ctx, "rel_0000000000000000")


@pytest.mark.parametrize("release_id", ["rel_0000000000000000", "../ledger", ""])
def test_an_unknown_release_is_reported(ctx, release_id):
    assert verify_release(ctx, release_id) == ["No such release."]
