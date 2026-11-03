"""The `finguard` command. The terminal counts as an operator."""
import json
import tomllib
from pathlib import Path

import pytest

from finguard import cli
from finguard.core.plugin import LoadError
from finguard.core.context import saved_memo
from tests.test_plugins_support import CORRECTED_MEMO, build

ROOT = Path(__file__).parent.parent


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    made = build(tmp_path)
    monkeypatch.setattr(cli, "_ctx", lambda: made)
    return made


def run(capsys, *args):
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def do(capsys, *args):
    code, out, _ = run(capsys, "do", *args)
    return code, json.loads(out)


def test_rules_lists_every_capability_and_how_it_is_verified(ctx, capsys):
    code, out, _ = run(capsys, "rules")
    assert code == 0
    for name in ("filings.list", "filings.page", "filings.add", "memo.check", "memo.release"):
        assert name in out
    assert "approval: person" in out and "Verified:" in out


def test_do_takes_numbers_by_the_schema_and_hides_the_image(ctx, capsys):
    code, body = do(capsys, "filings.page", "filing=nebius", "page=0")
    assert code == 0 and body["status"] == "done" and "not shown" in body["result"]["png_base64"]


def test_do_reports_refusals_with_a_failing_exit(ctx, capsys):
    code, body = do(capsys, "no.such")
    assert code == 1 and body["status"] == "refused" and body["why"] == "No such capability."
    code, body = do(capsys, "filings.page", "filing=nebius", "page=99999")
    assert code == 1 and "No such page" in body["why"]


def test_do_explains_a_bad_pair_or_number_or_file(ctx, capsys):
    assert run(capsys, "do", "filings.list", "oops")[0] == 2
    assert "must be a number" in run(capsys, "do", "filings.page", "filing=nebius", "page=two")[2]
    assert "Could not read" in run(capsys, "do", "memo.check", "memo=@/no/such/file", "filing=nebius")[2]


def test_a_memo_can_come_from_a_file(ctx, capsys):
    code, body = do(capsys, "memo.check", f"memo=@{ROOT / 'examples' / 'anthropic_memo.txt'}", "filing=anthropic")
    assert code == 0 and body["result"]["saved"] and body["result"]["memo"] == saved_memo("anthropic")


def test_the_whole_flow_from_the_terminal(ctx, capsys, tmp_path):
    memo = tmp_path / "memo.txt"
    memo.write_text(CORRECTED_MEMO)
    _, check = do(capsys, "memo.check", f"memo=@{memo}", "filing=anthropic")
    run_id = check["run_id"]
    code, asked = do(capsys, "memo.release", f"check_run_id={run_id}")
    assert code == 0 and asked["status"] == "needs_approval"
    approval = asked["approval"]["id"]

    code, out, _ = run(capsys, "approvals")
    assert code == 0 and approval in out and "pending" in out

    code, out, _ = run(capsys, "approve", approval, "--reason", "Checked by hand.")
    assert code == 0 and "approved" in out
    assert ctx.approvals.get(approval)["decided_by"] == "terminal"

    code, done = do(capsys, "memo.release", f"check_run_id={run_id}", "--approval", approval)
    assert code == 0 and done["status"] == "done"
    release_id = done["result"]["release_id"]

    code, out, _ = run(capsys, "release", "verify", release_id)
    assert (code, out.strip()) == (0, "Holds.")
    code, out, _ = run(capsys, "ledger", "verify")
    assert (code, out.strip()) == (0, "Holds.")

    code, again = do(capsys, "memo.release", f"check_run_id={run_id}", "--approval", approval)
    assert code == 1 and "already been used" in again["why"]

    bundle = ctx.state / "releases" / release_id / "memo.txt"
    bundle.write_text("Changed.")
    code, out, _ = run(capsys, "release", "verify", release_id)
    assert code == 1 and "memo.txt was changed" in out


def test_reject_needs_a_reason_and_is_final(ctx, capsys):
    _, asked = do(capsys, "filings.add", "url=https://x.example.com/a.pdf")
    approval = asked["approval"]["id"]
    with pytest.raises(SystemExit):
        cli.main(["reject", approval])
    capsys.readouterr()
    assert run(capsys, "reject", approval, "--reason", "Not a filing we use.")[0] == 0
    code, _, err = run(capsys, "approve", approval)
    assert code == 1 and "already rejected" in err


def test_deciding_an_unknown_request_is_a_plain_error(ctx, capsys):
    code, _, err = run(capsys, "approve", "apr_0000000000000000")
    assert code == 1 and err.strip() == "No such approval."


def test_no_requests_says_so(ctx, capsys):
    assert "No approval requests." in run(capsys, "approvals")[1]


def test_ledger_shows_lines_with_a_limit(ctx, capsys):
    do(capsys, "filings.list")
    do(capsys, "no.such")
    code, out, _ = run(capsys, "ledger")
    assert code == 0 and len(out.strip().splitlines()) == 4 and "requested" in out and "No such capability." in out
    assert len(run(capsys, "ledger", "--limit", "1")[1].strip().splitlines()) == 1


def test_a_tampered_ledger_fails_verify(ctx, capsys):
    do(capsys, "filings.list")
    ctx.ledger.path.write_text(ctx.ledger.path.read_text().replace("terminal", "web", 1))
    code, out, _ = run(capsys, "ledger", "verify")
    assert code == 1 and "Does not hold" in out and "changed after" in out


def test_verifying_an_unknown_release_fails(ctx, capsys):
    code, out, _ = run(capsys, "release", "verify", "rel_0000000000000000")
    assert code == 1 and "No such release" in out


def test_a_broken_plugin_stops_the_command_in_plain_words(monkeypatch, capsys):
    def broken():
        raise LoadError("a.act acts, so a person must approve it.")
    monkeypatch.setattr(cli, "_ctx", broken)
    code, out, err = run(capsys, "rules")
    assert code == 2 and err.strip() == "Finguard cannot start: a.act acts, so a person must approve it." and not out


def test_the_script_entry_is_declared():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["scripts"]["finguard"] == "finguard.cli:main"
    assert {"finguard.core", "finguard.plugins"} <= set(project["tool"]["setuptools"]["packages"])
