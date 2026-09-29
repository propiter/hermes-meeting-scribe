"""``job_scope.owner_job_scope`` against fake hosts: capability detection for older Hermes versions."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from meeting_scribe.job_scope import owner_job_scope


def _host(monkeypatch, *, multiplex=True, bound=None, stamps=True, drop=()):
    calls: list = []
    mod = types.ModuleType("agent.secret_scope")
    mod.is_multiplex_active = lambda: multiplex
    mod.current_secret_scope = lambda: bound
    mod.build_profile_secret_scope = lambda home: {"FROM": str(home)}
    if stamps:
        def set_secret_scope(secrets, *, profile_home=None):
            calls.append(("set", secrets, profile_home))
            return "tok"
    else:
        def set_secret_scope(secrets):
            calls.append(("set", secrets, None))
            return "tok"
    mod.set_secret_scope = set_secret_scope
    mod.reset_secret_scope = lambda token: calls.append(("reset", token))
    for name in drop:
        delattr(mod, name)
    pkg = types.ModuleType("agent")
    pkg.secret_scope = mod
    monkeypatch.setitem(sys.modules, "agent", pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", mod)
    return calls


def test_binds_the_owner_home_and_resets_it(monkeypatch):
    calls = _host(monkeypatch)
    with owner_job_scope(lambda: Path("/owner")):
        assert calls == [("set", {"FROM": "/owner"}, "/owner")]
    assert calls[-1] == ("reset", "tok")


def test_resets_even_when_the_job_fails(monkeypatch):
    calls = _host(monkeypatch)
    with pytest.raises(RuntimeError), owner_job_scope(lambda: Path("/owner")):
        raise RuntimeError("stage failed")
    assert calls[-1] == ("reset", "tok")


def test_host_without_profile_home_stamp(monkeypatch):
    calls = _host(monkeypatch, stamps=False)
    with owner_job_scope(lambda: Path("/owner")):
        pass
    assert calls == [("set", {"FROM": "/owner"}, None), ("reset", "tok")]


@pytest.mark.parametrize("kw", [dict(multiplex=False), dict(bound={"X": "1"}), dict(drop=("is_multiplex_active",))])
def test_no_op_when_not_needed_or_not_supported(monkeypatch, kw):
    calls = _host(monkeypatch, **kw)
    with owner_job_scope(lambda: pytest.fail("the owner is only resolved when a scope is bound")):
        pass
    assert calls == []


def test_no_op_when_hermes_is_not_importable(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent", None)
    with owner_job_scope(lambda: pytest.fail("not resolved")):
        pass
