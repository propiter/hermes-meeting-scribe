"""Regressions of the privacy review of direct-messages meetings and participant mentions (DESIGN §19.3,
§19.4). Invented names only; fixtures of ``test_dm_routes`` (Luis 11 with open DMs, Ana 10 closed)."""
from __future__ import annotations

from dataclasses import replace

from meeting_scribe.domain.models import Speaker

from .test_dm_routes import SUMMARY_WORD, deliver, env, texts  # noqa: F401 - fixture reuse


class NotFound(Exception):
    """discord.py's ``NotFound`` for a deleted account: HTTP 404, code 10013 "Unknown User"."""
    status, code = 404, 10013


async def test_a_deleted_discord_account_is_skipped_and_the_others_still_get_their_copy(env):
    env.svc.repo.save_meeting(replace(env.meeting, speakers=(Speaker("5", "Gone"), Speaker("11", "Luis"))))
    env.meeting = env.svc.repo.get_meeting(env.meeting.id)

    async def fetch_user(uid):
        if int(uid) == 5:
            raise NotFound("Unknown User")
        return env.bot.users[int(uid)]
    env.bot.fetch_user = fetch_user
    res = await deliver(env)
    assert res.ok, res.errors
    assert SUMMARY_WORD in texts(env.luis.dm.ordered())
