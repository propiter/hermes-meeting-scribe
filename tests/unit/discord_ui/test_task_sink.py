"""Task delivery through the Discord sink (DESIGN §16). Invented channel names only."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.discord_ui.render import RenderOptions
from meeting_scribe.discord_ui.sink import DiscordNotesSink
from meeting_scribe.domain.models import ActionItem, ActionStatus
from meeting_scribe.pipeline.task_moves import apply_move
from meeting_scribe.storage.artifacts import write_notes
from meeting_scribe.storage.layout import Layout
from meeting_scribe.storage.repo import Repository

from .fakes import FakeAdapter, FakeBot

ITEMS = (
    ActionItem(id="a1", title="Landing page", owner_speaker_id="11", owner_name="Luis", project="orion",
               project_key="discord:501", project_confidence=0.9, quote="yo hago la landing"),
    ActionItem(id="a2", title="Contract review", owner_speaker_id="10", owner_name="Ana", project_hint="Nebulla"),
    ActionItem(id="a3", title="Budget", owner_speaker_id=None),
)


class Svc:
    def __init__(self, tmp_path):
        self.layout = Layout(lambda: tmp_path / "data")
        self.repo = Repository(self.layout.db_path())

    def folder(self, meeting):
        return self.layout.meeting_folder(meeting)

    def move_item(self, meeting_id, item_id, channel_id, name):
        m = self.repo.get_meeting(meeting_id)
        return apply_move(self.repo, self.folder(m), m, item_id, channel_id, name)


class Views:
    """The 'view' of a message is the tuple of its custom ids; a panel is (header, blocks, nav)."""

    def view(self, buttons):
        return tuple(b.custom_id for b in buttons) or None

    def panel_view(self, panel):
        return ("panel", panel.header, tuple((b.item_id, b.content, tuple(x.custom_id for x in b.buttons))
                                            for b in panel.blocks), tuple(x.custom_id for x in panel.nav))

    def send_kwargs(self):
        return {}


@pytest.fixture
def env(tmp_path, meeting, notes):
    svc = Svc(tmp_path)
    m = replace(meeting, text_channel_id=None, channel_id="200")
    n = replace(notes, action_items=ITEMS)
    svc.repo.save_meeting(m)
    write_notes(svc.folder(m), m, n, "es")
    svc.repo.sync_action_items(m.id, n.action_items)
    bot = FakeBot()
    chat = bot.add(200, "Daily Sync", threads_ok=False)
    bot.add(900, "Product", kind="category")
    orion = bot.add(501, "『🚀』orion", category_id=900)
    nebula = bot.add(502, "🟢┃nebula", category_id=900)
    bot.user(11)
    bot.user(10, dms_open=False)
    adapter = FakeAdapter(bot)
    cfg: dict = {}
    state = {"loop": None}

    def make():
        s = settings_from_mapping(cfg)
        return DiscordNotesSink(
            settings=lambda: s, service=lambda: svc, adapter=lambda: adapter, loop=lambda: state["loop"],
            options=lambda mm: RenderOptions(lang="en", kanban_on=True, linear_on=False,
                                             is_owner_item=lambda i: i.owner_speaker_id == "11"),
            views=Views(), timeout=5)
    yield SimpleNamespace(svc=svc, meeting=m, notes=n, bot=bot, chat=chat, orion=orion, nebula=nebula, cfg=cfg,
                          state=state, make=make)
    svc.repo.close()


async def deliver(env, sink=None):
    env.state["loop"] = asyncio.get_running_loop()
    sink = sink or env.make()
    res = await asyncio.to_thread(sink.deliver, env.meeting, env.notes, env.svc.folder(env.meeting))
    assert res.ok, res.errors
    return sink


def thread_of(env, channel):
    """The thread started in a project channel, and its messages."""
    starter = channel.ordered()[0]
    thread = next(c for c in env.bot.channels.values() if c.parent is channel)
    return starter, thread


def ptr(env, suffix):
    row = env.svc.repo.get_delivery("discord", f"mtg:{env.meeting.id}:{suffix}")
    return json.loads(row["external_id"]) if row else None


async def test_each_task_is_one_message_with_its_own_buttons_in_its_project_thread(env):
    await deliver(env)
    starter, thread = thread_of(env, env.orion)
    assert "Migración SMTP" in starter.content
    [msg] = thread.ordered()
    assert "Landing page" in msg.content and "<@11>" in msg.content
    assert msg.view and all(cid.endswith(":a1") for cid in msg.view) and "mscribe:ok:k3v7q2ab:a1" in msg.view
    _, neb = thread_of(env, env.nebula)
    [msg2] = neb.ordered()
    assert "Contract review" in msg2.content and all(c.endswith(":a2") for c in msg2.view)
    assert not any(":ok:" in c for c in msg2.view)  # not an owner task: no Kanban


async def test_meeting_chat_keeps_summary_and_index_with_one_button(env):
    await deliver(env)
    msgs = env.chat.ordered()
    assert "Usar SES" in msgs[0].content
    index = msgs[-1]
    assert index.view == ("mscribe:mine:k3v7q2ab:all",)
    _, thread = thread_of(env, env.orion)
    assert f"<#{thread.id}>" in index.content and "**orion** — 1" in index.content
    assert "<@11> — 1" in index.content and "Unassigned — 1" in index.content
    assert any("Budget" in m.content for m in msgs[1:-1])  # no project -> meeting chat (voice chat: no thread)


async def test_reprocess_edits_instead_of_reposting(env):
    sink = await deliver(env)
    before = {c.id: len(c.messages) for c in env.bot.channels.values()}
    await deliver(env, sink)
    await deliver(env, env.make())
    assert {c.id: len(c.messages) for c in env.bot.channels.values()} == before
    _, thread = thread_of(env, env.orion)
    assert thread.ordered()[0].edits >= 2


async def test_approved_task_updates_only_its_message_and_keeps_the_others_aligned(env):
    sink = await deliver(env)
    env.svc.repo.upsert_delivery(env.meeting.id, "kanban", "mtg:k3v7q2ab:a1", external_id="t_42", url=None)
    env.svc.repo.set_action_status(env.meeting.id, "a1", ActionStatus.DELIVERED)
    _, neb = thread_of(env, env.nebula)
    other_before = neb.ordered()[0].view
    await sink.refresh_item(env.meeting.id, "a1")
    _, thread = thread_of(env, env.orion)
    [msg] = thread.ordered()
    assert "✅ Kanban `t_42`" in msg.content and msg.view is None
    assert neb.ordered()[0].view == other_before
    dm = env.bot.users[11].dm.ordered()[0]
    assert "t_42" in dm.view[2][0][1] and dm.view[2][0][2] == ()  # the DM copy was refreshed too


async def test_dms_are_on_by_default_and_a_closed_dm_is_soft(env):
    await deliver(env)
    [dm] = env.bot.users[11].dm.ordered()
    assert dm.view[0] == "panel" and [b[0] for b in dm.view[2]] == ["a1"] and dm.view[3] == ()
    assert env.bot.users[10].dm.ordered() == []
    assert "✉️" in env.chat.ordered()[-1].content and "<@10>" in env.chat.ordered()[-1].content.split("✉️")[1]


async def test_dms_can_be_disabled(env):
    env.cfg["delivery_dm_assignees"] = False
    await deliver(env)
    assert env.bot.users[11].dm.ordered() == [] and "✉️" not in env.chat.ordered()[-1].content


async def test_uncertain_project_goes_to_most_probable_channel_with_warning(env):
    env.bot.add(503, "nebula-web")
    env.nebula.name = "nebula-app"
    await deliver(env)
    posted = [m for c in env.bot.channels.values() if c.parent is not None for m in c.ordered() if "Contract" in m.content]
    assert len(posted) == 1 and "⚠️" in posted[0].content and "📁" in posted[0].content


async def test_missing_permission_falls_back_to_meeting_chat_and_says_so(env):
    env.nebula.can_post = False
    await deliver(env)
    assert any("Contract review" in m.content for m in env.chat.ordered())
    assert "<#502>" in env.chat.ordered()[-1].content


async def test_move_reposts_in_the_right_thread_deletes_the_old_message_and_learns(env):
    sink = await deliver(env)
    old = next(m for m in env.chat.ordered() if "Budget" in m.content)
    env.svc.repo.save_meeting(env.meeting)
    mention = await sink.move_item(env.meeting.id, "a3", "502")
    assert mention == "<#502>" and old.id not in env.chat.messages
    _, neb = thread_of(env, env.nebula)
    assert any("Budget" in m.content for m in neb.ordered())
    assert ptr(env, "task:a3")["target"] == "502"
    assert env.svc.repo.project_channel("nebula") == "502"
    await deliver(env, sink)  # a reprocess keeps it there, no duplicate
    public = [c for c in env.bot.channels.values() if not c.name.startswith("dm-")]
    assert sum("Budget" in m.content for c in public for m in c.ordered()) == 1


async def test_move_learns_the_spoken_name(env):
    sink = await deliver(env)
    await sink.move_item(env.meeting.id, "a2", "501")
    assert env.svc.repo.project_channel("Nebulla") == "501"


async def test_move_options_rank_the_likely_channels_first(env):
    sink = await deliver(env)
    opts = await sink.move_options(env.meeting.id, "a2")
    assert opts[0] == ("502", "#nebula") and ("501", "#orion") in opts and all(o[0] != "900" for o in opts)


async def test_task_panel_for_the_clicker(env):
    sink = await deliver(env)
    view = await sink.task_panel(env.meeting.id, "10", "m", 0, is_owner=False)
    assert [b[0] for b in view[2]] == ["a2"]
    everything = await sink.task_panel(env.meeting.id, "11", "a", 0, is_owner=True)
    assert [b[0] for b in everything[2]] == ["a1", "a2", "a3"]


async def test_not_connected_is_a_soft_failure(env):
    sink = DiscordNotesSink(settings=lambda: settings_from_mapping({}), service=lambda: env.svc,
                            adapter=lambda: None, loop=lambda: None, options=lambda m: None, views=Views())
    res = sink.deliver(env.meeting, env.notes, env.svc.folder(env.meeting))
    assert not res.ok and "not connected" in res.errors[0]
