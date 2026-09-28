"""The participants line of the notes (DESIGN §19.4): mentions once, exactly the right users, never
@everyone/roles/the bot; Meet attendees through person links; private channels only ping who can see
them; nothing in DM-only meetings. Invented names only: Ana (10), Luis (11), Marta (a Meet guest).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.discord_ui.render import RenderOptions, render_header
from meeting_scribe.discord_ui.sink import DiscordNotesSink
from meeting_scribe.domain.models import Speaker
from meeting_scribe.storage.artifacts import write_notes

from .fakes import BOT_USER_ID, FakeAdapter, FakeBot
from .test_task_sink import Svc, Views


@pytest.fixture
def env(tmp_path, meeting, notes):
    svc = Svc(tmp_path)
    m = replace(meeting, text_channel_id=None, channel_id="200", channel_name="Leadership",
                speakers=(Speaker("10", "Ana"), Speaker("11", "Luis"), Speaker(str(BOT_USER_ID), "Scribe", is_bot=True)))
    svc.repo.save_meeting(m)
    write_notes(svc.folder(m), m, notes, "es")
    bot = FakeBot()
    chat = bot.add(200, "Leadership", threads_ok=False)
    private = bot.add(700, "leadership-notes", public=False)
    for uid in (10, 11):
        bot.guild.add_member(uid)
    bot.user(10)
    bot.user(11)
    adapter = FakeAdapter(bot)
    cfg: dict = {}
    state = {"loop": None}

    def make():
        s = settings_from_mapping(cfg)
        return DiscordNotesSink(
            settings=lambda space=None: s, service=lambda: svc, adapter=lambda: adapter, loop=lambda: state["loop"],
            options=lambda mm: RenderOptions(lang="en", kanban_on=False, linear_on=False,
                                             is_owner_item=lambda i: False),
            views=Views(), timeout=5)
    yield SimpleNamespace(svc=svc, meeting=m, notes=notes, bot=bot, chat=chat, private=private, cfg=cfg,
                          state=state, make=make)
    svc.repo.close()


async def deliver(env, ok=True):
    env.state["loop"] = asyncio.get_running_loop()
    env.meeting = env.svc.repo.get_meeting(env.meeting.id)
    res = await asyncio.to_thread(env.make().deliver, env.meeting, env.notes, env.svc.folder(env.meeting))
    assert res.ok is ok, res.errors
    return res


def allowed(msg):
    return msg.sent_kwargs.get("allowed_mentions")


async def test_first_message_mentions_the_humans_with_exactly_those_pings(env):
    await deliver(env)
    first = env.chat.ordered()[0]
    assert "Participants: <@10>, <@11>" in first.content
    assert allowed(first) == {"users": ("10", "11"), "roles": False, "everyone": False}
    assert f"<@{BOT_USER_ID}>" not in first.content and "Scribe" not in first.content
    assert "@everyone" not in first.content and "@here" not in first.content


async def test_a_reprocess_edits_without_pinging_again(env):
    await deliver(env)
    first = env.chat.ordered()[0]
    await deliver(env)
    assert [m.id for m in env.chat.ordered()][0] == first.id
    assert "Participants: <@10>, <@11>" in first.content  # same text
    assert first.edit_kwargs[-1]["allowed_mentions"]["users"] == ()  # no ping on the edit


async def test_a_long_summary_mentions_only_in_its_first_part(env):
    long_notes = replace(env.notes, decisions=tuple(f"Decision {i} " + "x" * 80 for i in range(60)))
    env.notes = long_notes
    await deliver(env)
    parts = [m for m in env.chat.ordered() if "Decision" in m.content or "Participants" in m.content]
    assert len(parts) > 1
    assert allowed(parts[0])["users"] == ("10", "11")
    assert all(allowed(p)["users"] == () and "<@" not in p.content for p in parts[1:])


async def test_meet_attendees_are_mentioned_through_person_links_and_others_by_name(env):
    env.svc.repo.save_meeting(replace(env.meeting, speakers=(Speaker("gmeet:users/1", "Ana Gómez"),
                                                             Speaker("gmeet:users/2", "@everyone Marta"))))
    env.svc.repo.set_link("main", "10", name="ana gómez")
    await deliver(env)
    first = env.chat.ordered()[0]
    assert "<@10>" in first.content and allowed(first)["users"] == ("10",)
    assert "@\u200beveryone Marta" in first.content  # a typed name never pings


async def test_private_channel_mentions_only_members_who_can_see_it(env):
    env.cfg["meeting_routes"] = ["Leadership = 700:private"]
    env.private.viewers = {11}
    await deliver(env)
    first = env.private.ordered()[0]
    assert "<@11>" in first.content and "<@10>" not in first.content and "Ana" in first.content
    assert allowed(first)["users"] == ("11",)


async def test_private_channel_names_members_the_bot_cannot_check(env):
    env.cfg["meeting_routes"] = ["Leadership = 700:private"]
    env.bot.guild.members.pop(10)
    env.private.viewers = {10, 11}
    await deliver(env)
    assert allowed(env.private.ordered()[0])["users"] == ("11",)


async def test_setting_off_adds_no_line_and_default_mentions(env):
    env.cfg["delivery_mention_participants"] = False
    await deliver(env)
    first = env.chat.ordered()[0]
    assert "Participants" not in first.content and allowed(first) is None


async def test_dm_only_meetings_have_no_participants_line(env):
    env.cfg["meeting_routes"] = ["Leadership = :dm"]
    await deliver(env)
    assert env.chat.ordered() == [] and env.private.ordered() == []
    for uid in (10, 11):
        first = env.bot.users[uid].dm.ordered()[0]
        assert "Participants" not in first.content and allowed(first) is None


def test_render_header_without_participants_keeps_the_default_policy(meeting, notes):
    assert all(s.mentions is None for s in render_header(meeting, notes, "en"))
    specs = render_header(meeting, notes, "es", "-# 👥 Participantes: <@10>", ("10",))
    assert specs[0].mentions == ("10",) and "Participantes: <@10>" in specs[0].content


def test_the_notes_pointer_remembers_the_line(env):
    async def run():
        await deliver(env)
    asyncio.run(run())
    row = env.svc.repo.get_delivery("discord", f"mtg:{env.meeting.id}:notes")
    assert json.loads(row["external_id"])["people"].startswith("-# 👥 Participants: <@10>")


async def test_a_forum_post_mentions_in_its_opening_message(env):
    forum = env.bot.add_forum(710, "team-notes")
    env.cfg["meeting_routes"] = ["Leadership = 710"]
    await deliver(env)
    post = forum.posts[0]
    first = post.ordered()[0]
    assert "Participants: <@10>, <@11>" in first.content and allowed(first)["users"] == ("10", "11")


def test_the_setting_is_space_scoped_on_by_default_and_in_the_desktop_schema():
    from meeting_scribe.config import SPEC, config_schema, settings_from_mapping

    assert SPEC["delivery_mention_participants"].scope == "space"
    assert settings_from_mapping({}).delivery_mention_participants is True
    for lang, label in (("en", "Mention participants"), ("es", "Mencionar a los participantes")):
        field = next(f for f in config_schema(lang)["fields"] if f["key"] == "delivery_mention_participants")
        assert field["group"] == "delivery" and field["type"] == "bool" and field["label"] == label


async def test_a_deleted_summary_is_posted_again_with_the_line_but_no_ping(env):
    await deliver(env)
    first = env.chat.ordered()[0]
    await first.delete()
    await deliver(env)
    again = next(m for m in env.chat.ordered() if "Participants" in m.content)
    assert again.id != first.id and "Participants: <@10>, <@11>" in again.content
    assert allowed(again)["users"] == ()
