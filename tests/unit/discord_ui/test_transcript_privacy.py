"""Transcript attachment privacy and idempotency (review findings 4 and 11)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from .test_transcript_attachment import deliver, files_in, tenv  # noqa: F401 - fixture reuse
from .test_task_sink import env  # noqa: F401


def tptr(env):
    row = env.svc.repo.get_delivery("discord", f"mtg:{env.meeting.id}:transcript")
    return json.loads(row["external_id"]) if row else None


async def test_button_refresh_never_attaches_the_transcript(tenv):
    tenv.state["loop"] = asyncio.get_running_loop()
    tenv.cfg["delivery_discord_transcript"] = False
    await deliver(tenv)
    tenv.cfg.pop("delivery_discord_transcript")  # upgraded plugin: the setting defaults to on
    await tenv.make().refresh(tenv.meeting.id)  # what every button click does
    assert files_in(tenv.chat) == []


async def test_meeting_delivered_before_the_upgrade_is_not_attached_on_redelivery(tenv):
    tenv.cfg["delivery_discord_transcript"] = False  # stands for "delivered by a version without attachments"
    await deliver(tenv)
    assert tptr(tenv) is None
    tenv.cfg.pop("delivery_discord_transcript")
    await deliver(tenv)  # e.g. `reprocess from=deliver` after the upgrade
    assert files_in(tenv.chat) == []


async def test_first_delivery_that_crashed_after_the_summary_still_attaches(tenv, monkeypatch):
    import meeting_scribe.discord_ui.task_publisher as tp
    orig = tp.publish_transcript
    calls = {"n": 0}

    async def crash_once(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.CancelledError()  # the delivery timed out right after posting the summary
        return await orig(*a, **kw)
    monkeypatch.setattr(tp, "publish_transcript", crash_once)
    tenv.state["loop"] = asyncio.get_running_loop()
    sink = tenv.make()
    res = await asyncio.to_thread(sink.deliver, tenv.meeting, tenv.notes, tenv.svc.folder(tenv.meeting))
    assert not res.ok
    await deliver(tenv)
    assert len(files_in(tenv.chat)) == 1


async def test_title_change_alone_does_not_repost_the_transcript(tenv):
    await deliver(tenv)
    first = tptr(tenv)["messages"]
    folder = tenv.svc.layout.relative(tenv.svc.folder(tenv.meeting))
    tenv.meeting = replace(tenv.meeting, title="Another title", folder=folder)  # same files, new LLM title
    tenv.svc.repo.save_meeting(tenv.meeting)
    await deliver(tenv)
    assert tptr(tenv)["messages"] == first and len(files_in(tenv.chat)) == 1
    assert files_in(tenv.chat)[0][1]["name"].endswith("daily-sync.md")  # the original upload stayed


async def test_pointer_save_failure_after_send_does_not_duplicate(tenv, monkeypatch):
    from meeting_scribe.discord_ui import publisher
    orig = publisher.Pointers.save
    state = {"fail": True}

    async def save(self, suffix, ptr, url=""):
        if suffix == "transcript" and ptr.get("messages") and state["fail"]:
            state["fail"] = False
            raise RuntimeError("database is locked")
        return await orig(self, suffix, ptr, url)
    monkeypatch.setattr(publisher.Pointers, "save", save)
    await deliver(tenv)
    await deliver(tenv)
    assert len(files_in(tenv.chat)) == 1
    ptr = tptr(tenv)
    assert ptr["done"] and len(ptr["messages"]) == 1 and not ptr.get("sending")


async def test_pending_part_that_never_reached_discord_is_sent_on_retry(tenv, monkeypatch):
    from . import fakes
    patched = fakes.FakeChannel.send
    calls = {"n": 0}

    async def lost(self, content="", *, view=None, file=None, **kw):
        if file is not None and calls["n"] == 0:
            calls["n"] += 1
            raise asyncio.TimeoutError()  # unknown outcome: nothing was posted
        return await patched(self, content, view=view, file=file, **kw)
    monkeypatch.setattr(fakes.FakeChannel, "send", lost)
    await deliver(tenv)
    assert files_in(tenv.chat) == [] and tptr(tenv).get("sending")
    await deliver(tenv)
    assert len(files_in(tenv.chat)) == 1
