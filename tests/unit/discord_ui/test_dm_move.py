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


MISSING = object()  # a pointer written before the key existed


async def seed_dm(env, *, attach: object = True, legacy_transcript=False, with_task=False):
    """What an older version left: summary + index (and optionally a task) in the owner's DM."""
    dm = env.bot.user(42).dm
    old = await dm.send("old summary")
    notes = {"v": 2, "channel": dm.id, "thread": None, "messages": [old.id], "url": old.jump_url}
    if attach is not MISSING:
        notes["attach"] = attach
    save(env, "notes", notes)
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


async def seed_dm_transcript(env, dm):
    """A transcript an older version really delivered in the DM (``done`` with its message)."""
    from meeting_scribe.discord_ui.transcript_file import content_digest

    env.state["loop"] = asyncio.get_running_loop()
    old_file = await dm.send("📎 Full transcript", file={"name": "t.md", "data": b"x"})
    text = env.make()._transcript_text(env.meeting)
    save(env, "transcript", {"sha256": content_digest(text), "channel": dm.id, "messages": [old_file.id],
                             "parts": 1, "done": True})  # no ``sending``: written before that key existed
    return old_file


def watch_deletes(dm, notes_ch):
    """For each message deleted from ``dm``: how many transcript files ``notes_ch`` had at that moment."""
    from . import fakes

    seen: list[tuple[str, int]] = []
    original = fakes.FakeMessage.delete

    async def delete(self):
        if self.channel is dm:
            seen.append((self.content, len(files_in(notes_ch))))
        return await original(self)
    fakes.FakeMessage.delete = delete
    return seen, lambda: setattr(fakes.FakeMessage, "delete", original)


async def test_a_delivered_transcript_moves_even_when_the_notes_pointer_predates_the_attach_key(tenv):
    """Production bug: a summary pointer without ``attach`` + a genuine transcript in the DM. The
    transcript is re-posted in the server channel, and only then is the DM copy deleted."""
    env = tenv
    as_meet(env)
    dm = await seed_dm(env, attach=MISSING)
    await seed_dm_transcript(env, dm)
    notes_ch = env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    request_move(env)
    seen, restore = watch_deletes(dm, notes_ch)
    try:
        res = await run(env)
    finally:
        restore()
    assert res.ok, res.errors
    assert len(files_in(notes_ch)) == 1 and dm.ordered() == []
    assert ("📎 Full transcript", 1) in seen  # the new one existed when the DM copy went away
    assert ptr(env, "transcript")["channel"] == 650 and ptr(env, "transcript")["done"] is True
    assert ptr(env, "notes")["attach"] is True  # the intent is now explicit for later deliveries


# -- privacy: the derived intent never attaches what was not attached before -----------------------
def test_transcript_intent_prefers_the_explicit_key_then_a_delivered_transcript():
    from meeting_scribe.discord_ui.task_publisher import transcript_intent

    delivered = {"channel": 5, "messages": [7], "done": True, "parts": 1, "sha256": "x"}
    assert transcript_intent({"attach": True}, None) is True
    assert transcript_intent({"attach": False}, delivered) is False  # explicit refusal wins
    assert transcript_intent({}, delivered) is True
    assert transcript_intent({}, {"skipped": "legacy", "done": True}) is False
    assert transcript_intent({}, None) is False
    assert transcript_intent({}, {**delivered, "done": False}) is False  # never finished: no proof
    assert transcript_intent({}, {**delivered, "messages": []}) is False
    assert transcript_intent(None, None) is False


async def test_an_old_pointer_with_the_legacy_marker_moves_without_transcript(tenv):
    env = tenv
    as_meet(env)
    await seed_dm(env, attach=MISSING, legacy_transcript=True)
    notes_ch = env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    request_move(env)
    res = await run(env)
    assert res.ok, res.errors
    assert notes_ch.ordered() and files_in(notes_ch) == []
    assert ptr(env, "transcript") == {"skipped": "legacy", "done": True}
    assert ptr(env, "notes")["attach"] is False


async def test_an_old_pointer_without_any_transcript_moves_without_transcript(tenv):
    env = tenv
    as_meet(env)
    dm = await seed_dm(env, attach=MISSING)
    notes_ch = env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    request_move(env)
    assert (await run(env)).ok
    assert notes_ch.ordered() and files_in(notes_ch) == [] and dm.ordered() == []
    assert ptr(env, "transcript") == {"skipped": "legacy", "done": True}


# -- the DM copy of something goes away only once its replacement exists ---------------------------
async def test_an_explicit_refusal_keeps_the_dm_transcript_and_says_why(tenv):
    """``attach: false`` + a transcript in the DM: never re-posted (privacy), and never deleted either."""
    env = tenv
    as_meet(env)
    dm = await seed_dm(env, attach=False)
    await seed_dm_transcript(env, dm)
    notes_ch = env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    request_move(env)
    res = await run(env)
    assert res.ok, res.errors
    assert files_in(notes_ch) == [] and dm_texts(dm) == ["📎 Full transcript"]
    reason = env.svc.repo.kv_get(KV_DM_NOTES + env.meeting.id)
    assert reason and "transcript" in reason and env.meeting.id in reason
    assert list(ptr(env, "dm_leftover")["old"]) == ["transcript"]  # remembered, not orphaned
    assert ptr(env, "dm_move") is None
    assert (await run(env)).ok  # later deliveries neither delete it nor attach it
    assert files_in(notes_ch) == [] and dm_texts(dm) == ["📎 Full transcript"]


async def test_a_transcript_the_server_channel_refuses_stays_in_the_dm(tenv):
    """No *Attach Files* in the new channel: a notice there, the DM file kept, the reason recorded."""
    env = tenv
    as_meet(env)
    dm = await seed_dm(env, attach=MISSING)
    await seed_dm_transcript(env, dm)
    notes_ch = env.bot.add(650, "meet-notes")
    notes_ch.no_attach = True
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    request_move(env)
    res = await run(env)
    assert res.ok, res.errors
    assert dm_texts(dm) == ["📎 Full transcript"]
    assert "transcript" in env.svc.repo.kv_get(KV_DM_NOTES + env.meeting.id)


async def test_finish_keeps_any_old_message_whose_replacement_is_missing(env):
    """Every kind is checked, not only the transcript: here the new index pointer is absent."""
    from meeting_scribe.discord_ui.publisher import Pointers
    from meeting_scribe.discord_ui.task_publisher import LEFTOVER_SUFFIX, MOVE_SUFFIX

    as_meet(env)
    dm = await seed_dm(env, with_task=True)
    notes_ch = env.bot.add(650, "meet-notes")
    new = await notes_ch.send("new summary")
    new_task = await notes_ch.send("new task Budget")
    old = {s: ptr(env, s) for s in ("notes", "index", "task:a3")}
    save(env, "notes", {"v": 2, "channel": 650, "thread": None, "messages": [new.id], "url": new.jump_url})
    save(env, "task:a3", {"channel": 650, "message": new_task.id, "target": ""})
    env.svc.repo.delete_delivery("discord", f"mtg:{env.meeting.id}:index")
    move = {"from": dm.id, "channel": 650, "key": "google_meet_discord_channel", "attach": False, "old": old}
    save(env, MOVE_SUFFIX, move)
    env.state["loop"] = asyncio.get_running_loop()
    pub = env.make()._publisher(env.meeting)
    await pub._finish_move(env.meeting, move, Pointers(env.svc.repo, env.meeting.id), alive={"a3"})
    assert dm_texts(dm) == ["old index"]
    assert list(ptr(env, LEFTOVER_SUFFIX)["old"]) == ["index"] and ptr(env, MOVE_SUFFIX) is None
    assert "index" in env.svc.repo.kv_get(KV_DM_NOTES + env.meeting.id)
    res = await run(env)  # the next delivery posts the index again: now the DM copy can go
    assert res.ok, res.errors
    assert dm.ordered() == [] and ptr(env, LEFTOVER_SUFFIX) is None
    assert env.svc.repo.kv_get(KV_DM_NOTES + env.meeting.id) is None


async def test_an_old_task_that_no_longer_exists_is_cleaned_with_the_dm(env):
    """A reprocess that dropped a task: its DM message has no replacement and none is expected."""
    from meeting_scribe.discord_ui.task_publisher import MOVE_SUFFIX

    as_meet(env)
    dm = await seed_dm(env)
    gone = await dm.send("old task Gone")
    save(env, "task:zz", {"channel": dm.id, "message": gone.id, "target": ""})
    env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    request_move(env)
    res = await run(env)
    assert res.ok, res.errors
    assert dm.ordered() == [] and ptr(env, MOVE_SUFFIX) is None
    assert env.svc.repo.kv_get(KV_DM_NOTES + env.meeting.id) is None


# -- a failed transcript upload is not a finished move ---------------------------------------------
async def test_a_transient_transcript_failure_keeps_the_whole_dm_and_the_retry_finishes(tenv):
    from meeting_scribe.discord_ui.task_publisher import MOVE_SUFFIX

    env = tenv
    as_meet(env)
    dm = await seed_dm(env, attach=MISSING)
    await seed_dm_transcript(env, dm)
    notes_ch = env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    request_move(env)
    orig_send = type(notes_ch).send
    failed = {"n": 0}

    async def flaky(self, content="", **kw):
        if self is notes_ch and kw.get("file") is not None and not failed["n"]:
            failed["n"] += 1
            raise RuntimeError("503 Service Unavailable")
        return await orig_send(self, content, **kw)
    type(notes_ch).send = flaky
    try:
        res = await run(env)
    finally:
        type(notes_ch).send = orig_send
    assert not res.ok and "transcript" in res.errors[0]
    assert dm_texts(dm) == ["old summary", "old index", "📎 Full transcript"]  # nothing deleted
    assert ptr(env, MOVE_SUFFIX) is not None
    res = await run(env)  # the job's retry (the explicit flag is kept on failure)
    assert res.ok, res.errors
    assert dm.ordered() == [] and len(files_in(notes_ch)) == 1


async def test_an_exception_while_attaching_during_a_move_is_not_swallowed(tenv):
    env = tenv
    as_meet(env)
    dm = await seed_dm(env, attach=MISSING)
    await seed_dm_transcript(env, dm)
    env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    request_move(env)
    orig = env.make

    def make():
        sink = orig()
        sink._transcript_text = lambda m: (_ for _ in ()).throw(OSError("disk read error"))
        return sink
    env.make = make
    res = await run(env)
    assert not res.ok and "disk read error" in res.errors[0]
    assert dm_texts(dm) == ["old summary", "old index", "📎 Full transcript"]


# -- other pointers written by older versions ------------------------------------------------------
async def test_a_move_saved_by_the_previous_build_recovers_the_transcript_intent(tenv):
    """A ``dm_move`` recorded with ``attach: false`` (the bug) for a summary that predates the key:
    resumed, it re-derives the intent from the DM pointers it carries and re-posts the transcript."""
    from meeting_scribe.discord_ui.task_publisher import MOVE_SUFFIX

    env = tenv
    as_meet(env)
    dm = await seed_dm(env, attach=MISSING)
    await seed_dm_transcript(env, dm)
    notes_ch = env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    old = {s: ptr(env, s) for s in ("notes", "index", "transcript")}
    save(env, MOVE_SUFFIX, {"from": dm.id, "channel": 650, "key": "google_meet_discord_channel",
                            "attach": False, "old": old})
    for s in old:
        env.svc.repo.delete_delivery("discord", f"mtg:{env.meeting.id}:{s}")
    res = await run(env)
    assert res.ok, res.errors
    assert len(files_in(notes_ch)) == 1 and dm.ordered() == []


async def test_a_first_version_summary_pointer_in_a_dm_moves_whole(env):
    """0.1 format: no ``v``, no ``url``, no ``attach``; header and task rows all in ``messages``."""
    as_meet(env)
    dm = env.bot.user(42).dm
    parts = [await dm.send(t) for t in ("old summary", "old task row", "old buttons")]
    save(env, "notes", {"channel": dm.id, "thread": None, "messages": [m.id for m in parts]})
    notes_ch = env.bot.add(650, "meet-notes")
    env.cfg["google_meet_discord_channel"] = "meet-notes"
    request_move(env)
    res = await run(env)
    assert res.ok, res.errors
    assert dm.ordered() == [] and "Migración SMTP" in notes_ch.ordered()[0].content
    assert ptr(env, "notes")["v"] == 2 and ptr(env, "notes")["channel"] == 650
    assert env.svc.repo.kv_get(KV_DM_NOTES + env.meeting.id) is None


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
