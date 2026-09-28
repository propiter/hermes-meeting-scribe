"""Regression tests for the capture review findings W1, W2, W4-W7, W9 and the DAVE/TTS/clock notes."""
from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from meeting_scribe.audio.ffmpeg import FfmpegNotFound
from meeting_scribe.capture import receiver as receiver_mod
from meeting_scribe.capture.autojoin import AutoJoiner
from meeting_scribe.capture.controller import CaptureManager, default_writer
from meeting_scribe.capture.receiver import scribe_receiver_class
from meeting_scribe.capture.session import RecordingSession, SessionDeps
from meeting_scribe.config import settings_from_mapping

from .fakes import FRAME, FakeAdapter, FakeBot, FakeGuild, FakeMember, FakeVoiceChannel, FakeVoiceReceiver

OK = SimpleNamespace(ok=True, summary=lambda: "ok")


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


class Svc:
    def __init__(self, root: Path, fail_finish: bool = False) -> None:
        self.root, self.fail_finish = root, fail_finish
        self.finished: list[tuple[str, bool]] = []
        self.begun: list[Any] = []

    def begin_recording(self, m: Any) -> Any:
        self.begun.append(m)
        return m

    def track_path(self, m: Any, uid: str) -> Path:
        return self.root / f"{uid}.ogg"

    def finish_recording(self, mid: str, *, speakers: Any = (), partial: bool = False, heard: bool = True) -> None:
        if self.fail_finish:
            raise RuntimeError("sqlite locked")
        self.finished.append((mid, partial))


class W:
    def __init__(self, *a: Any) -> None:
        self.error: Optional[str] = None
        self.frames: list = []

    def write(self, f: Any) -> None:
        self.frames.extend(f)

    def close(self) -> None:
        pass


@pytest.fixture
def world(tmp_path: Path) -> SimpleNamespace:
    guild = FakeGuild()
    voice = FakeVoiceChannel(500, "Daily", guild)
    a, b = FakeMember(42, "Ana", channel=voice), FakeMember(43, "Luis", channel=voice)
    voice.members = [a, b]
    guild.members = {42: a, 43: b}
    guild.channels = [voice]
    adapter = FakeAdapter(FakeBot([guild]))
    cfg: dict = {"autoleave_grace_seconds": 60, "autojoin_grace_seconds": 0, "autojoin_min_humans": 2}
    return SimpleNamespace(guild=guild, voice=voice, adapter=adapter, cfg=cfg, clock=Clock(), tmp=tmp_path,
                           settings=lambda space=None: settings_from_mapping(cfg), members=(a, b))


def session(world: SimpleNamespace, svc: Optional[Svc] = None, factory: Any = None) -> RecordingSession:
    deps = SessionDeps(service=svc or Svc(world.tmp), settings=world.settings,
                       receiver_cls=scribe_receiver_class(FakeVoiceReceiver),
                       writer_factory=factory or (lambda p, t0: W()), clock=world.clock, tick=0.001)
    return RecordingSession(world.adapter, world.voice, deps)


def manager(world: SimpleNamespace, svc: Optional[Svc] = None, **kw: Any) -> CaptureManager:
    svc = svc or Svc(world.tmp)
    kw.setdefault("writer_factory", lambda ff, p, t0, k: W())
    kw.setdefault("ffmpeg", lambda: object())
    mgr = CaptureManager(service=lambda: svc, settings=world.settings, tick=0.001, clock=world.clock,
                         compat=lambda a: OK, **kw)
    mgr._adapter_ref = lambda: world.adapter  # type: ignore[assignment]
    mgr._loop = asyncio.get_running_loop()
    mgr._receiver_cls = scribe_receiver_class(FakeVoiceReceiver)
    mgr._compat_result = OK  # type: ignore[assignment]
    return mgr


# -- W1 -----------------------------------------------------------------------------------------
async def test_transient_reconnect_does_not_end_the_meeting(world):
    s = session(world)
    await s.start()
    s.vc.connected = False  # discord.py resuming (4015) / voice server update
    s.vc._connection.state = SimpleNamespace(name="got_both_voice_updates")
    await asyncio.sleep(0.05)
    assert not s.done
    s.vc.connected = True
    await asyncio.sleep(0.02)
    assert not s.done
    await s.stop()


async def test_reconnect_that_never_recovers_ends_after_grace_and_force_disconnects(world):
    s = session(world)
    await s.start()
    vc = s.vc
    vc.timeout = 30.0
    vc.connected = False
    await asyncio.sleep(0.02)
    assert not s.done
    world.clock.t += 31
    await asyncio.wait_for(s.wait(), 1)
    assert s.reason == "disconnected"
    assert vc.disconnects == 1 and vc.forced == [True]  # never left sitting in the channel


async def test_busy_includes_guild_voice_client(world):
    mgr = manager(world)
    world.guild.voice_client = object()  # discord.py still holds a client (mid-reconnect)
    assert mgr.busy(world.guild)


# -- W2 -----------------------------------------------------------------------------------------
async def test_autojoin_does_not_rejoin_after_manual_stop(world):
    mgr = manager(world)
    aj = AutoJoiner(mgr, world.settings, poll=0.001)
    mgr.on_session_end.append(aj.note_session_end)
    await mgr.start_in(world.voice)
    await mgr.session_for(world.guild.id).stop("stopped")
    await asyncio.sleep(0.01)
    me = world.guild.me
    await aj.on_voice_state_update(me, SimpleNamespace(channel=world.voice), SimpleNamespace(channel=None))
    extra = FakeMember(44, "Eva", channel=world.voice)
    world.voice.members.append(extra)
    await aj.on_voice_state_update(extra, SimpleNamespace(channel=None), SimpleNamespace(channel=world.voice))
    await asyncio.sleep(0.05)
    assert world.voice.connects == 1
    # everyone leaves: the cooldown clears and the next gathering records again
    world.voice.members.clear()
    for m in (*world.members, extra):
        await aj.on_voice_state_update(m, SimpleNamespace(channel=world.voice), SimpleNamespace(channel=None))
    world.voice.members.extend(world.members)
    for m in world.members:
        await aj.on_voice_state_update(m, SimpleNamespace(channel=None), SimpleNamespace(channel=world.voice))
    await asyncio.sleep(0.05)
    assert world.voice.connects == 2
    await mgr.session_for(world.guild.id).stop()


async def test_autojoin_close_from_another_thread_cancels_on_loop(world):
    world.cfg["autojoin_grace_seconds"] = 3600
    mgr = manager(world)
    aj = AutoJoiner(mgr, world.settings, poll=0.001)
    m = world.members[0]
    await aj.on_voice_state_update(m, SimpleNamespace(channel=None), SimpleNamespace(channel=world.voice))
    task = aj._watchers[world.voice.id]
    await asyncio.to_thread(aj.close)
    await asyncio.sleep(0.01)
    assert task.cancelled()


# -- W4 -----------------------------------------------------------------------------------------
async def test_missing_ffmpeg_fails_before_connecting(world):
    mgr = manager(world, ffmpeg=lambda: None, writer_factory=default_writer)
    with pytest.raises(FfmpegNotFound):
        await mgr.start_in(world.voice)
    assert world.voice.connects == 0


async def test_one_writer_failing_does_not_end_the_meeting(world):
    def factory(path: Path, t0: float) -> W:
        if path.stem == "42":
            raise OSError("Popen failed")
        return W()
    s = session(world, factory=factory)
    await s.start()
    for ssrc, uid in ((1, 42), (2, 43)):
        s.receiver.map_ssrc(ssrc, uid)
        s.receiver._buffers[ssrc].extend(FRAME)
    await asyncio.sleep(0.02)
    assert not s.done
    assert 43 in s._writers and 42 not in s._writers
    await s.stop()


# -- W5 -----------------------------------------------------------------------------------------
async def test_finish_failure_still_sets_finished(world):
    s = session(world, svc=Svc(world.tmp, fail_finish=True))
    await s.start()
    await s.stop("stopped")
    await asyncio.wait_for(s.wait(), 0.5)
    assert s.done


async def test_cancelled_stop_still_completes_finalize(world):
    svc = Svc(world.tmp)
    s = session(world, svc=svc)
    await s.start()
    gate = asyncio.Event()

    async def slow_send(content: str = "", **kw: Any) -> None:
        await gate.wait()
    world.voice.send = slow_send
    stopper = asyncio.ensure_future(s.stop())
    await asyncio.sleep(0.01)
    stopper.cancel()  # the controller's STOP_TIMEOUT
    await asyncio.gather(stopper, return_exceptions=True)
    gate.set()
    await asyncio.wait_for(s.wait(), 0.5)
    assert svc.finished and s.vc.disconnects == 1


# -- W6 -----------------------------------------------------------------------------------------
async def test_start_cancelled_after_connect_cleans_up(world, monkeypatch):
    monkeypatch.setattr("meeting_scribe.capture.consent.Consent.ANNOUNCE_TIMEOUT", 0.05)
    async def stuck_send(content: str = "", **kw: Any) -> None:
        await asyncio.sleep(3600)  # discord.py sleeping on a 429
    world.voice.send = stuck_send
    svc = Svc(world.tmp)
    mgr = manager(world, svc=svc)
    task = asyncio.ensure_future(mgr.start_in(world.voice))
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0.15)
    vc = world.voice.vc
    assert vc.disconnects == 1 and world.guild.id not in world.adapter._voice_clients
    assert not vc._connection.listeners  # receiver stopped: nothing buffers forever
    assert svc.finished and svc.finished[-1][1] is True
    assert mgr.session_for(world.guild.id) is None and not mgr.busy(world.guild)


# -- W7 -----------------------------------------------------------------------------------------
async def test_session_is_visible_while_starting(world):
    gate = asyncio.Event()

    async def slow_send(content: str = "", **kw: Any) -> None:
        await gate.wait()
    world.voice.send = slow_send
    mgr = manager(world)
    task = asyncio.ensure_future(mgr.start_in(world.voice))
    await asyncio.sleep(0.01)
    live = mgr.session_for(world.guild.id)
    assert live is not None and live.meeting is not None
    assert mgr.live_meeting_ids() == {live.meeting.id}
    gate.set()
    await task
    await live.stop()


async def test_failed_start_unregisters_the_session(world):
    class Boom(Svc):
        def begin_recording(self, m: Any) -> Any:
            raise RuntimeError("db down")
    mgr = manager(world, svc=Boom(world.tmp))
    with pytest.raises(RuntimeError):
        await mgr.start_in(world.voice)
    await asyncio.sleep(0.01)
    assert mgr.session_for(world.guild.id) is None and not mgr._sessions
    assert world.voice.vc.disconnects == 1


# -- W9 -----------------------------------------------------------------------------------------
async def test_leftover_rec_prefix_is_restored(world):
    world.guild.me.nick = "[REC] Hermes"
    world.guild.me.name = "Hermes"
    s = session(world)
    await s.start()
    await s.stop()
    assert world.guild.me.nick is None


async def test_leftover_prefix_with_custom_nick_restores_custom_nick(world):
    world.guild.me.nick = "[REC] Scribe"
    world.guild.me.name = "Hermes"
    s = session(world)
    await s.start()
    await s.stop()
    assert world.guild.me.nick == "Scribe"


# -- lower-confidence notes ---------------------------------------------------------------------
async def test_dave_session_is_refreshed_every_tick(world):
    s = session(world)
    await s.start()
    s.vc._connection.dave_session = marker = object()
    await asyncio.sleep(0.02)  # one tick, far below the old 5 s refresh
    assert s.receiver._dave_session is marker
    await s.stop()


async def test_unmapped_frames_are_dropped_while_dave_is_active(world):
    s = session(world)
    await s.start()
    s.receiver._dave_session = object()
    s.vc._connection.dave_session = s.receiver._dave_session
    s.receiver._buffers[7].extend(FRAME)  # E2EE payload decoded as noise before SPEAKING
    s.receiver.map_ssrc(7, 42)
    s.receiver._buffers[7].extend(FRAME)
    frames = s.receiver.drain()[42]
    assert len(frames) == 1
    await s.stop()


async def test_recording_voice_client_never_plays_audio(world):
    s = session(world)
    await s.start()
    done: list = []
    s.vc.play(SimpleNamespace(cleanup=lambda: done.append("cleanup")), after=lambda e: done.append(e))
    assert done == ["cleanup", None]
    await s.stop()


def test_timeline_clock_defaults_are_monotonic():
    assert SessionDeps.__dataclass_fields__["clock"].default is time.monotonic
    assert inspect.signature(CaptureManager).parameters["clock"].default is time.monotonic
    assert receiver_mod.TimedBuffer().clock_fn is time.monotonic

