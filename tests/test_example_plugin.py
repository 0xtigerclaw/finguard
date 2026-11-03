"""The plugin the README shows must really load, run and obey the load-time rules."""
import shutil
import sys
from pathlib import Path

import pytest

from finguard.core.context import Context
from finguard.core.door import dispatch
from finguard.core.plugin import Capability, LoadError, Registry

EXAMPLE = Path(__file__).parent.parent / "examples" / "plugins" / "net_debt.py"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    package = tmp_path / "myplugins"
    package.mkdir()
    (package / "__init__.py").write_text("")
    shutil.copy(EXAMPLE, package / "net_debt.py")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield Context.build(tmp_path / "state", package="myplugins")
    sys.modules.pop("myplugins", None)
    sys.modules.pop("myplugins.net_debt", None)


def test_dropped_in_plugin_loads_and_works_out_net_debt(ctx):
    out = dispatch("desk.net_debt", {"filing": "nebius", "page": 5, "as_of": "June 30, 2026"}, "test", ctx)
    assert out.status == "done" and out.result["net_debt_in_millions"] == "503.6"
    assert [c["printed"] for c in out.result["read"]] == ["46.7", "8,499.0", "8,042.1"]


def test_it_refuses_rather_than_guesses(ctx):
    out = dispatch("desk.net_debt", {"filing": "nebius", "page": 6, "as_of": "June 30, 2026"}, "test", ctx)
    assert out.status == "refused" and "nothing was worked out" in out.why


def test_a_plugin_that_acts_with_no_approval_cannot_load():
    with pytest.raises(LoadError, match="must approve"):
        Registry().add(Capability(name="desk.wire", summary="s", kind="act", approval="none", verifies="v",
                                  params={"type": "object"}, run=lambda c, p: {}))
