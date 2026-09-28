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
