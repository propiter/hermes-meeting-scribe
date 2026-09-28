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
