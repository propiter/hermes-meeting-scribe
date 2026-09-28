"""CaptureManager: guild/voice resolution, per-guild sessions, loop/executor dispatch (DESIGN §13)."""
from __future__ import annotations

import asyncio
import time
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from meeting_scribe.capture.compat import CompatResult
from meeting_scribe.capture.controller import CaptureManager
from meeting_scribe.commands import Caller
from meeting_scribe.config import settings_from_mapping

from .fakes import (FakeAdapter, FakeBot, FakeGuild, FakeMember, FakeTextChannel, FakeVoiceChannel,
                    FakeVoiceClient, FakeVoiceReceiver)


@dataclass
class Svc:
    root: Path
    finished: list = field(default_factory=list)

    def begin_recording(self, m):
        return m

    def track_path(self, m, uid):
        return self.root / f"{uid}.ogg"

    def finish_recording(self, mid, *, speakers=(), partial=False, heard=True):
        self.finished.append((mid, partial))


class NullWriter:
    error = None

    def write(self, frames):
        pass

    def close(self):
        pass


@pytest.fixture
def world(tmp_path):
    guild = FakeGuild()
    text = FakeTextChannel(300, "general", guild)
    daily = FakeVoiceChannel(500, "Daily Sync", guild)
    other = FakeVoiceChannel(501, "Planning", guild)
    ana = FakeMember(42, "Ana", channel=daily)
    luis = FakeMember(43, "Luis")
    daily.members = [ana]
    guild.members = {42: ana, 43: luis}
    guild.channels = [text, daily, other]
    mod = types.ModuleType("fake_hermes_adapter")
    setattr(mod, "VoiceReceiver", FakeVoiceReceiver)
    sys.modules["fake_hermes_adapter"] = mod
    adapter_cls = type("DiscordAdapter", (FakeAdapter,), {"__module__": "fake_hermes_adapter"})
    adapter = adapter_cls(FakeBot([guild]))
    svc = Svc(tmp_path)
    s = settings_from_mapping({"consent_nickname_prefix": ""})
    mgr = CaptureManager(service=lambda: svc, settings=lambda space=None: s, ffmpeg=lambda: None,
                         writer_factory=lambda ff, path, t0, kbps: NullWriter(),
                         compat=lambda adapter: CompatResult(True, (), ("x",)), tick=0.001)
    yield dict(guild=guild, text=text, daily=daily, other=other, adapter=adapter, mgr=mgr, svc=svc)
    sys.modules.pop("fake_hermes_adapter", None)


def caller(chat="300", user="42", platform="discord"):
    return Caller(platform, chat, user)


def in_thread(fn, *args):
    return asyncio.to_thread(fn, *args)


async def test_not_connected_before_attach(world):
    assert "still connecting" in (await in_thread(world["mgr"].start, caller(), None)).lower()


async def test_start_resolves_callers_voice_channel_from_executor(world):
    mgr = world["mgr"]
    mgr.attach(world["adapter"]._client, world["adapter"])
    reply = await in_thread(mgr.start, caller(), None)
    assert "Daily Sync" in reply
    assert world["daily"].connects == 1
    assert len(mgr.live_meeting_ids()) == 1
    assert "Daily Sync" in (await in_thread(mgr.start, caller(), None))  # already recording
    assert world["daily"].connects == 1
    stop = await in_thread(mgr.stop, caller())
    assert next(iter(world["svc"].finished))[1] is False and mgr.live_meeting_ids() == set()
    assert "`" in stop


@pytest.mark.parametrize("target", ["<#501>", "501", "planning", "#Planning"])
async def test_explicit_target(world, target):
    mgr = world["mgr"]
    mgr.attach(world["adapter"]._client, world["adapter"])
    reply = await in_thread(mgr.start, caller(user="43"), target)
    assert "Planning" in reply and world["other"].connects == 1
    await in_thread(mgr.stop, caller())


async def test_unknown_target_and_caller_not_in_voice(world):
    mgr = world["mgr"]
    mgr.attach(world["adapter"]._client, world["adapter"])
    assert "nope" in await in_thread(mgr.start, caller(), "nope")
    assert "voice channel" in (await in_thread(mgr.start, caller(user="43"), None)).lower()
    assert "general" in await in_thread(mgr.start, caller(), "<#300>")  # a text channel is not a target


async def test_non_discord_and_dm_callers(world):
    mgr = world["mgr"]
    mgr.attach(world["adapter"]._client, world["adapter"])
    assert "discord" in (await in_thread(mgr.start, caller(platform="telegram"), None)).lower()
    world["adapter"]._client.guilds[0].channels.append(FakeTextChannel(999, "dm", None))
    assert "server" in (await in_thread(mgr.start, caller(chat="999"), None)).lower()


async def test_busy_guild_is_refused_with_message(world):
    mgr = world["mgr"]
    mgr.attach(world["adapter"]._client, world["adapter"])
    world["adapter"]._voice_clients[100] = FakeVoiceClient(world["daily"])
    assert "/voice leave" in await in_thread(mgr.start, caller(), None)


async def test_incompatible_adapter_disables_capture(world, caplog):
    mgr = world["mgr"]
    mgr._compat = lambda adapter: CompatResult(False, ("VoiceReceiver._on_packet changed",), ())
    mgr.attach(world["adapter"]._client, world["adapter"])
    reply = await in_thread(mgr.start, caller(), None)
    # the chat user gets a plain sentence; the internals go to the log and to doctor/status
    assert "_on_packet" not in reply and "doctor" in reply and world["daily"].connects == 0
    assert "_on_packet" in caplog.text
    ok, detail = mgr.status()
    assert ok is False and "_on_packet" in detail


async def test_missing_ffmpeg_reply_hides_the_exception(world, caplog):
    from meeting_scribe.audio.ffmpeg import FfmpegNotFound

    mgr = world["mgr"]
    mgr.attach(world["adapter"]._client, world["adapter"])

    async def no_ffmpeg(*a, **k):
        raise FfmpegNotFound("ffmpeg binary not found on PATH")
    mgr.start_in = no_ffmpeg
    reply = await in_thread(mgr.start, caller(), None)
    assert "PATH" not in reply and "ffmpeg" not in reply and "doctor" in reply
    assert "ffmpeg binary not found" in caplog.text


async def test_called_on_the_loop_thread_schedules_and_returns(world):
    mgr = world["mgr"]
    mgr.attach(world["adapter"]._client, world["adapter"])
    reply = mgr.start(caller(), None)  # same thread as the adapter loop: must not block
    assert reply
    for _ in range(50):
        await asyncio.sleep(0.002)
        if mgr.live_meeting_ids():
            break
    assert len(mgr.live_meeting_ids()) == 1
    mgr.stop(caller())
    for _ in range(50):
        await asyncio.sleep(0.002)
    assert mgr.live_meeting_ids() == set()


async def test_session_ending_by_itself_is_forgotten(world):
    mgr = world["mgr"]
    mgr.attach(world["adapter"]._client, world["adapter"])
    await in_thread(mgr.start, caller(), None)
    await world["adapter"].leave_voice_channel(100)
    for _ in range(100):
        await asyncio.sleep(0.002)
        if not mgr.live_meeting_ids():
            break
    assert mgr.live_meeting_ids() == set() and world["svc"].finished[0][1] is True


async def test_stop_without_recording(world):
    mgr = world["mgr"]
    mgr.attach(world["adapter"]._client, world["adapter"])
    assert "not recording" in (await in_thread(mgr.stop, caller())).lower()


async def test_shutdown_finalizes_live_sessions(world):
    mgr = world["mgr"]
    mgr.attach(world["adapter"]._client, world["adapter"])
    await in_thread(mgr.start, caller(), None)
    await in_thread(mgr.shutdown)
    assert world["svc"].finished[0][1] is True and mgr.live_meeting_ids() == set()


async def test_attach_exposes_loop_and_compat(world):
    mgr = world["mgr"]
    assert mgr.loop is None and mgr.compat_result is None
    mgr.attach(world["adapter"]._client, world["adapter"])
    assert mgr.loop is asyncio.get_running_loop() and mgr.compat_result.ok


async def test_meeting_stays_live_until_teardown_finishes(world):
    """Found while fixing finding 2: live_meeting_ids() dropped the meeting when teardown BEGAN,
    so an owner-side recover() could close a recording whose tracks were still being flushed."""
    mgr = world["mgr"]
    mgr.attach(world["adapter"]._client, world["adapter"])
    await in_thread(mgr.start, caller(), None)
    session = mgr.session_for(100)
    assert session is not None
    gate = asyncio.Event()
    real = session._close_writers

    def slow_close() -> None:
        deadline = time.monotonic() + 2
        while not gate.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)
        real()

    session._close_writers = slow_close
    stopping = asyncio.ensure_future(session.stop("manual"))
    try:
        await asyncio.sleep(0.02)
        assert session.done and mgr.live_meeting_ids() == {session.meeting.id}
    finally:
        gate.set()
        await stopping
    assert mgr.live_meeting_ids() == set()


async def test_stop_reply_says_no_audio_when_nobody_was_heard(world):
    mgr = world["mgr"]
    mgr.attach(world["adapter"]._client, world["adapter"])
    await in_thread(mgr.start, caller(), None)
    stop = await in_thread(mgr.stop, caller())
    assert "No audio was captured" in stop and "preparing the notes" not in stop


async def test_a_server_of_no_space_is_never_recorded(world):
    """DESIGN §23: with several spaces an unassigned server gets an explanation, not a recording."""
    mgr = world["mgr"]
    mgr._space_of = lambda guild: None
    mgr.attach(world["adapter"]._client, world["adapter"])
    reply = await in_thread(mgr.start, caller(), None)
    assert "belongs to no space" in reply
    assert world["daily"].connects == 0 and mgr.live_meeting_ids() == set()
    assert not mgr.busy(world["guild"])  # the refused start left no stale "starting" mark


async def test_the_meeting_carries_the_server_space(world):
    mgr = world["mgr"]
    mgr._space_of = lambda guild: "team"
    mgr.attach(world["adapter"]._client, world["adapter"])
    await in_thread(mgr.start, caller(), None)
    session = mgr.session_for(world["guild"].id)
    assert session is not None and session.meeting.space == "team"
    await in_thread(mgr.stop, caller())
