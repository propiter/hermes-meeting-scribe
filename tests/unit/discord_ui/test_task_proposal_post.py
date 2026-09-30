"""The confirmation the agent's write tools post in a shared conversation (DESIGN §16.3): posted on the
gateway loop from the tool's thread, with ✅ Confirm / ✖ Cancel persistent buttons, notifying nobody."""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

from meeting_scribe import discord_ui
from meeting_scribe.discord_ui.render import TEMPLATE
from meeting_scribe.discord_ui.views import BUTTON_TEMPLATE

from .fakes import FakeAdapter, FakeBot
from .test_task_sink import Views


class _Runtime:
    """Weak-referenceable, like the real runtime."""


async def test_the_confirmation_is_posted_with_its_buttons_and_pings_nobody():
    bot = FakeBot()
    thread = bot.add(7001, "weekly")
    adapter = FakeAdapter(bot)
    runtime = _Runtime()
    discord_ui._STATES[runtime] = SimpleNamespace(adapter=adapter, kit=Views(), loop=asyncio.get_running_loop())
    post = discord_ui.proposer_for(runtime)
    message_id = await asyncio.to_thread(post, "7001", "📝 “Fix the mail” → Luis", "k3v7q2ab", "0a1b2c3d4e5f6a7b", "en")
    [msg] = thread.ordered()
    assert message_id == str(msg.id) and msg.content == "📝 “Fix the mail” → Luis"
    assert msg.view == ("mscribe:pok:k3v7q2ab:0a1b2c3d4e5f6a7b", "mscribe:pno:k3v7q2ab:0a1b2c3d4e5f6a7b")
    assert all(re.fullmatch(TEMPLATE, cid) and re.fullmatch(BUTTON_TEMPLATE, cid) for cid in msg.view)
    assert msg.sent_kwargs["allowed_mentions"] == {"users": (), "roles": False, "everyone": False}
    assert await asyncio.to_thread(post, "9999", "x", "k3v7q2ab", "0a1b2c3d4e5f6a7b", "en") is None  # unknown chat
    assert post("7001", "x", "k3v7q2ab", "0a1b2c3d4e5f6a7b", "en") is None  # never blocks the loop on itself
    assert discord_ui.proposer_for(_Runtime()) is None  # Discord not connected
