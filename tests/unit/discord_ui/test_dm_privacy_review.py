"""Regressions of the privacy review of direct-messages meetings and participant mentions (DESIGN §19.3,
§19.4). Invented names only; fixtures of ``test_dm_routes`` (Luis 11 with open DMs, Ana 10 closed)."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from meeting_scribe import privacy
from meeting_scribe.discord_ui.auth import check_dm
from meeting_scribe.domain.models import ActionItem, Speaker

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


async def test_nothing_is_anchored_while_nobody_got_the_copy(env):
    env.luis.dms_open = False
    res = await deliver(env)
    assert res.waiting
    assert not (privacy.record(env.svc.repo, env.meeting.id) or {}).get("recipients")


async def test_the_recipients_are_worked_out_again_until_someone_got_it(env):
    """Nobody reachable at first; a participant who becomes reachable later is included (never frozen out)."""
    env.luis.dms_open = False
    env.svc.repo.save_meeting(replace(env.meeting, speakers=(Speaker("11", "Luis"),)))
    env.meeting = env.svc.repo.get_meeting(env.meeting.id)
    assert (await deliver(env)).waiting
    env.svc.repo.save_meeting(replace(env.meeting, speakers=(Speaker("11", "Luis"), Speaker("12", "Marta"))))
    env.meeting = env.svc.repo.get_meeting(env.meeting.id)
    marta = env.bot.user(12)
    res = await deliver(env)
    assert res.ok and SUMMARY_WORD in texts(marta.dm.ordered()), res.errors
    assert privacy.record(env.svc.repo, env.meeting.id)["recipients"] == ["11", "12"]


async def test_a_dm_task_offers_no_move_and_a_move_click_is_refused(env):
    await deliver(env)
    task = next(m for m in env.luis.dm.ordered() if "**Landing page**" in m.content)
    assert not any(":prj:" in c for c in (task.view or ()))
    it = ActionItem(id="a1", title="x", owner_speaker_id="11")
    click = SimpleNamespace(user=SimpleNamespace(id=11), guild=None, channel=SimpleNamespace(id=env.luis.dm.id),
                            channel_id=env.luis.dm.id)
    for action in ("prj", "tsel"):
        assert not check_dm(click, {"11": str(env.luis.dm.id)}, action, it, "en").allowed


async def test_a_dm_meeting_never_moves_a_task_into_a_channel(env):
    secret = env.bot.add(777, "board-secret-plans", public=False)
    secret.viewers = {99}
    await deliver(env)
    sink = env.make()
    assert await sink.move_options(env.meeting.id, "a1", viewer="11") == []
    with pytest.raises(LookupError):
        await sink.move_item(env.meeting.id, "a1", "777", viewer="11", learn=False)
    await sink.share(env.meeting.id, "a1", "project")
    assert "Landing page" not in texts(secret.ordered())
