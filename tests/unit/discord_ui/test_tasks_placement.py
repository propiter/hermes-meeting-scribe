"""``delivery_tasks_placement`` (DESIGN §16.1): every task with the meeting notes, or per project.

Invented channel names only. ``env`` comes from ``test_task_sink`` (voice chat 200 without threads,
``#orion`` 501, ``#nebula`` 502; Luis 11 with open DMs, Ana 10 with closed DMs).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.domain.models import ActionStatus

from .test_task_sink import deliver, env, ptr, thread_of  # noqa: F401 - fixture reuse

TITLES = ("Landing page", "Contract review", "Budget")


def threads(env):
    return [c for c in env.bot.channels.values() if c.parent is not None and not getattr(c, "is_dm", False)]


def everywhere(env):
    """Every message in a server channel or thread (DMs excluded), as ``(channel id, content)``."""
    return [(c.id, m.content) for c in env.bot.channels.values() if not getattr(c, "is_dm", False)
            for m in c.ordered()]


def with_title(env, title):
    return [cid for cid, text in everywhere(env) if title in text]


@pytest.fixture
def together(env):  # noqa: F811
    env.cfg["delivery_tasks_placement"] = "meeting"
    return env


# -- meeting (the default) ------------------------------------------------------------------------------
def test_meeting_is_the_default():
    assert settings_from_mapping({}).delivery_tasks_placement == "meeting"


async def test_every_task_goes_with_the_notes_and_nothing_to_project_channels(together):
    await deliver(together)
    assert together.orion.ordered() == [] and together.nebula.ordered() == [] and threads(together) == []
    for title in TITLES:
        assert with_title(together, title) == [together.chat.id]  # exactly once, in the meeting chat
    msgs = together.chat.ordered()
    assert "Usar SES" in msgs[0].content and msgs[-1].view == ("mscribe:mine:k3v7q2ab:all",)
    task = next(m for m in msgs if "Landing page" in m.content)
    assert "📁 orion" in task.content and "mscribe:ok:k3v7q2ab:a1" in task.view  # labelled, with its buttons
    assert any(cid.startswith("mscribe:prj:") for cid in task.view)  # 📁 Move stays available
    index = msgs[-1].content
    assert "**orion** — 1" in index and f"<#{together.chat.id}>" in index


async def test_assignees_still_get_their_dm(together):
    await deliver(together)
    [dm] = together.bot.users[11].dm.ordered()
    assert [b[0] for b in dm.view[2]] == ["a1"]
    assert "✉️" in together.chat.ordered()[-1].content  # Ana's DMs are closed: said in the index


async def test_tasks_of_one_project_are_posted_together(together):
    await deliver(together)
    order = [t for m in together.chat.ordered() for t in TITLES if t in m.content]
    assert order == ["Landing page", "Contract review", "Budget"]  # projects first, no project last


async def test_a_notes_thread_holds_every_task(together):
    together.chat.threads_ok = True
    await deliver(together)
    [thread] = threads(together)
    assert thread.parent is together.chat
    assert all(with_title(together, t) == [thread.id] for t in TITLES)


async def test_the_fallback_channel_is_not_used(together):
    backlog = together.bot.add(620, "backlog")
    together.cfg["delivery_fallback_channel"] = "620"
    await deliver(together)
    assert backlog.ordered() == [] and with_title(together, "Budget") == [together.chat.id]


async def test_republishing_edits_in_place(together):
    sink = await deliver(together)
    before = {c.id: len(c.messages) for c in together.bot.channels.values()}
    await deliver(together, sink)
    await deliver(together, together.make())
    assert {c.id: len(c.messages) for c in together.bot.channels.values()} == before


async def test_a_manual_move_still_publishes_that_task_in_the_chosen_channel(together):
    sink = await deliver(together)
    old = next(m for m in together.chat.ordered() if "Budget" in m.content)
    assert await sink.move_item(together.meeting.id, "a3", "502", viewer="10") == "<#502>"
    assert old.id not in together.chat.messages
    anchor, task = together.nebula.ordered()  # straight in the channel, after the meeting's anchor
    assert "Migración SMTP" in anchor.content and "Budget" in task.content
    assert ptr(together, "task:a3")["target"] == "502"
    assert with_title(together, "Contract review") == [together.chat.id]  # the others stay together
    await deliver(together, sink)  # a reprocess keeps the move, no duplicate
    assert with_title(together, "Budget") == [502]


# -- switching placement ----------------------------------------------------------------------------------
async def test_switching_to_meeting_brings_tasks_back_and_removes_the_project_anchors(env):  # noqa: F811
    await deliver(env)
    _, orion_thread = thread_of(env, env.orion)
    env.cfg["delivery_tasks_placement"] = "meeting"
    await deliver(env, env.make())  # reprocess --from deliver
    assert env.orion.ordered() == [] and env.nebula.ordered() == []  # anchors deleted
    assert orion_thread.id not in env.bot.channels and threads(env) == []  # their threads too
    assert all(with_title(env, t) == [env.chat.id] for t in TITLES)
    assert ptr(env, "thread:501") is None and ptr(env, "thread:502") is None
    before = {c.id: len(c.messages) for c in env.bot.channels.values()}
    await deliver(env, env.make())  # a second reprocess: nothing new
    assert {c.id: len(c.messages) for c in env.bot.channels.values()} == before


async def test_switching_to_projects_moves_tasks_out_of_the_notes(together):
    await deliver(together)
    together.cfg["delivery_tasks_placement"] = "projects"
    await deliver(together, together.make())
    _, orion = thread_of(together, together.orion)
    _, nebula = thread_of(together, together.nebula)
    assert with_title(together, "Landing page") == [orion.id]
    assert with_title(together, "Contract review") == [nebula.id]
    assert with_title(together, "Budget") == [together.chat.id]


async def test_projects_inline_posts_in_the_channel_without_a_thread(env):  # noqa: F811
    env.cfg["delivery_tasks_placement"] = "projects_inline"
    await deliver(env)
    assert threads(env) == []
    anchor, task = env.orion.ordered()
    assert "Migración SMTP" in anchor.content and "Landing page" in task.content


async def test_a_button_refresh_keeps_the_placement_the_meeting_was_delivered_with(env):  # noqa: F811
    await deliver(env)  # projects
    env.cfg["delivery_tasks_placement"] = "meeting"
    fresh = env.make()
    env.svc.repo.set_action_status(env.meeting.id, "a2", ActionStatus.DISMISSED)
    await fresh.refresh_item(env.meeting.id, "a2")
    await fresh.refresh(env.meeting.id)
    _, nebula = thread_of(env, env.nebula)
    assert with_title(env, "Contract review") == [nebula.id]  # not re-laid by a click
    assert ptr(env, "notes")["tasks"] == "projects"


async def test_a_meeting_published_before_the_setting_keeps_its_layout_on_refresh(env):  # noqa: F811
    await deliver(env)  # projects, with threads
    notes = ptr(env, "notes")
    notes.pop("tasks")  # as an older version stored it
    env.svc.repo.upsert_delivery(env.meeting.id, "discord", f"mtg:{env.meeting.id}:notes",
                                 external_id=json.dumps(notes), url=notes.get("url"))
    env.cfg["delivery_tasks_placement"] = "meeting"
    await env.make().refresh(env.meeting.id)
    _, orion = thread_of(env, env.orion)
    assert with_title(env, "Landing page") == [orion.id]


async def test_a_task_left_in_a_refused_forum_is_not_duplicated_after_switching(env):  # noqa: F811
    del env.bot.channels[501]
    env.orion = env.bot.add_forum(501, "orion", tags=("orion",))
    await deliver(env)
    [post] = env.orion.posts
    env.cfg["delivery_tasks_placement"] = "meeting"
    await deliver(env, env.make())
    assert post.id not in env.bot.channels  # the project post named after the meeting is gone
    assert with_title(env, "Landing page") == [env.chat.id]


async def test_concurrent_publishes_do_not_duplicate_in_meeting_mode(together):
    sink = await deliver(together)
    await asyncio.gather(sink.refresh(together.meeting.id), sink.refresh(together.meeting.id))
    assert all(len(with_title(together, t)) == 1 for t in TITLES)


# -- private, direct messages and forum notes keep their own rules --------------------------------------------
async def test_notes_forum_post_holds_every_task(together):
    forum = together.bot.add_forum(640, "meeting-notes")
    together.cfg["delivery_discord_channel"] = "640"
    await deliver(together)
    [post] = forum.posts
    assert all(with_title(together, t) == [post.id] for t in TITLES)
    assert together.orion.ordered() == [] and together.nebula.ordered() == []
    assert "Usar SES" in post.ordered()[0].content and post.ordered()[-1].view == ("mscribe:mine:k3v7q2ab:all",)


async def test_the_index_does_not_report_project_channels_it_does_not_use(together):
    together.nebula.can_post = False
    await deliver(together)
    index = together.chat.ordered()[-1].content
    assert "⛔" not in index and "- **nebula** — 1 · <#200>" in index  # linked to the notes, no warning


async def test_the_index_still_reports_a_channel_without_permission_in_projects_mode(env):  # noqa: F811
    env.nebula.can_post = False
    await deliver(env)
    assert "⛔" in env.chat.ordered()[-1].content
