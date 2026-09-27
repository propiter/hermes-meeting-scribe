"""Regression tests for review findings W3 (reload), W8 (partial notes post), S1, S2."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("discord")

from meeting_scribe.config import settings_from_mapping  # noqa: E402
from meeting_scribe.discord_ui.actions import ButtonActions  # noqa: E402
REPLY_LIMIT = 1900  # Discord hard limit is 2000
from meeting_scribe.discord_ui.render import RenderOptions  # noqa: E402
from meeting_scribe.discord_ui.sink import DiscordNotesSink  # noqa: E402
from meeting_scribe.domain.models import Candidate, SinkResult  # noqa: E402
from meeting_scribe.storage.artifacts import write_notes  # noqa: E402

from .fakes import FakeAdapter, FakeBot, FakeInteraction  # noqa: E402
from .test_install import install_all, make_adapter
from .test_install import rt as rt  # noqa: F401  (fixture)
from .test_sink import Svc as SinkSvc, Views  # noqa: E402


# -- W3 -----------------------------------------------------------------------------------------
def test_each_install_has_a_unique_factory_qualname(rt, monkeypatch):
    a = install_all(rt, monkeypatch).handlers["discord"][0]
    b = install_all(rt, monkeypatch).handlers["discord"][0]
    assert a.__qualname__ != b.__qualname__  # Hermes keys re-wiring by (plugin, qualname)


async def test_unload_detaches_listener_and_dynamic_items(rt, monkeypatch):
    ctx = install_all(rt, monkeypatch)
    adapter = make_adapter()
    bot = adapter._client
    ctx.handlers["discord"][0](bot, adapter)
    assert bot.listeners["on_voice_state_update"] and bot.dynamic_items
    for cb in reversed(ctx.unload):
        cb()
    assert bot.listeners["on_voice_state_update"] == [] and bot.dynamic_items == []
    ctx.handlers["discord"][0](bot, adapter)  # a stale factory call after unload is inert
    assert bot.listeners["on_voice_state_update"] == []


async def test_unload_from_worker_thread_runs_bot_work_on_the_loop(rt, monkeypatch):
    ctx = install_all(rt, monkeypatch)
    adapter = make_adapter()
    bot = adapter._client
    ctx.handlers["discord"][0](bot, adapter)
    loop = asyncio.get_running_loop()
    seen: list = []
    orig = bot.remove_listener

    def remove_listener(fn: Any, name: Any = None) -> None:
        seen.append(asyncio.get_running_loop() is loop)
        orig(fn, name)
    bot.remove_listener = remove_listener
    ui_unload = [cb for cb in ctx.unload if cb.__name__ == "meeting_scribe_discord_unload"][0]
    await asyncio.to_thread(ui_unload)
    assert seen == [True]


# -- W8 -----------------------------------------------------------------------------------------
@pytest.fixture
def sink_env(tmp_path, meeting, notes):
    svc = SinkSvc(tmp_path)
    m = replace(meeting, text_channel_id=None, channel_id="200")
    svc.repo.save_meeting(m)
    write_notes(svc.folder(m), m, notes, "es")
    svc.repo.sync_action_items(m.id, notes.action_items)
    bot = FakeBot()
    ch = bot.add(200, "Daily Sync", threads_ok=False)
    adapter = FakeAdapter(bot)
    yield SimpleNamespace(svc=svc, meeting=m, notes=notes, ch=ch, adapter=adapter)
    svc.repo.close()


async def test_retry_after_partial_post_does_not_duplicate_messages(sink_env, monkeypatch):
    env = sink_env
    loop = asyncio.get_running_loop()
    s = settings_from_mapping({})
    sink = DiscordNotesSink(settings=lambda: s, service=lambda: env.svc, adapter=lambda: env.adapter,
                            loop=lambda: loop, views=Views(), timeout=5,
                            options=lambda mm: RenderOptions("en", True, False, lambda i: True))
    orig = type(env.ch).send
    calls = {"n": 0}

    async def flaky(self: Any, content: str = "", **kw: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("429 on part 2")
        return await orig(self, content, **kw)
    monkeypatch.setattr(type(env.ch), "send", flaky)
    folder = env.svc.folder(env.meeting)
    r1 = await asyncio.to_thread(sink.deliver, env.meeting, env.notes, folder)
    assert not r1.ok
    row = env.svc.repo.get_delivery("discord", "mtg:k3v7q2ab:notes")
    assert len(json.loads(row["external_id"])["messages"]) == 1  # the header is remembered
    r2 = await asyncio.to_thread(sink.deliver, env.meeting, env.notes, folder)
    assert r2.ok, r2.errors
    assert len(env.ch.ordered()) == 1 + len(env.notes.action_items) + 1  # summary, tasks, index: no duplicate
    ptr = json.loads(env.svc.repo.get_delivery("discord", "mtg:k3v7q2ab:notes")["external_id"])
    assert ptr["messages"] == [env.ch.ordered()[0].id]


# -- S1 / S2 ------------------------------------------------------------------------------------
class ActSvc:
    def __init__(self) -> None:
        self.errors: tuple = ()

    def approve_all(self, mid: str, sink: str) -> SinkResult:
        return SinkResult(sink, False, (), errors=self.errors)

    def require(self, mid: str) -> Any:
        return SimpleNamespace(id=mid)

    def candidates(self, meeting: Any) -> list:
        return [Candidate("hermes:p1", "Website", "hermes")]


def actions(svc: ActSvc) -> ButtonActions:
    async def refresh(mid: str) -> None:
        return None
    return ButtonActions(service=lambda: svc, settings=lambda: settings_from_mapping({}), owners=lambda: ("11",),
                         check_auth=lambda i: True, refresh=refresh,
                         project_view=lambda mid, cands: ("select", mid))


async def test_project_picker_defers_before_the_catalog_lookup():
    svc = ActSvc()
    order: list[str] = []
    i = FakeInteraction(11)
    orig_defer = i.response.defer

    async def defer(**kw: Any) -> None:
        order.append("defer")
        await orig_defer(**kw)

    def candidates(meeting: Any) -> list:
        order.append("lookup")
        return [Candidate("hermes:p1", "Website", "hermes")]
    i.response.defer = defer
    svc.candidates = candidates  # type: ignore[method-assign]
    await actions(svc).handle(i, "prj", "k3v7q2ab", "all")
    assert order == ["defer", "lookup"]
    assert i.followup.sent and i.followup.sent[0]["view"] == ("select", "k3v7q2ab")


async def test_replies_are_truncated_below_discord_limit():
    svc = ActSvc()
    svc.errors = tuple(f"item {n}: Linear said something long " * 5 for n in range(50))
    i = FakeInteraction(11)
    await actions(svc).handle(i, "alll", "k3v7q2ab", "all")
    assert len(i.followup.sent[0]["content"]) <= REPLY_LIMIT
