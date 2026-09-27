"""install(ctx, runtime) for capture + discord_ui: platform handler, listeners, sink, pipeline."""
from __future__ import annotations

import asyncio
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("discord")

from meeting_scribe import capture, discord_ui, doctor  # noqa: E402
from meeting_scribe.capture.compat import CompatResult  # noqa: E402
from meeting_scribe.capture.controller import CaptureManager  # noqa: E402
from meeting_scribe.runtime import Host, Runtime  # noqa: E402

import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("_capture_fakes", Path(__file__).parents[1] / "capture" / "fakes.py")
assert _spec is not None and _spec.loader is not None
_fakes = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)
FakeAdapter, FakeBot, FakeGuild, FakeVoiceReceiver = (_fakes.FakeAdapter, _fakes.FakeBot, _fakes.FakeGuild,
                                                      _fakes.FakeVoiceReceiver)


class Kanban:
    def list_boards(self):
        return []


class Ctx:
    def __init__(self):
        self.handlers: dict = {}
        self.unload: list = []

    def register_platform_handler(self, platform, factory):
        self.handlers.setdefault(platform, []).append(factory)

    def on_unload(self, cb):
        self.unload.append(cb)


@pytest.fixture
def rt(tmp_path, monkeypatch):
    cfg: dict = {}
    host = Host(get_config=lambda k, d=None: cfg.get(k, d), set_config=cfg.__setitem__,
                data_dir=lambda: tmp_path / "data", llm=lambda: SimpleNamespace(), secret=lambda n: None,
                spawner=lambda target, *, name, daemon=True: threading.Thread(target=target, name=name, daemon=daemon),
                call_mcp=lambda: None, kanban=Kanban(), project_sources=lambda: [], llm_ready=lambda: (True, "ok"))
    runtime = Runtime(host)
    checks_before = list(doctor.registry.names())
    mod = types.ModuleType("fake_hermes_adapter2")
    setattr(mod, "VoiceReceiver", FakeVoiceReceiver)
    setattr(mod, "_component_check_auth", lambda i, users, roles: str(i.user.id) in {str(u) for u in users})
    sys.modules["fake_hermes_adapter2"] = mod
    yield runtime
    runtime.close()
    sys.modules.pop("fake_hermes_adapter2", None)
    for name in set(doctor.registry.names()) - set(checks_before):
        doctor.registry._checks.pop(name, None)


def make_adapter():
    cls = type("DiscordAdapter", (FakeAdapter,), {"__module__": "fake_hermes_adapter2"})
    a = cls(FakeBot([FakeGuild()]))
    a.config = SimpleNamespace(home_channel=None)
    return a


def install_all(rt, monkeypatch):
    monkeypatch.setattr("meeting_scribe.capture.controller.compat_for_adapter",
                        lambda adapter: CompatResult(True, (), ("x",)))
    ctx = Ctx()
    capture.install(ctx, rt)
    discord_ui.install(ctx, rt)
    return ctx


def test_capture_install_sets_controller_checks_and_unload(rt, monkeypatch):
    ctx = install_all(rt, monkeypatch)
    assert isinstance(rt.capture, CaptureManager)
    assert {"discord_compat", "discord_voice_deps"} <= set(doctor.registry.names())
    assert ctx.unload and len(ctx.handlers["discord"]) == 1
    assert any(getattr(s, "name", "") == "discord" for s in rt.sinks())
    ok, detail = rt.capture_status()
    assert ok and "waiting" in detail


async def test_factory_attaches_listeners_items_and_starts_pipeline(rt, monkeypatch):
    ctx = install_all(rt, monkeypatch)
    adapter = make_adapter()
    bot = adapter._client
    ctx.handlers["discord"][0](bot, adapter)
    assert rt.capture.adapter is adapter and rt.capture.loop is asyncio.get_running_loop()
    assert len(bot.listeners["on_voice_state_update"]) == 1
    assert len(bot.dynamic_items) == 2
    assert rt.pipeline_running()
    ok, detail = rt.capture_status()
    assert ok and "ready" in detail


async def test_reconnect_with_new_bot_rewires_without_duplicates(rt, monkeypatch):
    ctx = install_all(rt, monkeypatch)
    factory = ctx.handlers["discord"][0]
    a1 = make_adapter()
    factory(a1._client, a1)
    factory(a1._client, a1)
    assert len(a1._client.listeners["on_voice_state_update"]) == 1
    a2 = make_adapter()
    factory(a2._client, a2)
    assert rt.capture.adapter is a2 and len(a2._client.listeners["on_voice_state_update"]) == 1
    assert a1._client.listeners["on_voice_state_update"] == []


async def test_button_auth_uses_owners_and_hermes_helper(rt, monkeypatch):
    ctx = install_all(rt, monkeypatch)
    adapter = make_adapter()
    adapter._allowed_user_ids = {"77"}
    ctx.handlers["discord"][0](adapter._client, adapter)
    actions = discord_ui.state_for(rt).actions
    user = lambda uid: SimpleNamespace(user=SimpleNamespace(id=uid))  # noqa: E731
    # Hermes' helper still gates the 0.1 meeting-wide buttons; task buttons are per assignee (§16).
    assert actions._hermes_allows(user(77)) and not actions._hermes_allows(user(78))
    assert not actions.is_owner(user(77))
    rt.set_config("owners", ["77"])
    assert actions.is_owner(user(77))


def test_install_adds_the_discord_channels_catalog(rt, monkeypatch):
    install_all(rt, monkeypatch)
    assert any(getattr(c, "name", "") == "discord" for c in rt.catalogs())


def test_unload_shuts_capture_down(rt, monkeypatch):
    ctx = install_all(rt, monkeypatch)
    calls = []
    monkeypatch.setattr(rt.capture, "shutdown", lambda *a, **k: calls.append(1))
    for cb in ctx.unload:
        cb()
    assert calls


def test_discord_ui_without_capture_still_registers_sink(rt):
    ctx = Ctx()
    discord_ui.install(ctx, rt)
    assert rt.capture is None and ctx.handlers["discord"]
    assert any(getattr(s, "name", "") == "discord" for s in rt.sinks())


def test_capture_status_delegates_to_controller(rt):
    rt.capture = SimpleNamespace(status=lambda: (False, "incompatible: boom"), live_meeting_ids=set)
    assert rt.capture_status() == (False, "incompatible: boom")


def test_packages_expose_install():
    assert callable(capture.install) and callable(discord_ui.install)
    assert Path(capture.__file__).name == "__init__.py"
