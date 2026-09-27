"""Notes an older version posted in a DM (DESIGN §19): they stay there until an EXPLICIT
``reprocess <id> --from deliver`` with a configured channel moves them, server first, DM last.
Invented names only."""
from __future__ import annotations

import asyncio
import json

from meeting_scribe.domain.models import KV_DM_NOTES, KV_MOVE_FROM_DM, Utterance
from meeting_scribe.storage.artifacts import write_transcript

from .test_task_sink import as_meet, env, ptr  # noqa: F401 - fixture reuse
from .test_transcript_attachment import files_in, tenv  # noqa: F401 - fixture reuse


async def run(env):
    env.state["loop"] = asyncio.get_running_loop()
    sink = env.make()
    return await asyncio.to_thread(sink.deliver, env.meeting, env.notes, env.svc.folder(env.meeting))


def request_move(env):
    env.svc.repo.kv_set(KV_MOVE_FROM_DM + env.meeting.id, "1")


def save(env, suffix, value):
    env.svc.repo.upsert_delivery(env.meeting.id, "discord", f"mtg:{env.meeting.id}:{suffix}", url="",
                                 external_id=json.dumps(value))


async def seed_dm(env, *, attach=True, legacy_transcript=False, with_task=False):
    """What an older version left: summary + index (and optionally a task) in the owner's DM."""
    dm = env.bot.user(42).dm
    old = await dm.send("old summary")
    save(env, "notes", {"v": 2, "channel": dm.id, "thread": None, "messages": [old.id], "url": old.jump_url,
                        "attach": attach})
    if with_task:
        task = await dm.send("old task Budget")
        save(env, "task:a3", {"channel": dm.id, "message": task.id, "target": ""})
    old_index = await dm.send("old index")
    save(env, "index", {"channel": dm.id, "message": old_index.id})
    if legacy_transcript:
        save(env, "transcript", {"skipped": "legacy", "done": True})
    return dm


def dm_texts(dm):
    return [m.content for m in dm.ordered()]


# -- stays where it is unless asked --------------------------------------------------------------
async def test_a_normal_delivery_keeps_editing_the_notes_in_the_dm(env):
    as_meet(env)
    dm = await seed_dm(env)
    notes_ch = env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    res = await run(env)
    assert res.ok, res.errors
    assert "Migración SMTP" in dm.ordered()[0].content and notes_ch.ordered() == []
    assert ptr(env, "notes")["channel"] == dm.id
    reason = env.svc.repo.kv_get(KV_DM_NOTES + env.meeting.id)
    assert reason and f"reprocess {env.meeting.id} --from deliver" in reason


async def test_button_refresh_never_moves_even_when_a_move_was_requested(env):
    """P4: sink.refresh (buttons) and moves re-render in place; only DELIVER moves."""
    as_meet(env)
    dm = await seed_dm(env)
    notes_ch = env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    request_move(env)
    env.state["loop"] = asyncio.get_running_loop()
    sink = env.make()
    await sink.refresh(env.meeting.id)
    assert notes_ch.ordered() == [] and "Migración SMTP" in dm.ordered()[0].content
    assert env.svc.repo.kv_get(KV_MOVE_FROM_DM + env.meeting.id) == "1"  # still pending for the reprocess
    panel = await sink.task_panel(env.meeting.id, "11", "m", 0, is_owner=True)
    assert panel is not None  # 📋 My tasks keeps working for a meeting that lives in a DM


# -- the explicit move -----------------------------------------------------------------------------
async def test_requested_move_publishes_in_the_configured_channel_then_cleans_the_dm(env):
    as_meet(env)
    dm = await seed_dm(env, with_task=True)
    notes_ch = env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    request_move(env)
    res = await run(env)
    assert res.ok, res.errors
    assert dm.ordered() == []
    msgs = notes_ch.ordered()
    assert "Migración SMTP" in msgs[0].content and msgs[-1].view == ("mscribe:mine:k3v7q2ab:all",)
    thread = next(c for c in env.bot.channels.values() if c.parent is notes_ch)  # tasks without project
    assert any("Budget" in m.content for m in thread.ordered())
    assert ptr(env, "notes")["channel"] == 650 and ptr(env, "index")["channel"] == 650
    assert ptr(env, "dm_move") is None
    assert env.svc.repo.kv_get(KV_MOVE_FROM_DM + env.meeting.id) is None
    assert env.svc.repo.kv_get(KV_DM_NOTES + env.meeting.id) is None
    before = len(notes_ch.messages)
    request_move(env)
    assert (await run(env)).ok  # idempotent: nothing to move any more, nothing duplicated
    assert len(notes_ch.messages) == before


async def test_unreachable_configured_channel_leaves_the_dm_intact(env):
    """P1 / C1: the server channel is checked BEFORE anything in the DM is touched."""
    as_meet(env)
    dm = await seed_dm(env)
    env.cfg["google_meet_discord_channel"] = "777777777777"  # deleted / no access
    request_move(env)
    res = await run(env)
    assert not res.ok and "DM" in res.errors[0] and "777777777777" in res.errors[0]
    assert dm_texts(dm) == ["old summary", "old index"]
    assert ptr(env, "notes")["channel"] == dm.id and ptr(env, "index")["channel"] == dm.id
    assert env.svc.repo.kv_get(KV_MOVE_FROM_DM + env.meeting.id) == "1"  # the retry tries again


async def test_a_dm_id_configured_as_the_channel_is_not_a_move_target(env):
    """P1b: the DM's own id in delivery_discord_channel is ignored; nothing is deleted."""
    as_meet(env)
    dm = await seed_dm(env)
    env.cfg["delivery_discord_channel"] = str(dm.id)
    request_move(env)
    res = await run(env)
    assert res.ok, res.errors
    assert "Migración SMTP" in dm.ordered()[0].content and ptr(env, "notes")["channel"] == dm.id


async def test_without_an_explicit_channel_the_move_is_refused_and_explained(env):
    """P3 / I1: an automatic channel (system channel) never receives a meeting from a DM."""
    as_meet(env)
    dm = await seed_dm(env)
    general = env.bot.add(660, "general")
    env.bot.guild.system_channel_id = 660
    request_move(env)
    res = await run(env)
    assert res.ok, res.errors
    assert general.ordered() == [] and "Migración SMTP" in dm.ordered()[0].content
    reason = env.svc.repo.kv_get(KV_DM_NOTES + env.meeting.id)
    assert "config set google_meet_discord_channel" in reason and "--from deliver" in reason
    assert env.svc.repo.kv_get(KV_MOVE_FROM_DM + env.meeting.id) is None  # refused: asked again later


async def test_an_interrupted_move_resumes_without_duplicates_and_the_dm_survives_until_the_end(env):
    as_meet(env)
    dm = await seed_dm(env)
    notes_ch = env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    request_move(env)
    orig_send = type(notes_ch).send
    failed = {"done": False}

    async def flaky(self, content="", **kw):
        if self is notes_ch and "📋" in content and not failed["done"]:  # summary posted, index fails
            failed["done"] = True
            raise RuntimeError("503 Service Unavailable")
        return await orig_send(self, content, **kw)
    type(notes_ch).send = flaky
    try:
        res = await run(env)
    finally:
        type(notes_ch).send = orig_send
    assert not res.ok
    assert dm_texts(dm) == ["old summary", "old index"]  # nothing deleted before the end
    assert ptr(env, "notes")["channel"] == 650  # the new summary is remembered: no duplicate
    res = await run(env)
    assert res.ok, res.errors
    assert dm.ordered() == []
    assert sum("Migración SMTP" in m.content for m in notes_ch.ordered()) == 1


# -- the transcript keeps its original intent ------------------------------------------------------
async def test_a_legacy_meeting_never_gets_its_transcript_attached_by_the_move(tenv):
    """P2 / I1."""
    env = tenv
    as_meet(env)
    write_transcript(env.svc.folder(env.meeting), [Utterance(0.0, 2.0, "10", "Ana", "Secreto interno.")])
    await seed_dm(env, legacy_transcript=True)
    notes_ch = env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    request_move(env)
    assert (await run(env)).ok
    assert notes_ch.ordered() and files_in(notes_ch) == []
    assert ptr(env, "transcript") == {"skipped": "legacy", "done": True}


async def test_a_summary_posted_without_attachment_intent_stays_without_transcript(tenv):
    env = tenv
    as_meet(env)
    await seed_dm(env, attach=False)
    notes_ch = env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    request_move(env)
    assert (await run(env)).ok
    assert notes_ch.ordered() and files_in(notes_ch) == []


async def test_a_transcript_attached_in_the_dm_moves_with_the_notes(tenv):
    env = tenv
    as_meet(env)
    dm = await seed_dm(env)
    env.state["loop"] = asyncio.get_running_loop()
    # the older version attached it in the DM too
    old_file = await dm.send("📎 Full transcript", file={"name": "t.md", "data": b"x"})
    from meeting_scribe.discord_ui.transcript_file import content_digest
    text = env.make()._transcript_text(env.meeting)
    save(env, "transcript", {"sha256": content_digest(text), "channel": dm.id, "messages": [old_file.id],
                             "parts": 1, "done": True, "sending": None})
    notes_ch = env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    request_move(env)
    assert (await run(env)).ok
    assert dm.ordered() == [] and len(files_in(notes_ch)) == 1


async def test_a_button_refresh_during_an_unfinished_move_restores_the_dm_copy(env):
    """The move started but the new summary was never posted: buttons roll back to the DM (still intact)."""
    from meeting_scribe.discord_ui.task_publisher import MOVE_SUFFIX

    as_meet(env)
    dm = await seed_dm(env)
    notes_ch = env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    request_move(env)
    notes_ch.fail_sends = 1  # the very first send (the summary) fails
    assert not (await run(env)).ok
    assert ptr(env, MOVE_SUFFIX) is not None and ptr(env, "notes") is None
    env.state["loop"] = asyncio.get_running_loop()
    await env.make().refresh(env.meeting.id)
    assert ptr(env, MOVE_SUFFIX) is None and ptr(env, "notes")["channel"] == dm.id
    assert "Migración SMTP" in dm.ordered()[0].content and notes_ch.ordered() == []
    res = await run(env)  # the reprocess retry (flag still set) starts the move again and finishes it
    assert res.ok, res.errors
    assert dm.ordered() == [] and "Migración SMTP" in notes_ch.ordered()[0].content


async def test_a_channel_that_disappears_mid_move_rolls_back_when_nothing_was_posted(env):
    from meeting_scribe.discord_ui.task_publisher import MOVE_SUFFIX

    as_meet(env)
    dm = await seed_dm(env)
    notes_ch = env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "650"
    request_move(env)
    notes_ch.fail_sends = 1
    assert not (await run(env)).ok
    del env.bot.channels[650]  # deleted before the retry
    res = await run(env)
    assert not res.ok and "DM copy is kept" in res.errors[0]
    assert ptr(env, MOVE_SUFFIX) is None and ptr(env, "notes")["channel"] == dm.id
    assert dm_texts(dm) == ["old summary", "old index"]
