"""RecordingSession lifecycle with fake Discord objects (DESIGN §4)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from meeting_scribe.capture.receiver import scribe_receiver_class
from meeting_scribe.capture.session import Busy, RecordingSession, SessionDeps
from meeting_scribe.config import settings_from_mapping
from meeting_scribe.domain.models import MeetingState

from .fakes import (FRAME, FakeAdapter, FakeBot, FakeGuild, FakeMember, FakeVoiceChannel, FakeVoiceClient,
                    FakeVoiceReceiver)


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


@dataclass
class FakeService:
    root: Path
    begun: list = field(default_factory=list)
    finished: list = field(default_factory=list)
    heard: list = field(default_factory=list)

    def begin_recording(self, meeting):
        assert meeting.state is MeetingState.RECORDING
        self.begun.append(meeting)
        return meeting

    def track_path(self, meeting, user_id):
        return self.root / meeting.id / "tracks" / f"{user_id}.ogg"

    def finish_recording(self, meeting_id, *, speakers=(), partial=False, heard=True):
        self.finished.append((meeting_id, tuple(speakers), partial))
        self.heard.append(heard)


class FakeWriter:
    instances: list["FakeWriter"] = []

    def __init__(self, path: Path, t0: float) -> None:
        self.path, self.t0, self.frames, self.closed, self.error = path, t0, [], False, None
        FakeWriter.instances.append(self)

    def write(self, frames):
        self.frames += list(frames)

    def close(self):
        self.closed = True


@pytest.fixture
def world(tmp_path):
    FakeWriter.instances = []
    guild = FakeGuild()
    voice = FakeVoiceChannel(500, "Daily Sync", guild)
    ana, luis, bot = (FakeMember(42, "Ana", channel=voice), FakeMember(43, "Luis", channel=voice),
                      FakeMember(77, "OtherBot", bot=True, channel=voice))
    voice.members = [ana, luis, bot]
    guild.members = {42: ana, 43: luis, 77: bot}
    guild.channels = [voice]
    adapter = FakeAdapter(FakeBot([guild]))
    clock = Clock()
    service = FakeService(tmp_path)
    cfg = {"autoleave_grace_seconds": 60, "limits_max_duration_minutes": 240}

    def deps(**over: Any) -> SessionDeps:
        s = settings_from_mapping({**cfg, **over})
        return SessionDeps(service=service, settings=lambda: s, receiver_cls=scribe_receiver_class(FakeVoiceReceiver),
                           writer_factory=lambda path, t0: FakeWriter(path, t0), clock=clock,
                           now=lambda: datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc), tick=0.001)
    return dict(guild=guild, voice=voice, adapter=adapter, clock=clock, service=service, deps=deps, ana=ana)


async def run_ticks(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0.002)


async def started(world, **over):
    s = RecordingSession(world["adapter"], world["voice"], world["deps"](**over), started_by="42")
    await s.start()
    return s


async def test_start_joins_registers_and_begins_recording(world):
    s = await started(world)
    vc = world["voice"].vc
    assert world["adapter"]._voice_clients[100] is vc
    assert s.meeting.channel_id == "500" and s.meeting.guild_name == "Acme" and s.meeting.started_by == "42"
    assert world["service"].begun == [s.meeting]
    assert s.receiver._running and s.receiver._vc is vc
    assert "Daily Sync" in world["voice"].sent[0]["content"]  # consent announcement
    assert world["guild"].me.nick == "[REC] Hermes"
    await s.stop("stopped")


async def test_refuses_when_guild_already_has_voice_client(world):
    other = FakeVoiceClient(world["voice"])
    world["adapter"]._voice_clients[100] = other
    with pytest.raises(Busy):
        await started(world)
    assert world["voice"].connects == 0


async def test_drain_writes_frames_per_user_and_skips_bots(world):
    s = await started(world)
    rx = s.receiver
    rx.map_ssrc(1, 42)
    rx.map_ssrc(2, 77)
    rx._buffers[1].extend(FRAME)
    rx._buffers[2].extend(FRAME)
    await run_ticks()
    assert [w.path.name for w in FakeWriter.instances] == ["42.ogg"]
    assert FakeWriter.instances[0].t0 == s.t0 and len(FakeWriter.instances[0].frames) == 1
    meeting = await s.stop("stopped")
    mid, speakers, partial = world["service"].finished[0]
    assert mid == meeting.id and partial is False
    assert {(sp.user_id, sp.name) for sp in speakers} == {("42", "Ana"), ("43", "Luis")}
    assert FakeWriter.instances[0].closed
    assert world["voice"].vc.disconnects == 1 and 100 not in world["adapter"]._voice_clients
    assert world["guild"].me.nick is None  # nickname restored


async def test_stop_is_idempotent(world):
    s = await started(world)
    await asyncio.gather(s.stop("stopped"), s.stop("stopped"))
    assert len(world["service"].finished) == 1


async def test_autoleave_after_grace_with_no_humans(world):
    s = await started(world, **{"autoleave_grace_seconds": 30})
    world["voice"].members = [m for m in world["voice"].members if m.bot]
    await run_ticks()
    assert not s.done
    world["clock"].t += 31
    await asyncio.wait_for(s.wait(), 1)
    assert s.reason == "empty" and world["service"].finished[0][2] is False


async def test_humans_returning_cancel_autoleave(world):
    s = await started(world, **{"autoleave_grace_seconds": 30})
    humans = [m for m in world["voice"].members if not m.bot]
    world["voice"].members = []
    await run_ticks()
    world["clock"].t += 20
    world["voice"].members = humans
    await run_ticks()
    world["clock"].t += 20
    await run_ticks()
    assert not s.done
    await s.stop("stopped")


async def test_max_duration_stops(world):
    s = await started(world, **{"limits_max_duration_minutes": 1})
    world["clock"].t += 61
    await asyncio.wait_for(s.wait(), 1)
    assert s.reason == "max_duration"


async def test_voice_disconnect_finalizes_partial(world):
    s = await started(world)
    world["voice"].vc.connected = False  # discord.py gave up: state disconnected, client cleaned up
    world["voice"].vc._connection.state = SimpleNamespace(name="disconnected")
    await asyncio.wait_for(s.wait(), 1)
    assert s.reason == "disconnected" and world["service"].finished[0][2] is True


async def test_external_voice_leave_finalizes_partial(world):
    s = await started(world)
    await world["adapter"].leave_voice_channel(100)  # Hermes /voice leave or adapter.disconnect()
    await asyncio.wait_for(s.wait(), 1)
    assert s.reason == "disconnected" and world["service"].finished[0][2] is True


async def test_keepalive_and_dave_refresh(world):
    s = await started(world)
    conn = world["voice"].vc._connection
    world["clock"].t += 16
    conn.dave_session = object()
    await run_ticks()
    assert conn.sent == [b"\xf8\xff\xfe"]
    assert s.receiver._dave_session is conn.dave_session
    await s.stop("stopped")


async def test_nickname_failure_is_not_fatal(world):
    async def deny(**kw):
        raise PermissionError("Missing Permissions")
    world["guild"].me.edit = deny
    s = await started(world)
    assert s.meeting is not None
    await s.stop("stopped")


async def test_finalize_sync_without_loop_marks_partial(world):
    s = await started(world)
    s.finalize_sync()
    assert world["service"].finished[0][2] is True
    assert FakeWriter.instances == [] or all(w.closed for w in FakeWriter.instances)
    await s.stop("stopped")  # no double finish
    assert len(world["service"].finished) == 1


async def test_nobody_heard_finishes_as_empty_and_says_so(world):
    """Auto-join case: someone joins and leaves before speaking; no frame ever arrives."""
    s = await started(world, **{"autoleave_grace_seconds": 30})
    world["voice"].members = [m for m in world["voice"].members if m.bot]
    await run_ticks()
    world["clock"].t += 31
    await asyncio.wait_for(s.wait(), 1)
    assert s.heard is False and world["service"].heard == [False]
    last = world["voice"].sent[-1]["content"]
    assert "No audio was captured" in last and "preparing the notes" not in last


async def test_someone_heard_finishes_normally(world):
    s = await started(world)
    s.receiver.map_ssrc(1, 42)
    s.receiver._buffers[1].extend(FRAME)
    await run_ticks()
    await s.stop("stopped")
    assert s.heard is True and world["service"].heard == [True]
    assert "preparing the notes" in world["voice"].sent[-1]["content"]


async def test_only_a_bot_heard_counts_as_nobody(world):
    s = await started(world)
    s.receiver.map_ssrc(2, 77)
    s.receiver._buffers[2].extend(FRAME)
    await run_ticks()
    await s.stop("stopped")
    assert s.heard is False and world["service"].heard == [False]
