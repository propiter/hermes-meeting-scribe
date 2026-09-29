"""Regressions of the privacy review of meeting routes (DESIGN §19.2): a private meeting never leaks
out of its private channel — not by a rule edit, a renamed channel, a refused delete, an unusable
channel or an unreadable rule. Invented names only (see ``test_private_routes``)."""
from __future__ import annotations

import asyncio

import pytest

from meeting_scribe import privacy

from .test_private_routes import SUMMARY_WORD, env, outside, texts  # noqa: F401  (fixture)


def public_msgs(env):
    return [m for m in outside(env) if not m.channel.is_dm]


async def run_deliver(env):
    env.state["loop"] = asyncio.get_running_loop()
    sink = env.make()
    return await asyncio.to_thread(sink.deliver, env.meeting, env.notes, env.svc.folder(env.meeting))


# F7 -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("entry", ["Leadership:private", "Leadership -> 700:private", "<#200> = 700:private"])
async def test_unreadable_private_rule_holds_the_meeting_instead_of_publishing_it(env, entry):
    env.cfg["meeting_routes"] = [entry]
    res = await run_deliver(env)
    assert not res.ok and res.waiting and "meeting_routes" in res.errors[0]
    assert [m for c in env.bot.channels.values() for m in c.ordered()] == []
    assert privacy.record(env.svc.repo, env.meeting.id) is None  # waits; not private for good


# F4 -----------------------------------------------------------------------------------------------
async def test_renamed_channel_never_carries_a_private_meeting_into_a_public_one(env):
    """Rule by NAME; the private channel is renamed and a public channel takes the name. The meeting stays
    anchored to 700: the delivery waits with the reason, and buttons are only accepted from 700."""
    env.cfg["meeting_routes"] = ["Leadership = #leadership-notes:private"]
    res = await run_deliver(env)
    assert res.ok and privacy.record(env.svc.repo, env.meeting.id)["channel"] == "700"
    env.private.name = "leadership-archive"
    public = env.bot.add(800, "leadership-notes")
    res = await run_deliver(env)
    assert not res.ok and res.waiting and "private-move" in res.errors[0] and "<#700>" in res.errors[0]
    assert public.ordered() == [] and SUMMARY_WORD in texts(env.private.ordered())
    place = await env.make().private_place(env.meeting.id)
    assert "800" not in place and "700" in place


async def test_rule_edited_to_another_channel_holds_the_meeting_until_the_admin_moves_it(env):
    await run_deliver(env)
    public = env.bot.add(800, "town-square")
    env.cfg["meeting_routes"] = ["Leadership = 800:private"]
    res = await run_deliver(env)
    assert res.waiting and public.ordered() == []
    assert "800" not in await env.make().private_place(env.meeting.id)
    await env.make().refresh(env.meeting.id)  # a button refresh keeps working in the anchored channel
    assert public.ordered() == [] and SUMMARY_WORD in texts(env.private.ordered())
    # the explicit admin move: the meeting goes to the new channel and leaves the old one
    privacy.anchor(env.svc.repo, env.meeting.id, "800")
    res = await run_deliver(env)
    assert res.ok and SUMMARY_WORD in texts(public.ordered()) and SUMMARY_WORD not in texts(env.private.ordered())
    assert "700" not in await env.make().private_place(env.meeting.id)


# F5 -----------------------------------------------------------------------------------------------
async def test_unusable_rule_never_deletes_the_private_copy_itself(env):
    """The rule is edited to a channel that cannot be used: a refresh (share all, 📁, a button) keeps the
    summary, index and transcript in the anchored private channel, and a delivery only waits."""
    await run_deliver(env)
    before = texts(env.private.ordered())
    env.cfg["meeting_routes"] = ["Leadership = 799:private"]
    await env.make().refresh(env.meeting.id)
    res = await run_deliver(env)
    assert res.waiting and SUMMARY_WORD in texts(env.private.ordered())
    assert texts(env.private.ordered()) == before


async def test_withdraw_keeps_the_anchored_channel_even_if_the_rule_points_nowhere(env):
    """The publisher's own place of a private meeting is its anchor, not the rule's current targets."""
    await run_deliver(env)
    env.cfg["meeting_routes"] = []
    sink = env.make()
    got = await sink.board(env.meeting.id)
    assert got is not None
    pub = got[0]
    from meeting_scribe.discord_ui.publisher import Pointers
    assert "700" in await pub.private_place(env.meeting, Pointers(env.svc.repo, env.meeting.id))


# F1 / F2 / F3: withdrawing public copies ---------------------------------------------------------
from .fakes import FakeChannel, FakeHTTPError  # noqa: E402
from .test_private_routes import QUOTE  # noqa: E402


def forbid_thread_delete(monkeypatch):
    """Discord refuses deleting threads/posts without Manage Threads, even the bot's own."""
    orig = FakeChannel.delete

    async def delete(self):
        if self.parent is not None:
            raise FakeHTTPError(403, 50013, "Missing Permissions")
        await orig(self)
    monkeypatch.setattr(FakeChannel, "delete", delete)


def public_threads(env):
    return [c for c in env.bot.channels.values() if c.parent is not None and c.parent is not env.private
            and not c.is_dm]


async def test_public_forum_post_is_emptied_and_renamed_when_it_cannot_be_deleted(env, monkeypatch):
    forum = env.bot.add_forum(730, "meeting-notes")
    env.cfg.update({"meeting_routes": [], "delivery_discord_channel": "730"})
    await run_deliver(env)
    [post] = forum.posts
    forbid_thread_delete(monkeypatch)
    env.cfg["meeting_routes"] = ["Leadership = 700:private"]
    assert (await run_deliver(env)).ok
    assert SUMMARY_WORD not in texts(post.ordered()) and QUOTE not in texts(post.ordered())
    assert all(m.content == "🔒 Content withdrawn." and m.view is None and m.file is None for m in post.ordered())
    assert post.name == "Content withdrawn" and post.archived and post.thread_edits[-1].get("locked")
    assert SUMMARY_WORD in texts(env.private.ordered())


async def test_notes_and_project_threads_are_emptied_and_renamed_when_they_cannot_be_deleted(env, monkeypatch):
    env.cfg["meeting_routes"] = []
    env.chat.threads_ok = True
    await run_deliver(env)
    assert public_threads(env)
    forbid_thread_delete(monkeypatch)
    env.cfg["meeting_routes"] = ["Leadership = 700:private"]
    assert (await run_deliver(env)).ok
    for thread in public_threads(env):
        assert thread.name == "Content withdrawn"
        assert all(m.content == "🔒 Content withdrawn." for m in thread.ordered()), thread.name
    assert SUMMARY_WORD not in texts(public_msgs(env)) and QUOTE not in texts(public_msgs(env))


async def test_messages_that_cannot_be_deleted_or_emptied_are_retried_and_reported(env, monkeypatch):
    from meeting_scribe.discord_ui.withdraw import PENDING_KV
    from .fakes import FakeMessage

    env.cfg.update({"meeting_routes": [], "delivery_tasks_placement": "projects_inline"})
    await run_deliver(env)
    orion = [m.id for m in env.orion.ordered()]
    real_delete, real_edit = FakeMessage.delete, FakeMessage.edit

    async def refuse(self, **kw):
        if self.id in orion:
            raise FakeHTTPError(503, 0, "Service Unavailable")
        return await real_edit(self, **kw)

    async def refuse_delete(self):
        if self.id in orion:
            raise FakeHTTPError(503, 0, "Service Unavailable")
        await real_delete(self)
    monkeypatch.setattr(FakeMessage, "edit", refuse)
    monkeypatch.setattr(FakeMessage, "delete", refuse_delete)
    env.cfg["meeting_routes"] = ["Leadership = 700:private"]
    await run_deliver(env)
    pending = env.svc.repo.kv_get(PENDING_KV + env.meeting.id)
    assert pending and "Manage Messages" in pending
    monkeypatch.setattr(FakeMessage, "edit", real_edit)
    monkeypatch.setattr(FakeMessage, "delete", real_delete)
    await run_deliver(env)
    assert env.orion.ordered() == [] and env.svc.repo.kv_get(PENDING_KV + env.meeting.id) is None


async def test_rule_to_an_unusable_channel_withdraws_public_task_messages(env):
    env.cfg.update({"meeting_routes": [], "delivery_tasks_placement": "projects_inline"})
    await run_deliver(env)
    assert QUOTE in texts(env.orion.ordered())
    env.cfg["meeting_routes"] = ["Leadership = 799:private"]
    res = await run_deliver(env)
    assert res.waiting and privacy.record(env.svc.repo, env.meeting.id) is not None
    assert QUOTE not in texts(public_msgs(env)) and "Landing page" not in texts(public_msgs(env))


@pytest.mark.parametrize("fallback", ["", "600"])  # the voice chat, or the fallback channel
async def test_unrouted_task_is_withdrawn_while_the_meeting_waits(env, fallback):
    env.cfg.update({"meeting_routes": [], "delivery_fallback_channel": fallback})
    await run_deliver(env)
    assert "Budget" in texts(public_msgs(env))
    env.cfg["meeting_routes"] = ["Leadership = #leadership-notez:private"]
    res = await run_deliver(env)
    assert res.waiting and "Budget" not in texts(public_msgs(env))
    assert all(not dm.ordered() or "Landing page" not in texts(dm.ordered())
               for dm in (u.dm for u in env.bot.users.values()))
