"""``register(ctx)`` against a fake PluginContext (the real loader is covered by integration tests)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting_scribe import plugin

ROOT = Path(__file__).resolve().parents[2]


class FakeCtx:
    def __init__(self, tmp_path, config=None):
        self.cfg = dict(config or {})
        self.tools, self.commands, self.cli, self.skills, self.aux = {}, {}, {}, {}, {}
        self.llm = SimpleNamespace()
        self.plugin_id = "meeting-scribe"
        self.platform_handlers, self.unload = {}, []

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def set_config(self, key, value):
        self.cfg[key] = value

    def register_auxiliary_task(self, key, *, display_name, description, defaults=None):
        self.aux[key] = display_name

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (toolset, schema, handler)

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = (handler, args_hint)

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = (setup_fn, handler_fn)

    def register_skill(self, name, path, description="", frontmatter=None):
        assert Path(path).exists()
        self.skills[name] = path

    def call_mcp(self, server, tool, arguments=None, timeout=30):
        raise PermissionError("not allowlisted")

    def register_platform_handler(self, platform, factory):
        self.platform_handlers.setdefault(platform, []).append(factory)

    def on_unload(self, callback):
        self.unload.append(callback)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    c = FakeCtx(tmp_path)
    monkeypatch.setattr(plugin, "_host_overrides", lambda: {"data_dir": lambda: tmp_path / "data",
                                                           "secret": lambda name: None,
                                                           "mcp_allowed": lambda: False})
    yield c
    rt = plugin.RUNTIMES.pop(id(c), None)
    if rt is not None:
        rt.close()


def test_register_wires_everything(ctx):
    rt = plugin.register(ctx, ROOT)
    assert ctx.aux == {"meeting_scribe": "Meeting Scribe"}
    assert set(ctx.tools) == {"meeting_search", "meeting_get"}
    assert all(v[0] == "meeting_scribe" for v in ctx.tools.values())
    assert set(ctx.commands) == {"meeting", "meet", "rec"}
    assert ctx.commands["meeting"][1]  # args hint so Discord shows an argument field
    assert "meeting-scribe" in ctx.cli and set(ctx.skills) == {"meeting-scribe"}


def test_register_installs_live_capture_when_discord_is_available(ctx):
    pytest.importorskip("discord")
    rt = plugin.register(ctx, ROOT)
    assert rt.capture is not None and hasattr(rt.capture, "start")  # Phase B installed
    assert len(ctx.platform_handlers["discord"]) == 1 and len(ctx.unload) == 3
    assert any(getattr(s, "name", "") == "discord" for s in rt.sinks())


def test_register_degrades_without_discord(ctx, monkeypatch):
    """CI/CLI hosts without discord.py: tools, commands and CLI still register; capture is off."""
    monkeypatch.setitem(sys.modules, "discord", None)  # makes `import discord` raise ImportError
    for mod in [m for m in sys.modules if m.startswith("meeting_scribe.discord_ui")]:
        monkeypatch.delitem(sys.modules, mod)  # force a fresh import that hits the missing discord
    rt = plugin.register(ctx, ROOT)
    assert set(ctx.tools) == {"meeting_search", "meeting_get"} and "meeting-scribe" in ctx.cli
    assert not ctx.platform_handlers.get("discord") and rt is not None


def test_aliases_from_config(tmp_path, monkeypatch):
    c = FakeCtx(tmp_path, {"commands_aliases": ["notes-rec", "Meeting", "bad name!"]})
    monkeypatch.setattr(plugin, "_host_overrides", lambda: {"data_dir": lambda: tmp_path / "d",
                                                           "secret": lambda name: None,
                                                           "mcp_allowed": lambda: False})
    plugin.register(c, ROOT)
    assert set(c.commands) == {"meeting", "notes-rec"}
    plugin.RUNTIMES.pop(id(c)).close()


def test_command_handler_uses_session_caller(ctx, monkeypatch):
    plugin.register(ctx, ROOT)
    monkeypatch.setattr(plugin, "caller_from_session", lambda: plugin.Caller("discord", "1", "2"))
    handler = ctx.commands["rec"][0]
    assert "/rec" in handler("help")
    assert "not connected" in handler("")  # capture installed, gateway not connected in unit tests


def test_tool_handlers_return_json(ctx):
    plugin.register(ctx, ROOT)
    out = json.loads(ctx.tools["meeting_search"][2]({"query": "nothing"}))
    assert out == {"results": []}


def test_cli_handler_returns_exit_code(ctx, capsys):
    import argparse
    plugin.register(ctx, ROOT)
    setup_fn, handler_fn = ctx.cli["meeting-scribe"]
    parser = argparse.ArgumentParser()
    setup_fn(parser)
    assert handler_fn(parser.parse_args(["config", "get", "kanban_mode"])) == 0
    assert capsys.readouterr().out.strip() == "approve"


def test_optional_phase_b_install_hook(ctx, monkeypatch):
    calls = []
    fake = SimpleNamespace(install=lambda c, rt: calls.append(rt))
    monkeypatch.setitem(sys.modules, "meeting_scribe.discord_ui", fake)
    rt = plugin.register(ctx, ROOT)
    assert calls == [rt]


def test_broken_phase_b_install_does_not_break_core(ctx, monkeypatch):
    def boom(c, rt):
        raise RuntimeError("discord.py missing")
    monkeypatch.setitem(sys.modules, "meeting_scribe.capture", SimpleNamespace(install=boom))
    plugin.register(ctx, ROOT)
    assert set(ctx.tools) == {"meeting_search", "meeting_get"}


def test_unload_closes_runtime_and_drops_registry(tmp_path, monkeypatch):
    """Review W3: a reload must not leave the old pipeline thread / SQLite handle running."""
    c = FakeCtx(tmp_path, {})
    monkeypatch.setattr(plugin, "_host_overrides", lambda: {"data_dir": lambda: tmp_path / "d",
                                                           "secret": lambda name: None,
                                                           "mcp_allowed": lambda: False})
    rt = plugin.register(c, ROOT)
    closed: list = []
    real_close = rt.close
    monkeypatch.setattr(rt, "close", lambda: (closed.append(1), real_close()))
    for cb in reversed(c.unload):
        cb()
    assert closed == [1] and id(c) not in plugin.RUNTIMES


def test_meeting_command_follows_profile_switch(tmp_path, monkeypatch):
    """Review finding 7: /meeting used to capture runtime.service() and hit a closed database."""
    root = {"p": tmp_path / "a"}
    c = FakeCtx(tmp_path, {})
    monkeypatch.setattr(plugin, "_host_overrides", lambda: {"data_dir": lambda: root["p"],
                                                           "secret": lambda name: None,
                                                           "mcp_allowed": lambda: False})
    monkeypatch.setattr(plugin, "caller_from_session", lambda: plugin.Caller("discord", "1", "2"))
    rt = plugin.register(c, ROOT)
    handler = c.commands["meeting"][0]
    handler("list")
    root["p"] = tmp_path / "b"
    reply = handler("list")
    assert "closed database" not in reply and "Error" not in reply
    plugin.RUNTIMES.pop(id(c))
    rt.close()
