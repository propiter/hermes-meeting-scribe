"""Text the plugin does not write (a summary, a task title, a typed name) never notifies anyone: every
message names exactly the users it may ping, nobody by default (DESIGN §19.4)."""
from __future__ import annotations

import asyncio

from meeting_scribe.discord_ui.publisher import Messages
from meeting_scribe.discord_ui.render import MessageSpec
from meeting_scribe.discord_ui.views import ViewKit

from .fakes import FakeBot


class LooseDefault:
    """A view factory whose DEFAULT policy would ping every user in the text."""

    def send_kwargs(self):
        return {"allowed_mentions": "users=True"}

    def mention_kwargs(self, users):
        return {"allowed_mentions": {"users": tuple(users), "roles": False, "everyone": False}}

    def view(self, buttons):
        return None


def test_a_message_without_named_users_never_uses_a_policy_that_pings_the_text():
    channel = FakeBot().add(1, "general")
    msgs = Messages(adapter=None, views=LooseDefault())
    msg = asyncio.run(msgs.send(channel, spec=MessageSpec("Ask <@66> about it")))
    assert msg.sent_kwargs["allowed_mentions"] == {"users": (), "roles": False, "everyone": False}
    msg = asyncio.run(msgs.send(channel, spec=MessageSpec("<@11> <@66>", mentions=("11",))))
    assert msg.sent_kwargs["allowed_mentions"]["users"] == ("11",)


def test_the_discord_views_ping_nobody_by_default():
    kit = ViewKit(handler=None)
    for rule in (kit.send_kwargs()["allowed_mentions"], kit.mention_kwargs(())["allowed_mentions"]):
        assert rule.users is False or list(rule.users) == []
        assert rule.roles is False and rule.everyone is False
