"""Private and normal ``meeting_routes`` rules through the Discord sink (DESIGN §19.2).

Invented names only. The private notes channel is ``#leadership-notes`` (700, not visible to
@everyone); the project channels are ``#orion`` (501) and ``#nebula`` (502).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from meeting_scribe import privacy
from meeting_scribe.config import settings_from_mapping
from meeting_scribe.discord_ui.render import RenderOptions
from meeting_scribe.discord_ui.sink import DiscordNotesSink
from meeting_scribe.domain.errors import DirectMessageUnavailable, ItemDismissed
from meeting_scribe.domain.models import ActionItem, ActionStatus
from meeting_scribe.storage.artifacts import write_notes

from .fakes import FakeAdapter, FakeBot
from .test_task_sink import Svc, Views

ITEMS = (
    ActionItem(id="a1", title="Landing page", description="Draft the new landing page", owner_speaker_id="11",
               owner_name="Luis", project="orion", project_key="discord:501", project_confidence=0.9,
               quote="the secret plan is to sell", due="2026-10-02"),
    ActionItem(id="a2", title="Contract review", owner_speaker_id="10", owner_name="Ana", project="nebula",
               project_key="discord:502", project_confidence=0.9),
    ActionItem(id="a3", title="Budget", owner_speaker_id=None),
)
SUMMARY_WORD = "Usar SES"  # a decision of the ``notes`` fixture: must never leave the private channel
QUOTE = "the secret plan"


@pytest.fixture
def env(tmp_path, meeting, notes):
    svc = Svc(tmp_path)
    m = replace(meeting, text_channel_id=None, channel_id="200", channel_name="Leadership", category_id="900",
                category_name="Board")
    n = replace(notes, action_items=ITEMS)
    svc.repo.save_meeting(m)
    write_notes(svc.folder(m), m, n, "es")
    svc.repo.sync_action_items(m.id, n.action_items)
    bot = FakeBot()
    voice_chat = bot.add(200, "Leadership", threads_ok=False)
    private = bot.add(700, "leadership-notes", public=False)
    design = bot.add(710, "design-meetings")
    orion = bot.add(501, "orion")
    nebula = bot.add(502, "nebula")
    fallback = bot.add(600, "general-tasks")
    bot.user(11)
    bot.user(10, dms_open=False)
    adapter = FakeAdapter(bot)
    cfg: dict = {"meeting_routes": ["Leadership = 700:private"], "delivery_fallback_channel": "600"}
    state = {"loop": None}

    def make():
        s = settings_from_mapping(cfg)
        return DiscordNotesSink(
            settings=lambda space=None: s, service=lambda: svc, adapter=lambda: adapter, loop=lambda: state["loop"],
            options=lambda mm: RenderOptions(lang="en", kanban_on=True, linear_on=False,
                                             is_owner_item=lambda i: i.owner_speaker_id == "11"),
            views=Views(), timeout=5)
    yield SimpleNamespace(svc=svc, meeting=m, notes=n, bot=bot, chat=voice_chat, private=private, design=design,
                          orion=orion, nebula=nebula, fallback=fallback, cfg=cfg, state=state, make=make)
    svc.repo.close()


async def deliver(env, sink=None, *, ok=True):
    env.state["loop"] = asyncio.get_running_loop()
    sink = sink or env.make()
    res = await asyncio.to_thread(sink.deliver, env.meeting, env.notes, env.svc.folder(env.meeting))
    assert res.ok is ok, res.errors
    return sink, res


def outside(env):
    """Every message posted anywhere but the private channel (threads included) or a DM."""
    return [m for c in env.bot.channels.values() if c is not env.private and c.parent is not env.private
            for m in c.ordered()]


def inside(env, channel=None):
    """Every message of the private channel and of its threads, oldest first."""
    home = channel or env.private
    return sorted((m for c in env.bot.channels.values() if c is home or c.parent is home for m in c.ordered()),
                  key=lambda m: m.id)


def task_msg(env, title):
    """The task's own message (its ``task:<item>`` pointer), wherever it is."""
    item = next(i.id for i in ITEMS if i.title == title)
    row = env.svc.repo.get_delivery("discord", f"mtg:{env.meeting.id}:task:{item}")
    ptr = json.loads(row["external_id"])
    return env.bot.channels[int(ptr["channel"])].messages[int(ptr["message"])]


def texts(msgs):
    return "\n".join(m.content for m in msgs)


# -- private: nothing leaves by itself ---------------------------------------------------------------
async def test_private_meeting_publishes_everything_only_in_its_channel(env):
    await deliver(env)
    msgs = inside(env)
    body = texts(msgs)
    assert SUMMARY_WORD in body and all(t in body for t in ("Landing page", "Contract review", "Budget"))
    assert outside(env) == []  # no project channel, no fallback, no voice chat, no thread, no DM
    assert privacy.record(env.svc.repo, env.meeting.id) == {"rule": "Leadership", "channel": "700"}
    index = next(m for m in env.private.ordered() if "Private meeting" in m.content)
    assert "Private meeting" in index.content and "Shared: 0 of 3" in index.content
    assert "mscribe:sha:k3v7q2ab:all" in index.view


async def test_each_private_task_has_share_buttons_where_they_apply(env):
    await deliver(env)
    by_title = {t: task_msg(env, t) for t in ("Landing page", "Contract review", "Budget")}
    a1 = by_title["Landing page"].view
    assert "mscribe:shd:k3v7q2ab:a1" in a1 and "mscribe:shp:k3v7q2ab:a1" in a1 and "mscribe:ok:k3v7q2ab:a1" in a1
    assert "mscribe:shp:k3v7q2ab:a2" in by_title["Contract review"].view
    assert not any(c.startswith(("mscribe:shd", "mscribe:shp")) for c in by_title["Budget"].view or ())
    assert "Nothing has been shared yet" in by_title["Landing page"].content


async def test_share_with_assignee_sends_only_the_task_and_is_idempotent(env):
    sink, _ = await deliver(env)
    assert await sink.share(env.meeting.id, "a1", "dm") == "dm"
    [dm] = env.bot.users[11].dm.ordered()
    assert "Landing page" in dm.content and "Draft the new landing page" in dm.content and "2026-10-02" in dm.content
    assert QUOTE not in dm.content and SUMMARY_WORD not in dm.content and "700" not in dm.content
    assert dm.view is None
    assert await sink.share(env.meeting.id, "a1", "dm") == ""  # already done: nothing new
    assert len(env.bot.users[11].dm.ordered()) == 1
    task = task_msg(env, "Landing page")
    assert "Sent to <@11>" in task.content and "mscribe:shd:k3v7q2ab:a1" not in task.view
    assert [m for m in outside(env) if m.channel is not env.bot.users[11].dm] == []


async def test_share_with_closed_dms_says_so_and_records_nothing(env):
    sink, _ = await deliver(env)
    with pytest.raises(DirectMessageUnavailable):
        await sink.share(env.meeting.id, "a2", "dm")
    task = task_msg(env, "Contract review")
    assert "mscribe:shd:k3v7q2ab:a2" in task.view


async def test_publish_in_project_channel_posts_the_task_only(env):
    sink, _ = await deliver(env)
    assert await sink.share(env.meeting.id, "a1", "project") == "501"
    [copy] = env.orion.ordered()
    assert "Landing page" in copy.content and copy.view is None
    assert QUOTE not in copy.content and SUMMARY_WORD not in copy.content and "700" not in copy.content
    assert await sink.share(env.meeting.id, "a1", "project") == ""
    assert len(env.orion.ordered()) == 1 and env.bot.users[11].dm.ordered() == []
    task = task_msg(env, "Landing page")
    assert "Published in <#501>" in task.content and "mscribe:shp:k3v7q2ab:a1" not in task.view


async def test_share_all_does_the_normal_distribution_and_reports_failures(env):
    sink, _ = await deliver(env)
    report = await sink.share_all(env.meeting.id)
    assert (report.dms, report.channels) == (1, 2) and report.failed == ["Contract review"]  # Ana: DMs closed
    assert "Landing page" in texts(env.orion.ordered()) and "Contract review" in texts(env.nebula.ordered())
    assert env.fallback.ordered() == [] and env.chat.ordered() == []  # Budget has no project: stays private
    assert SUMMARY_WORD not in texts(outside(env))
    again = await sink.share_all(env.meeting.id)
    assert (again.dms, again.channels) == (0, 0)
    assert len(env.orion.ordered()) == 1 and len(env.bot.users[11].dm.ordered()) == 1
    assert "Shared: 2 of 3" in next(m for m in env.private.ordered() if "Private meeting" in m.content).content


async def test_reprocess_keeps_everything_private_and_edits_shared_copies(env):
    sink, _ = await deliver(env)
    await sink.share(env.meeting.id, "a1", "project")
    before = {c.id: len(c.messages) for c in env.bot.channels.values()}
    renamed = tuple(replace(i, title="Landing page v2") if i.id == "a1" else i for i in ITEMS)
    env.notes = replace(env.notes, action_items=renamed)
    env.svc.repo.sync_action_items(env.meeting.id, renamed)
    write_notes(env.svc.folder(env.meeting), env.meeting, env.notes, "es")
    await deliver(env, env.make())
    assert {c.id: len(c.messages) for c in env.bot.channels.values()} == before
    assert "Landing page v2" in env.orion.ordered()[0].content
    # a task that disappears on reprocess takes its shared copy with it
    env.notes = replace(env.notes, action_items=ITEMS[1:])
    env.svc.repo.sync_action_items(env.meeting.id, ITEMS[1:])
    write_notes(env.svc.folder(env.meeting), env.meeting, env.notes, "es")
    await deliver(env, env.make())
    assert env.orion.ordered() == []


async def test_dismissed_task_offers_no_share_and_cannot_be_shared(env):
    sink, _ = await deliver(env)
    env.svc.repo.set_action_status(env.meeting.id, "a1", ActionStatus.DISMISSED)
    await sink.refresh_item(env.meeting.id, "a1")
    task = task_msg(env, "Landing page")
    assert not any(c.startswith(("mscribe:shd", "mscribe:shp")) for c in task.view or ())
    with pytest.raises(ItemDismissed):
        await sink.share(env.meeting.id, "a1", "project")
    assert env.orion.ordered() == []


async def test_rule_removed_later_keeps_the_meeting_private(env):
    sink, _ = await deliver(env)
    env.cfg["meeting_routes"] = []
    await deliver(env, env.make())
    assert outside(env) == [] and await env.make().private_place(env.meeting.id) >= {"700"}


async def test_rule_made_private_after_publishing_withdraws_public_copies(env):
    env.cfg["meeting_routes"] = []
    env.chat.threads_ok = True  # the voice chat holds the notes AND a thread with the unrouted tasks
    await deliver(env)
    assert env.orion.ordered() and env.chat.ordered()  # normal: voice chat + project threads
    before = {c.id for c in env.bot.channels.values() if c.parent is not None}
    assert before  # project threads (and the notes thread) exist
    env.cfg["meeting_routes"] = ["Leadership = 700:private"]
    await deliver(env, env.make())
    public = [m for m in outside(env) if not m.channel.is_dm]
    assert SUMMARY_WORD not in texts(public) and QUOTE not in texts(public)
    assert all(t in texts(inside(env)) for t in ("Landing page", "Budget"))
    left = [c for c in env.bot.channels.values() if c.parent is not None and c.parent is not env.private]
    assert left == []  # no thread named after the meeting is left in a public channel
    assert [m.content for m in public] == [] or all("no longer shown here" in m.content for m in public)


# -- the rule's channel cannot be used: waits, never a more public channel -----------------------------
@pytest.mark.parametrize("route", ["Leadership = 799:private", "Leadership = #missing:private",
                                   "Leadership = 700:hidden"])
async def test_unusable_private_rule_waits_and_posts_nothing(env, route):
    env.cfg["meeting_routes"] = [route]
    _, res = await deliver(env, ok=False)
    assert res.waiting and "meeting_routes" in res.errors[0]
    assert [m for c in env.bot.channels.values() for m in c.ordered()] == []


async def test_unusable_normal_rule_also_waits(env):
    env.cfg["meeting_routes"] = ["Leadership = 799"]
    _, res = await deliver(env, ok=False)
    assert res.waiting and [m for c in env.bot.channels.values() for m in c.ordered()] == []


# -- normal rules --------------------------------------------------------------------------------------
async def test_normal_rule_only_moves_the_notes(env):
    env.cfg["meeting_routes"] = ["category:Board = design-meetings"]
    await deliver(env)
    assert SUMMARY_WORD in texts(env.design.ordered()) and inside(env) == []
    assert env.chat.ordered() == []  # not the voice chat
    assert env.orion.ordered() and env.nebula.ordered()  # project tasks as usual
    assert env.bot.users[11].dm.ordered()  # assignee panel as usual
    assert privacy.record(env.svc.repo, env.meeting.id) is None
    assert not any("mscribe:sha" in v for m in inside(env, env.design) for v in (m.view or ()))


async def test_private_rule_on_a_forum(env):
    forum = env.bot.add_forum(720, "leadership-forum", public=False)
    env.cfg["meeting_routes"] = ["Leadership = 720:private"]
    await deliver(env)
    [post] = forum.posts
    assert SUMMARY_WORD in texts(post.ordered()) and "Landing page" in texts(post.ordered())
    assert [m for m in outside(env) if m.channel is not post] == []
    assert await env.make().private_place(env.meeting.id) >= {"720", str(post.id)}


async def test_private_rule_has_no_fallback_and_reports_the_rule(env):
    env.state["loop"] = asyncio.get_running_loop()
    dest = env.make().destination(env.meeting)
    assert dest.private and dest.rule == "Leadership" and dest.fallback_channel is None
    assert dest.targets == ["700"]
    report = json.loads(env.svc.repo.kv_get("discord.routes_report.main") or "null")
    assert report is None  # written on a delivery, not on a plain resolution
    await deliver(env)
    [row] = json.loads(env.svc.repo.kv_get("discord.routes_report.main"))
    assert row["origin"] == "Leadership" and row["status"] == "ok" and row["private"] and row["public"] is False
