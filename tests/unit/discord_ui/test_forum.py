"""Forum (type 15) and media (type 16) channels as notes, fallback and project destinations (DESIGN §19.1).

Invented names only. The fakes mirror discord.py 2.x: a forum has no ``send``; ``create_thread``
returns ``(thread, message)``; a post that requires a tag is refused with code 40067.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.discord_ui.destination import auto_channel, pick_tags, resolve
from meeting_scribe.discord_ui.guild import snapshot_channels

from .fakes import FakeBot
from .test_task_sink import as_meet, env, ptr  # noqa: F401 - fixture reuse
from .test_transcript_attachment import files_in, tenv  # noqa: F401 - fixture reuse

POST_NAME = "2026-09-26 · Migración SMTP"


async def deliver(env, sink=None, *, ok=True):  # noqa: F811
    env.state["loop"] = asyncio.get_running_loop()
    sink = sink or env.make()
    res = await asyncio.to_thread(sink.deliver, env.meeting, env.notes, env.svc.folder(env.meeting))
    if ok:
        assert res.ok, res.errors
    return sink, res


def counts(env):  # noqa: F811
    return {c.id: len(c.messages) for c in env.bot.channels.values()}


# -- resolution ---------------------------------------------------------------------------------------
@pytest.fixture
def meet(meeting):
    from meeting_scribe.domain.models import SOURCE_GOOGLE_MEET

    return replace(meeting, guild_id="", channel_id="gmeet:space1", text_channel_id=None, source=SOURCE_GOOGLE_MEET)


@pytest.mark.parametrize("media", [False, True])
def test_a_forum_or_media_channel_is_resolved_by_name(meet, media):
    bot = FakeBot()
    bot.add(300, "general")
    bot.add_forum(301, "📝・meeting-notes", media=media)
    d = resolve(bot, meet, settings_from_mapping({"google_meet_discord_channel": "#meeting-notes"}))
    assert d.targets[0] == "301"
    step = next(s for s in d.steps if s.key == "google_meet_discord_channel")
    assert step.status == "ok" and step.kind == ("media" if media else "forum")
    assert step.to_dict()["kind"] == step.kind  # stored in the report for doctor / config list


def test_a_forum_id_is_accepted_and_a_voice_channel_name_still_is_not(meet):
    bot = FakeBot()
    bot.add_forum(301, "notes")
    bot.add(302, "standup", kind="voice")
    d = resolve(bot, meet, settings_from_mapping({"google_meet_discord_channel": "301",
                                                  "delivery_discord_channel": "standup"}))
    assert d.targets[0] == "301"
    assert {s.key: s.status for s in d.steps}["delivery_discord_channel"] == "not_text"


def test_the_automatic_choice_accepts_a_forum_named_like_the_defaults():
    bot = FakeBot()
    bot.add(300, "random")
    forum = bot.add_forum(301, "🗒️ reuniones")
    step = auto_channel(bot.guild, ("reuniones",))
    assert step.channel_id == "301" and step.kind == "forum"
    forum.can_post_in_threads = False  # cannot post inside its own posts: not usable
    assert auto_channel(bot.guild, ("reuniones",)).status == "none"


def test_missing_forum_permissions_and_a_required_tag_are_reported(meet):
    bot = FakeBot()
    forum = bot.add_forum(301, "notes", tags=("Orion", "Infra"), require_tag=True, can_attach=False)
    forum.can_post_in_threads = False
    d = resolve(bot, meet, settings_from_mapping({"google_meet_discord_channel": "301"}))
    text = " ".join(d.warnings)
    assert "Send Messages in Threads" in text and "Attach Files" in text and "forum #notes" in text
    assert "requires a tag" in text and "delivery_forum_default_tag" in text and "Orion, Infra" in text
    d = resolve(bot, meet, settings_from_mapping({"google_meet_discord_channel": "301",
                                                  "delivery_forum_default_tag": ["infra"]}))
    assert not any("requires a tag" in w for w in d.warnings)


def test_a_project_forum_is_checked_too(meet):
    bot = FakeBot()
    bot.add(300, "general")
    forum = bot.add_forum(510, "orion", require_tag=True, tags=("Backend",))
    forum.can_post_in_threads = False
    d = resolve(bot, meet, settings_from_mapping({"project_channels": ["Orion=510"]}))
    text = " ".join(d.warnings)
    assert "project_channels[orion]" in text and "Send Messages in Threads" in text and "requires a tag" in text


def test_pick_tags_matches_decorated_names_caps_at_five_and_uses_the_default_only_when_required():
    bot = FakeBot()
    names = ("🚀 Orion", "ALPHA", "beta", "gamma", "delta", "epsilon", "Minutes")
    forum = bot.add_forum(301, "notes", tags=names)
    picked = pick_tags(forum, ["orion", "alpha", "Beta", "gamma", "delta", "epsilon", "unknown"])
    assert [t.name for t in picked] == ["🚀 Orion", "ALPHA", "beta", "gamma", "delta"]
    assert pick_tags(forum, ["unknown"], ["minutes"]) == []  # not required: no default
    forum.flags.require_tag = True
    assert [t.name for t in pick_tags(forum, ["unknown"], ["nope", "minutes"])] == ["Minutes"]
    assert pick_tags(forum, ["unknown"], ["nope"]) == []  # never invented


def test_the_guild_snapshot_lists_forums_as_postable_project_channels():
    bot = FakeBot()
    forum = bot.add_forum(510, "orion")
    media = bot.add_forum(511, "designs", media=True)
    by_id = {c.id: c for c in snapshot_channels(bot.guild, need_threads=True)}
    assert by_id["510"].kind == "forum" and by_id["510"].can_post
    assert by_id["511"].kind == "forum" and by_id["511"].can_post
    forum.can_post_in_threads = False
    media.can_post = False
    by_id = {c.id: c for c in snapshot_channels(bot.guild, need_threads=False)}
    assert not by_id["510"].can_post and not by_id["511"].can_post


# -- publishing: the notes forum --------------------------------------------------------------------------
@pytest.fixture
def fenv(tenv):  # noqa: F811
    """Notes in a forum (by id), transcript on disk, orion/nebula text project channels."""
    tenv.forum = tenv.bot.add_forum(700, "meeting-notes", tags=("Orion", "Minutes"))
    tenv.cfg["delivery_discord_channel"] = "700"
    return tenv


def post_of(forum, n=0):
    return forum.posts[n]


async def test_one_post_per_meeting_with_summary_transcript_tasks_and_index_inside(fenv):
    await deliver(fenv)
    [post] = fenv.forum.posts
    assert post.name == POST_NAME
    msgs = post.ordered()
    assert "Usar SES" in msgs[0].content and "¿Presupuesto?" in msgs[0].content  # decisions, open questions
    assert msgs[1].file["name"].endswith(".md")  # the transcript right after the summary, inside the post
    assert any("Budget" in m.content and m.view for m in msgs)  # task without project, with its buttons
    assert msgs[-1].view == ("mscribe:mine:k3v7q2ab:all",)  # the index, last
    assert fenv.chat.ordered() == []  # nothing in the voice chat
    notes = ptr(fenv, "notes")
    assert notes["channel"] == post.id and notes["forum"] == 700 and notes["messages"][0] == msgs[0].id
    assert notes["url"] == post.jump_url and notes["name"] == POST_NAME
    assert ptr(fenv, "index")["channel"] == post.id


async def test_republishing_edits_in_place_and_never_duplicates(fenv):
    sink, _ = await deliver(fenv)
    before = counts(fenv)
    await deliver(fenv, sink)
    await deliver(fenv, fenv.make())
    await sink.refresh(fenv.meeting.id)
    assert len(fenv.forum.posts) == 1 and counts(fenv) == before
    assert post_of(fenv.forum).ordered()[0].edits >= 3


async def test_a_deleted_post_is_created_again_with_its_transcript(fenv):
    await deliver(fenv)
    fenv.forum.delete_post(post_of(fenv.forum))
    await deliver(fenv)
    assert len(fenv.forum.posts) == 2
    new = post_of(fenv.forum, 1)
    assert "Usar SES" in new.ordered()[0].content and len(files_in(new)) == 1
    assert any("Budget" in m.content for m in new.ordered()) and new.ordered()[-1].view
    assert ptr(fenv, "notes")["channel"] == new.id and ptr(fenv, "index")["channel"] == new.id


async def test_a_deleted_first_message_is_posted_again_inside_the_same_post(fenv):
    await deliver(fenv)
    post = post_of(fenv.forum)
    await post.ordered()[0].delete()
    await deliver(fenv)
    first = post.messages[ptr(fenv, "notes")["messages"][0]]
    assert len(fenv.forum.posts) == 1 and "Usar SES" in first.content
    assert sum("Usar SES" in m.content for m in post.ordered()) == 1


async def test_a_long_summary_keeps_the_first_part_as_the_opening_message(fenv):
    long = replace(fenv.notes, decisions=tuple(f"Decisión número {i} " + "x" * 150 for i in range(30)))
    fenv.notes = long
    await deliver(fenv)
    post = post_of(fenv.forum)
    ids = ptr(fenv, "notes")["messages"]
    assert len(ids) >= 3 and ids[0] == post.ordered()[0].id and all(i in post.messages for i in ids)
    fenv.notes = replace(long, decisions=("Usar SES",))  # shorter: only the extra parts are deleted
    await deliver(fenv)
    assert ptr(fenv, "notes")["messages"] == ids[:1] and ids[0] in post.messages
    assert all(i not in post.messages for i in ids[1:]) and len(fenv.forum.posts) == 1


async def test_a_new_title_renames_the_post_in_place(fenv):
    await deliver(fenv)
    fenv.notes = replace(fenv.notes, meeting_title="Plan de correo")
    await deliver(fenv)
    post = post_of(fenv.forum)
    assert len(fenv.forum.posts) == 1 and post.name == "2026-09-26 · Plan de correo"
    assert ptr(fenv, "notes")["name"] == post.name


async def test_an_archived_post_is_unarchived_to_edit_it(fenv):
    await deliver(fenv)
    post = post_of(fenv.forum)
    post.archived = True
    await deliver(fenv)
    assert not post.archived and len(fenv.forum.posts) == 1


async def test_media_channels_behave_like_forums(tenv):  # noqa: F811
    media = tenv.bot.add_forum(701, "notes-gallery", media=True)
    tenv.cfg["delivery_discord_channel"] = "#notes-gallery"
    await deliver(tenv)
    [post] = media.posts
    assert post.name == POST_NAME and len(files_in(post)) == 1


# -- tags ---------------------------------------------------------------------------------------------
async def test_the_project_tag_and_the_configured_tags_are_applied(fenv):
    fenv.meeting = replace(fenv.meeting, project="orion")
    fenv.svc.repo.save_meeting(fenv.meeting)
    fenv.cfg["delivery_forum_tags"] = ["minutes", "not-there"]
    await deliver(fenv)
    assert [t.name for t in post_of(fenv.forum).applied_tags] == ["Orion", "Minutes"]


async def test_a_forum_that_requires_a_tag_uses_the_default_tag(fenv):
    fenv.forum.flags.require_tag = True
    fenv.cfg["delivery_forum_default_tag"] = ["minutes"]
    await deliver(fenv)
    assert [t.name for t in post_of(fenv.forum).applied_tags] == ["Minutes"]


async def test_a_refused_post_waits_with_the_reason_and_goes_nowhere_else(fenv):
    fenv.forum.flags.require_tag = True
    fenv.bot.add(630, "general")  # an automatic candidate exists: it must NOT be used
    _, res = await deliver(fenv, ok=False)
    assert not res.ok and res.waiting and res.deferred
    assert "requires a tag" in res.errors[0] and "delivery_forum_default_tag" in res.errors[0]
    assert fenv.forum.posts == [] and fenv.bot.channels[630].ordered() == [] and fenv.chat.ordered() == []
    assert all(u.dm.ordered() == [] for u in fenv.bot.users.values())  # never a DM
    fenv.cfg["delivery_forum_default_tag"] = ["Minutes"]
    await deliver(fenv)
    assert len(fenv.forum.posts) == 1


# -- project channels that are forums -----------------------------------------------------------------------
@pytest.fixture
def penv(env):  # noqa: F811
    """orion (501) replaced by a FORUM with the same id; notes in the voice chat as before."""
    del env.bot.channels[501]
    env.orion = env.bot.add_forum(501, "『🚀』orion", category_id=900, tags=("orion", "urgent"))
    return env


async def test_a_project_forum_gets_one_post_per_meeting_with_its_tasks(penv):
    await deliver(penv)
    [post] = penv.orion.posts
    anchor, task = post.ordered()
    assert post.name == POST_NAME and "Migración SMTP" in anchor.content
    assert "Landing page" in task.content and "mscribe:ok:k3v7q2ab:a1" in task.view
    assert [t.name for t in post.applied_tags] == ["orion"]  # the project's tag
    index = penv.chat.ordered()[-1]
    assert f"<#{post.id}>" in index.content  # the index links the post
    stored = ptr(penv, "thread:501")
    assert stored["thread"] == post.id and stored["message"] == anchor.id and stored["forum"] == 501


async def test_a_project_forum_is_edited_in_place_and_a_deleted_post_is_recreated(penv):
    sink, _ = await deliver(penv)
    before = counts(penv)
    await deliver(penv, sink)
    assert len(penv.orion.posts) == 1 and counts(penv) == before
    penv.orion.delete_post(penv.orion.posts[0])
    await deliver(penv, sink)
    assert len(penv.orion.posts) == 2
    assert any("Landing page" in m.content for m in penv.orion.posts[1].ordered())


async def test_a_project_forum_that_refuses_the_post_leaves_only_those_tasks_waiting(penv):
    penv.orion.flags.require_tag = True
    penv.orion.available_tags = []
    _, res = await deliver(penv, ok=False)
    assert res.waiting and "requires a tag" in res.errors[0]
    assert "Usar SES" in penv.chat.ordered()[0].content  # the notes and other tasks are posted
    assert not any("Landing page" in m.content for c in penv.bot.channels.values() for m in c.ordered()
                   if not getattr(c, "is_dm", False))  # never another channel
    neb = next(c for c in penv.bot.channels.values() if c.parent is penv.nebula)
    assert any("Contract review" in m.content for m in neb.ordered())


async def test_the_move_button_moves_a_task_into_a_project_forum_post(penv):
    sink, _ = await deliver(penv)
    options = dict(await sink.move_options(penv.meeting.id, "a2"))
    assert "501" in options  # forums are offered
    await sink.move_item(penv.meeting.id, "a2", "501")
    [post] = penv.orion.posts
    assert any("Contract review" in m.content for m in post.ordered())
    assert ptr(penv, "task:a2")["channel"] == post.id


async def test_the_move_button_moves_a_task_out_of_a_project_forum_post(penv):
    sink, _ = await deliver(penv)
    await sink.move_item(penv.meeting.id, "a1", "502")
    assert not any("Landing page" in m.content for m in penv.orion.posts[0].ordered())
    thread = next(c for c in penv.bot.channels.values() if c.parent is penv.nebula)
    assert any("Landing page" in m.content for m in thread.ordered())


async def test_the_fallback_channel_may_be_a_forum(env):  # noqa: F811
    backlog = env.bot.add_forum(620, "backlog")
    env.cfg["delivery_fallback_channel"] = "#backlog"
    await deliver(env)
    [post] = backlog.posts
    assert any("Budget" in m.content for m in post.ordered())


# -- DMs point to the post ------------------------------------------------------------------------------
async def test_assignee_dm_links_to_the_notes_post(fenv):
    sink, _ = await deliver(fenv)
    [dm] = fenv.bot.users[11].dm.ordered()
    header = dm.view[1]
    assert post_of(fenv.forum).jump_url in header and [b[0] for b in dm.view[2]] == ["a1"]
    fenv.forum.delete_post(post_of(fenv.forum))  # post recreated: the DM is edited with the new link
    await deliver(fenv, sink)
    [dm] = fenv.bot.users[11].dm.ordered()
    assert post_of(fenv.forum, 1).jump_url in dm.view[1]


async def test_a_button_refresh_edits_the_task_inside_the_post(fenv):
    from meeting_scribe.domain.models import ActionStatus

    sink, _ = await deliver(fenv)
    fenv.svc.repo.set_action_status(fenv.meeting.id, "a3", ActionStatus.DISMISSED)
    await sink.refresh_item(fenv.meeting.id, "a3")
    post = post_of(fenv.forum)
    budget = next(m for m in post.ordered() if "Budget" in m.content)
    assert "~~Budget~~" in budget.content and budget.view is None and len(fenv.forum.posts) == 1


async def test_the_destination_report_records_the_forum_kind(fenv):
    await deliver(fenv)
    raw = fenv.svc.repo.kv_get("discord.destination_report.discord")
    step = next(s for s in json.loads(raw)["steps"] if s["key"] == "delivery_discord_channel")
    assert step["kind"] == "forum" and step["channel_id"] == "700"


async def test_meet_notes_in_a_forum(fenv):
    as_meet(fenv)
    fenv.cfg.update({"delivery_discord_channel": "", "google_meet_discord_channel": "meeting-notes"})
    await deliver(fenv)
    [post] = fenv.forum.posts
    assert "Usar SES" in post.ordered()[0].content


async def test_the_move_button_refuses_a_forum_that_would_reject_the_post(penv):
    from meeting_scribe.discord_ui.actions import friendly_error
    from meeting_scribe.domain.errors import ForumTagRequired

    sink, _ = await deliver(penv)
    penv.orion.flags.require_tag = True
    penv.orion.available_tags = []
    with pytest.raises(ForumTagRequired) as err:
        await sink.move_item(penv.meeting.id, "a2", "501")
    assert "delivery_forum_default_tag" in friendly_error(err.value, "en")
    assert penv.orion.posts[0].ordered()[1:] and not any("Contract review" in m.content
                                                         for m in penv.orion.posts[0].ordered())
    assert penv.svc.repo.get_action_item(penv.meeting.id, "a2").project_key != "discord:501"  # nothing moved


async def test_a_fallback_or_project_channel_equal_to_the_notes_forum_uses_the_meeting_post(fenv):
    fenv.cfg.update({"delivery_fallback_channel": "700", "project_channels": ["Nebulla=700"]})
    await deliver(fenv)
    [post] = fenv.forum.posts
    contents = [m.content for m in post.ordered()]
    assert any("Budget" in c for c in contents), contents
    assert any("Contract review" in c for c in contents), contents
