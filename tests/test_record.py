import json

import pytest

from finguard.errors import MuseError
from finguard.muse import Muse, ReplayTransport, image_part
from finguard.record import Run, read_lines, rebuild_request, sha256, verify
from tests.conftest import Scripted

PNG = b"\x89PNG not really"


def _call(run, reply=None):
    transport = Scripted(reply if reply is not None else {"ok": True})
    Muse(transport, run).ask("sys", [{"type": "text", "text": "q"}, image_part(PNG)], "S", {"type": "object"})
    return transport.sent[0]


def test_a_request_rebuilds_byte_for_byte(run):
    sent = _call(run)
    entry = read_lines(run.path)[0]
    assert rebuild_request(entry, run.folder) == sent
    assert entry["request_sha256"] == sha256(sent)
    assert "base64" not in entry["request"]
    assert verify(run.folder) == []


def test_changing_a_line_breaks_the_chain(run):
    _call(run)
    run.write("receipt", verdict="wrong")
    lines = run.path.read_text().splitlines()
    run.path.write_text("\n".join([lines[0], lines[1].replace("wrong", "match")]) + "\n")
    assert any("changed after" in p for p in verify(run.folder))


def test_removing_a_line_breaks_the_chain(run):
    _call(run)
    run.write("receipt", verdict="wrong")
    lines = run.path.read_text().splitlines()
    run.path.write_text(lines[1] + "\n")
    assert verify(run.folder)


def test_a_missing_page_image_is_reported(run):
    _call(run)
    next((run.folder / "images").glob("*.png")).unlink()
    assert any("missing" in p for p in verify(run.folder))


def test_a_failed_call_is_still_on_the_record(run):
    with pytest.raises(MuseError):
        _call(run, MuseError("timeout", "slow"))
    entry = read_lines(run.path)[0]
    assert entry["error"] == "timeout" and entry["reply"] is None


def test_reopening_a_run_continues_the_chain(run):
    _call(run)
    Run(run.folder).write("note")
    assert verify(run.folder) == []


def test_replay_fails_on_an_unknown_request(tmp_path, run):
    muse = Muse(ReplayTransport(tmp_path), run)
    with pytest.raises(MuseError) as e:
        muse.ask("s", [{"type": "text", "text": "q"}], "S", {"type": "object"})
    assert e.value.code == "no_recording"


def test_replay_notices_a_saved_reply_nobody_asked_for(tmp_path):
    (tmp_path / "abc.json").write_text(json.dumps({}))
    with pytest.raises(AssertionError):
        ReplayTransport(tmp_path).assert_all_used()


@pytest.mark.parametrize("reply", [b"not json", b'{"choices": []}', b'{"choices":[{"message":{"content":"plain words"}}]}'])
def test_a_garbled_reply_is_named_not_used(run, reply):
    with pytest.raises(MuseError) as e:
        Muse(Scripted(reply), run).ask("s", [{"type": "text", "text": "q"}], "S", {"type": "object"})
    assert e.value.code == "bad_reply"


def test_too_many_images_never_reaches_the_api(run):
    transport = Scripted()
    with pytest.raises(MuseError) as e:
        Muse(transport, run).ask("s", [image_part(PNG)] * 51, "S", {"type": "object"})
    assert e.value.code == "too_large" and transport.sent == []
