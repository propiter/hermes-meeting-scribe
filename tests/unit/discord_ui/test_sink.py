"""DiscordNotesSink: loop bridging, target fallback, threads, idempotent edit (DESIGN §8)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.discord_ui.render import RenderOptions
from meeting_scribe.discord_ui.sink import DiscordNotesSink
from meeting_scribe.storage.artifacts import write_notes
from meeting_scribe.storage.layout import Layout
from meeting_scribe.storage.repo import Repository

from .fakes import FakeAdapter, FakeBot


class Svc:
    def __init__(self, tmp_path):
        self.layout = Layout(lambda: tmp_path / "data")
        self.repo = Repository(self.layout.db_path())

    def folder(self, meeting):
        return self.layout.meeting_folder(meeting)


class Views:
    """Stands in for discord.ui: the 'view' is just the tuple of custom ids."""

    def view(self, buttons):
        return tuple(b.custom_id for b in buttons) or None

    def send_kwargs(self):
        return {}


@pytest.fixture
def env(tmp_path, meeting, notes):
    svc = Svc(tmp_path)
    m = replace(meeting, text_channel_id=None, channel_id="200")
    svc.repo.save_meeting(m)
    write_notes(svc.folder(m), m, notes, "es")
    svc.repo.sync_action_items(m.id, notes.action_items)
    bot = FakeBot()
    voice = bot.add(200, "Daily Sync", threads_ok=False)
    notes_ch = bot.add(300, "meeting-notes")
    home = bot.add(400, "home")
    adapter = FakeAdapter(bot, home="400")
    cfg: dict = {}
    state: dict = {"adapter": adapter, "loop": None}

    def make():
        s = settings_from_mapping(cfg)
        return DiscordNotesSink(
            settings=lambda: s, service=lambda: svc, adapter=lambda: state["adapter"], loop=lambda: state["loop"],
            options=lambda meeting: RenderOptions(lang="en", kanban_on=True, linear_on=False,
                                                  is_owner_item=lambda i: i.owner_speaker_id == "11"),
            views=Views(), timeout=5)
    yield dict(svc=svc, meeting=m, notes=notes, voice=voice, notes_ch=notes_ch, home=home, cfg=cfg, state=state,
               make=make, adapter=adapter)
    svc.repo.close()


async def deliver(env, sink=None):
    env["state"]["loop"] = asyncio.get_running_loop()
    sink = sink or env["make"]()
    return await asyncio.to_thread(sink.deliver, env["meeting"], env["notes"], env["svc"].folder(env["meeting"]))


async def test_posts_in_voice_text_chat_without_thread_and_records_pointer(env):
    res = await deliver(env)
    assert res.ok, res.errors
    msgs = env["voice"].ordered()
    assert "Migración SMTP" in msgs[0].content and any("<@11>" in m.content for m in msgs)
    task = next(m for m in msgs if "Enviar credenciales" in m.content)
    assert "mscribe:ok:k3v7q2ab:a0000000001" in task.view and all(c.endswith("a0000000001") for c in task.view)
    assert msgs[-1].view == ("mscribe:mine:k3v7q2ab:all",)  # the index closes the meeting chat
    row = env["svc"].repo.get_delivery("discord", "mtg:k3v7q2ab:notes")
    ptr = json.loads(row["external_id"])
    assert ptr["channel"] == 200 and ptr["thread"] is None and ptr["messages"] == [msgs[0].id]
    assert row["url"] == msgs[0].jump_url


async def test_configured_channel_gets_header_plus_thread(env):
    env["cfg"]["delivery_discord_channel"] = "300"
    res = await deliver(env)
    assert res.ok
    assert len(env["notes_ch"].ordered()) == 2  # summary + index in the channel, tasks in its thread
    threads = [c for c in env["adapter"]._client.channels.values() if c.parent is env["notes_ch"]]
    assert len(threads) == 1 and len(threads[0].ordered()) == 2
    assert json.loads(env["svc"].repo.get_delivery("discord", "mtg:k3v7q2ab:notes")["external_id"])["thread"] == threads[0].id


async def test_thread_disabled_posts_everything_in_channel(env):
    env["cfg"].update({"delivery_discord_channel": "300", "delivery_discord_thread": False})
    await deliver(env)
    assert len(env["notes_ch"].ordered()) > 1


async def test_falls_back_to_home_channel(env):
    env["meeting"] = replace(env["meeting"], channel_id="999")
    res = await deliver(env)
    assert res.ok and env["home"].ordered()


async def test_reprocess_edits_instead_of_reposting(env):
    await deliver(env)
    first = env["voice"].ordered()
    env["notes"] = replace(env["notes"], tldr="Nuevo resumen.")
    res = await deliver(env)
    assert res.ok
    again = env["voice"].ordered()
    assert [m.id for m in again] == [m.id for m in first]
    assert "Nuevo resumen." in again[0].content and again[0].edits == 1


async def test_reprocess_with_fewer_messages_deletes_surplus(env):
    await deliver(env)
    before = len(env["voice"].ordered())
    env["notes"] = replace(env["notes"], action_items=())
    for item in env["svc"].repo.list_action_items(env["meeting"].id):
        env["svc"].repo.set_action_status(env["meeting"].id, item.id, item.status)
    env["svc"].repo.sync_action_items(env["meeting"].id, ())
    await deliver(env)
    after = env["voice"].ordered()
    assert len(after) == before - 2 and not any("Enviar credenciales" in m.content for m in after)
    assert env["svc"].repo.list_deliveries(env["meeting"].id, sink="discord", prefix="mtg:k3v7q2ab:task:") == []


async def test_deleted_messages_are_reposted(env):
    await deliver(env)
    for m in env["voice"].ordered():
        await m.delete()
    res = await deliver(env)
    assert res.ok and env["voice"].ordered()


async def test_not_connected_fails_softly_for_retry(env):
    env["state"]["adapter"] = None
    res = await deliver(env)
    assert not res.ok and "not connected" in res.errors[0]


async def test_no_reachable_channel_is_an_error(env):
    env["adapter"].config.home_channel = None
    env["meeting"] = replace(env["meeting"], channel_id="999")
    res = await deliver(env)
    assert not res.ok and "channel" in res.errors[0]


async def test_refresh_rerenders_statuses_on_loop(env):
    from meeting_scribe.domain.models import ActionStatus
    await deliver(env)
    env["svc"].repo.set_action_status(env["meeting"].id, "a0000000001", ActionStatus.DELIVERED)
    sink = env["make"]()
    await sink.refresh(env["meeting"].id)
    body = [m for m in env["voice"].ordered() if "Enviar credenciales" in m.content][0]
    assert "✅" in body.content and body.view is None


def test_enabled_follows_setting(env):
    assert env["make"]().enabled()
    env["cfg"]["delivery_discord_enabled"] = False
    assert not env["make"]().enabled()
