"""Full-transcript attachment in Discord (DESIGN §17.3): once, split when large, soft on permissions."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from meeting_scribe.discord_ui.transcript_file import base_name, split_parts
from meeting_scribe.domain.models import SOURCE_GOOGLE_MEET, ActionItem, Speaker, Utterance
from meeting_scribe.storage.artifacts import write_transcript

from .test_task_sink import Views, env  # noqa: F401 - fixture reuse


class FileViews(Views):
    def file(self, name, data):
        return {"name": name, "data": data}


class Forbidden(Exception):
    def __init__(self):
        super().__init__("403 Forbidden (error code: 50013): Missing Permissions")
        self.status, self.code = 403, 50013


def files_in(channel):
    return [(m.content, m.file) for m in channel.ordered() if getattr(m, "file", None)]


@pytest.fixture
def tenv(env, monkeypatch):  # noqa: F811
    """The task-sink env with attachment support in the fake channel + a transcript on disk."""
    from . import fakes

    original = fakes.FakeChannel.send

    async def send(self, content="", *, view=None, file=None, **kw):
        if file is not None and getattr(self, "no_attach", False):
            raise Forbidden()
        msg = await original(self, content, view=view, **kw)
        msg.file = file
        return msg
    monkeypatch.setattr(fakes.FakeChannel, "send", send)
    utts = [Utterance(0.0, 2.0, "10", "Ana", "Hola a todos."), Utterance(65.0, 70.0, "11", "Luis", "Listo.")]
    write_transcript(env.svc.folder(env.meeting), utts)

    import meeting_scribe.discord_ui.sink as sink_mod
    orig_make = env.make

    def make():
        sink = orig_make()
        sink._views = FileViews()
        return sink
    env.make = make
    env.sink_mod = sink_mod
    return env


async def deliver(env, sink=None):
    env.state["loop"] = asyncio.get_running_loop()
    sink = sink or env.make()
    res = await asyncio.to_thread(sink.deliver, env.meeting, env.notes, env.svc.folder(env.meeting))
    assert res.ok, res.errors
    return sink


async def test_transcript_attached_once_after_the_summary(tenv):
    await deliver(tenv)
    [(label, f)] = files_in(tenv.chat)
    assert label == "📎 Full transcript"
    assert f["name"] == "transcript-2026-09-26-daily-sync.md"
    text = f["data"].decode()
    assert "**[00:00] Ana:** Hola a todos." in text and "**[01:05] Luis:** Listo." in text
    msgs = tenv.chat.ordered()
    assert "Usar SES" in msgs[0].content and msgs[1].file is f  # right after the summary
    await deliver(tenv)
    await deliver(tenv, tenv.make())
    assert len(files_in(tenv.chat)) == 1  # retries / refreshes never re-attach


async def test_setting_off_attaches_nothing(tenv):
    tenv.cfg["delivery_discord_transcript"] = False
    await deliver(tenv)
    assert files_in(tenv.chat) == []


async def test_changed_transcript_replaces_the_old_attachment(tenv):
    await deliver(tenv)
    write_transcript(tenv.svc.folder(tenv.meeting), [Utterance(0.0, 1.0, "10", "Ana", "Otra cosa.")])
    await deliver(tenv)
    [(_, f)] = files_in(tenv.chat)
    assert "Otra cosa." in f["data"].decode()


async def test_missing_attach_permission_posts_a_notice_once_and_delivery_succeeds(tenv):
    tenv.chat.no_attach = True
    await deliver(tenv)
    notices = [m for m in tenv.chat.ordered() if "Attach Files" in (m.content or "")]
    assert len(notices) == 1 and files_in(tenv.chat) == []
    await deliver(tenv)
    assert len([m for m in tenv.chat.ordered() if "Attach Files" in (m.content or "")]) == 1
    row = tenv.svc.repo.get_delivery("discord", f"mtg:{tenv.meeting.id}:transcript")
    assert json.loads(row["external_id"])["skipped"] == "no_permission"


async def test_transient_failure_retries_without_duplicates(tenv, monkeypatch):
    from . import fakes
    calls = {"n": 0}
    patched = fakes.FakeChannel.send

    async def flaky(self, content="", *, view=None, file=None, **kw):
        if file is not None and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("503 Service Unavailable")
        return await patched(self, content, view=view, file=file, **kw)
    monkeypatch.setattr(fakes.FakeChannel, "send", flaky)
    await deliver(tenv)  # the attachment failed softly: delivery still ok
    assert files_in(tenv.chat) == []
    await deliver(tenv)
    assert len(files_in(tenv.chat)) == 1


def test_large_transcript_is_split_on_line_boundaries(meeting):
    lines = [f"**[00:{i % 60:02d}] Ana:** " + "palabra " * 20 + "\n" for i in range(200)]
    text = "".join(lines)
    parts = split_parts(meeting, text, max_bytes=4000)
    assert len(parts) > 1 and all(len(p.data) <= 4000 for p in parts)
    assert b"".join(p.data for p in parts).decode() == text
    assert all(p.data.decode().endswith("\n") for p in parts)
    n = len(parts)
    assert [p.name for p in parts] == [f"{base_name(meeting)}-part{i}of{n}.md" for i in range(1, n + 1)]
    assert split_parts(meeting, "ñ" * 3000, max_bytes=1001)[0].data.decode()  # never cuts UTF-8


async def test_large_transcript_posts_numbered_parts(tenv, monkeypatch):
    import meeting_scribe.discord_ui.transcript_file as tf
    monkeypatch.setattr(tf, "MAX_BYTES", 60)
    orig = tf.publish_transcript

    async def small(*a, **kw):
        kw["max_bytes"] = 60
        return await orig(*a, **kw)
    import meeting_scribe.discord_ui.task_publisher as tp
    monkeypatch.setattr(tp, "publish_transcript", small)
    await deliver(tenv)
    got = files_in(tenv.chat)
    assert len(got) >= 2 and got[0][0].startswith("📎 Full transcript — part 1/")
    await deliver(tenv)
    assert len(files_in(tenv.chat)) == len(got)


async def test_meet_meeting_uses_its_channel_and_never_mentions_or_dms_imported_speakers(tenv):
    meet = replace(tenv.meeting, guild_id="", channel_id="gmeet:space1", text_channel_id=None,
                   source=SOURCE_GOOGLE_MEET, external_id="conferenceRecords/r1",
                   speakers=(Speaker("gmeet:1", "Ana Example"),))
    tenv.svc.repo.save_meeting(meet)
    notes = replace(tenv.notes, action_items=(ActionItem(id="m1", title="Send deck", owner_speaker_id="gmeet:1",
                                                         owner_name="Ana Example"),))
    from meeting_scribe.storage.artifacts import write_notes
    write_notes(tenv.svc.folder(meet), meet, notes, "en")
    tenv.svc.repo.sync_action_items(meet.id, notes.action_items)
    meet_chat = tenv.bot.add(777, "meet-notes", threads_ok=False)
    tenv.cfg["google_meet_discord_channel"] = "777"
    tenv.meeting, tenv.notes = meet, notes
    await deliver(tenv)
    contents = "\n".join(m.content or "" for m in meet_chat.ordered())
    assert "Send deck" in contents and "Ana Example" in contents
    assert "<@gmeet" not in contents
    assert files_in(meet_chat)
    assert tenv.bot.users[11].dm.ordered() == [] and tenv.svc.repo.list_deliveries(
        meet.id, sink="discord", prefix=f"mtg:{meet.id}:dm:") == []


async def test_meet_meeting_without_any_channel_waits_for_one(tenv):
    """DESIGN §19: no channel resolvable -> the delivery waits (deferred, no attempt), it is not skipped."""
    meet = replace(tenv.meeting, guild_id="", channel_id="gmeet:space1", text_channel_id=None,
                   source=SOURCE_GOOGLE_MEET, external_id="conferenceRecords/r2")
    tenv.cfg["delivery_auto_channel_names"] = []
    tenv.state["loop"] = asyncio.get_running_loop()
    res = await asyncio.to_thread(tenv.make().deliver, meet, tenv.notes, tenv.svc.folder(meet))
    assert not res.ok and res.deferred and res.waiting and "google_meet_discord_channel" in res.errors[0]
