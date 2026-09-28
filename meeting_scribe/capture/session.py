"""One live recording in one guild (DESIGN §4, §15).

Lifecycle: ``start()`` joins with our OWN ``channel.connect()`` under the adapter's per-guild voice
lock and registers the client in ``adapter._voice_clients`` (so Hermes' ``/voice status|leave``
and shutdown see it) — but never in ``_voice_receivers``/``_voice_listen_tasks``/
``_voice_text_channels``/``_voice_timeout_tasks``, so Hermes starts no agent turns and no
inactivity timer. Playback is muted on our voice client (see :func:`mute_playback`), so a Hermes
voice-mode reply in another text chat of the guild can never be spoken into the meeting.

A drain loop (0.5 s) moves timed frames from the receiver into per-speaker writers, sends the UDP
keepalive, re-reads the DAVE session every tick, tells the receiver who is in the channel (the
candidates for a voice SPEAKING never announced, DESIGN §4.1), and decides when to stop: explicit stop, no
humans for ``autoleave_grace_seconds``, ``limits_max_duration_minutes``, or the voice client being
lost (``/voice leave``/adapter disconnect immediately; a discord.py reconnect only after it has
not recovered for ``vc.timeout`` seconds → partial).

Missing audio (DESIGN §4.1): each tick adds the elapsed time to every unmuted person in the channel.
At the end, a person unmuted there for more than ``MISSING_AUDIO_SECONDS`` whose voice never reached
a track is recorded on the meeting (``missing_audio``) and logged as a WARNING with the SSRCs that
could not be identified, so the notes, Desktop and ``status`` say whose audio is missing.

Finalisation always completes (review W5): it runs as its own task shielded from the caller's
cancellation and sets ``_finished`` in a ``finally``, so ``wait()`` and the controller's reaper
can never hang.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol

from ..config import Settings
from ..domain.ids import short_id
from ..domain.models import Meeting, MeetingState, Speaker
from ..discord_ui.render import safe_name
from ..i18n import t
from .consent import Consent

log = logging.getLogger(__name__)
KEEPALIVE = b"\xf8\xff\xfe"
KEEPALIVE_SECONDS = 15.0
RECONNECT_GRACE_SECONDS = 30.0  # discord.py's own VoiceClient.timeout default
PARTIAL_REASONS = frozenset({"disconnected", "shutdown", "error"})
MISSING_AUDIO_SECONDS = 60.0


class Busy(RuntimeError):
    """The guild already has a voice client (e.g. ``/voice join`` or another recording)."""


class Writer(Protocol):
    error: Optional[str]

    def write(self, frames: Any) -> None: ...

    def close(self) -> None: ...


@dataclass
class SessionDeps:
    service: Any  # MeetingService (begin_recording / track_path / finish_recording)
    settings: Callable[[], Settings]
    receiver_cls: type
    writer_factory: Callable[[Any, float], Writer]
    clock: Callable[[], float] = time.monotonic  # timeline clock; display times come from ``now``
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    tick: float = 0.5
    space: str = ""  # the space owning the guild (DESIGN §23); the meeting belongs to it from the start


def mute_playback(vc: Any) -> None:
    """Make ``vc.play`` a no-op that completes immediately (the recording client never speaks)."""
    def play(source: Any, *, after: Optional[Callable[[Optional[Exception]], Any]] = None, **kw: Any) -> None:
        log.info("meeting-scribe: refused audio playback into a recorded voice channel")
        cleanup = getattr(source, "cleanup", None)
        if callable(cleanup):
            cleanup()
        if after is not None:
            after(None)
    try:
        vc.play = play
    except AttributeError as exc:  # a slotted client: nothing to guard, Hermes still has no text mapping
        log.debug("meeting-scribe: cannot mute playback: %s", exc)


def _muted(member: Any) -> bool:
    voice = getattr(member, "voice", None)
    return bool(getattr(voice, "self_mute", False) or getattr(voice, "mute", False))


def _conn_state_name(vc: Any) -> Optional[str]:
    return getattr(getattr(getattr(vc, "_connection", None), "state", None), "name", None)


class RecordingSession:
    def __init__(self, adapter: Any, channel: Any, deps: SessionDeps, *, started_by: Optional[str] = None,
                 text_channel: Any = None) -> None:
        self.adapter = adapter
        self.channel = channel
        self.guild = channel.guild
        self.deps = deps
        self.started_by = started_by
        self.text_channel = text_channel
        self.meeting: Optional[Meeting] = None
        self.receiver: Any = None
        self.vc: Any = None
        self.t0 = 0.0
        self.reason: Optional[str] = None
        self.done = False
        self._writers: dict[Any, Writer] = {}  # user id, or an "unidentified-N" label
        self._writer_errors: dict[Any, str] = {}
        self._unmuted_seconds: dict[str, float] = {}
        self._last_tick = 0.0
        self.missing_audio: tuple[str, ...] = ()
        self._speakers: dict[str, Speaker] = {}
        self._task: Optional[asyncio.Task] = None
        self._teardown: Optional[asyncio.Task] = None
        self._final_lock = asyncio.Lock()
        self._finished = asyncio.Event()
        self._ready = asyncio.Event()  # the locked connect phase is over (success or failure)
        self._unhealthy_since: Optional[float] = None
        self.consent = Consent(self.guild, channel, text_channel, deps.settings)

    @property
    def guild_id(self) -> int:
        return int(self.guild.id)

    @property
    def lang(self) -> str:
        return self.deps.settings().ui_language

    # -- start ----------------------------------------------------------------------------------
    async def start(self) -> Meeting:
        try:
            await self._connect()
        except BaseException:
            self.done = True
            self._finished.set()
            raise
        finally:
            self._ready.set()
        assert self.meeting is not None
        self._task = asyncio.ensure_future(self._run())  # drains even while the consent calls below wait
        try:
            await self.consent.announce(t("capture.announce", self.lang, channel=self.channel.name))
            if not self.done:
                await self.consent.set_nickname()
        except BaseException:  # e.g. the dispatch timeout cancelled us: never leave the bot behind
            await asyncio.shield(self._finalize("error"))
            raise
        if self.done:  # stopped while announcing: undo a prefix applied meanwhile
            await self.consent.restore_nickname()
        return self.meeting

    async def _connect(self) -> None:
        lock = self.adapter._voice_locks.setdefault(self.guild_id, asyncio.Lock())
        async with lock:
            existing = self.adapter._voice_clients.get(self.guild_id) or getattr(self.guild, "voice_client", None)
            if existing is not None and existing.is_connected():
                raise Busy(t("capture.busy", self.lang))
            self.vc = await self.channel.connect()
            mute_playback(self.vc)
            self.adapter._voice_clients[self.guild_id] = self.vc
            try:
                self.receiver = self.deps.receiver_cls(self.vc, clock=self.deps.clock)
                self.receiver.start()
                self.t0 = self._last_tick = self.deps.clock()
                self._note_members()
                self._update_presence()
                # Known before the row exists: live_meeting_ids() must cover it for recover() (W7).
                self.meeting = self._new_meeting()
                self.meeting = await asyncio.to_thread(self.deps.service.begin_recording, self.meeting)
            except BaseException:
                self._stop_receiver()
                await self._release_voice()
                raise

    def _new_meeting(self) -> Meeting:
        category = getattr(getattr(self.channel, "category", None), "name", "") or ""
        category_id = getattr(self.channel, "category_id", None)
        text_id = getattr(self.text_channel, "id", None)
        return Meeting(id=short_id(), guild_id=str(self.guild.id), channel_id=str(self.channel.id),
                       channel_name=self.channel.name, started_at=self.deps.now(), state=MeetingState.RECORDING,
                       title=self.channel.name, speakers=tuple(self._speakers.values()),
                       guild_name=getattr(self.guild, "name", "") or "", category_name=category,
                       category_id=str(category_id) if category_id is not None else None,
                       text_channel_id=str(text_id) if text_id is not None else None, started_by=self.started_by,
                       space=self.deps.space)

    # -- speakers -------------------------------------------------------------------------------
    def humans(self) -> list[Any]:
        return [m for m in getattr(self.channel, "members", []) if not getattr(m, "bot", False)]

    def _note_members(self) -> None:
        for m in self.humans():
            self._speakers.setdefault(str(m.id), Speaker(str(m.id), m.display_name))

    def _update_presence(self) -> None:
        """Tell the receiver who may own an unannounced SSRC: everyone in the channel but us (another
        bot can talk too), and who is muted; count each unmuted person's time for ``missing_audio``."""
        now = self.deps.clock()
        elapsed, self._last_tick = max(0.0, now - self._last_tick), now
        me = getattr(getattr(self.vc, "user", None), "id", None)
        members = [m for m in getattr(self.channel, "members", []) if m.id != me]
        muted = [m.id for m in members if _muted(m)]
        self.receiver.update_presence([m.id for m in members], muted)
        for m in members:
            if not getattr(m, "bot", False) and m.id not in muted:
                key = str(m.id)
                self._unmuted_seconds[key] = self._unmuted_seconds.get(key, 0.0) + elapsed

    def _speaker_for(self, user_id: int) -> Optional[Speaker]:
        """Speaker for audio from ``user_id``; None for bots (their audio is not recorded)."""
        key = str(user_id)
        if key in self._speakers:
            return self._speakers[key]
        member = self.guild.get_member(int(user_id))
        if member is not None and getattr(member, "bot", False):
            return None
        sp = Speaker(key, getattr(member, "display_name", None) or key)
        self._speakers[key] = sp
        return sp

    # -- loop -----------------------------------------------------------------------------------
    def _voice_lost(self, now: float) -> bool:
        vc = self.vc
        if vc is None or self.adapter._voice_clients.get(self.guild_id) is not vc:
            return True  # /voice leave, adapter disconnect, or someone replaced the client
        if vc.is_connected():
            self._unhealthy_since = None
            return False
        if _conn_state_name(vc) == "disconnected" and getattr(self.guild, "voice_client", None) is not vc:
            return True  # discord.py tore the client down for good (cleanup ran)
        if self._unhealthy_since is None:
            self._unhealthy_since = now
        grace = float(getattr(vc, "timeout", None) or RECONNECT_GRACE_SECONDS)
        return now - self._unhealthy_since >= grace  # a resume/move that never recovered

    def _unidentified_speaker(self, label: str) -> Speaker:
        """The speaker of an "unidentified participant" track, named after the person a SPEAKING
        that arrived later designated, if any (SPEAKING is authoritative)."""
        if label not in self._speakers:
            number = label.rsplit("-", 1)[-1]
            name = t("capture.unidentified", self.lang) + ("" if number == "1" else f" {number}")
            self._speakers[label] = Speaker(label, name)
        return self._speakers[label]

    def _writer_for(self, user_id: Any) -> Optional[Writer]:
        writer = self._writers.get(user_id)
        if writer is not None or user_id in self._writer_errors:
            return writer
        assert self.meeting is not None
        try:
            path = self.deps.service.track_path(self.meeting, str(user_id))
            writer = self._writers[user_id] = self.deps.writer_factory(path, self.t0)
        except Exception as exc:  # one speaker's encoder failing must not end everyone's meeting
            self._writer_errors[user_id] = f"{type(exc).__name__}: {exc}"
            log.error("meeting-scribe: track writer for %s could not start: %s", user_id, exc)
            return None
        return writer

    def _drain(self) -> None:
        if self.receiver is None or self.meeting is None:
            return
        for user_id, frames in self.receiver.drain().items():
            if self._speaker_for(user_id) is None:
                continue
            writer = self._writer_for(user_id)
            if writer is not None:
                writer.write(frames)
        for label, frames in self.receiver.drain_unidentified().items():
            self._unidentified_speaker(label)
            writer = self._writer_for(label)
            if writer is not None:
                writer.write(frames)

    async def _run(self) -> None:
        clock = self.deps.clock
        last_keepalive = self.t0
        empty_since: Optional[float] = None
        reason = "error"
        try:
            while not self.done:
                await asyncio.sleep(self.deps.tick)
                if self.done:
                    return
                now = clock()
                if self._voice_lost(now):
                    reason = "disconnected"
                    break
                self.receiver.refresh_connection()  # DAVE key/session changes apply on the next tick
                self._update_presence()
                self._drain()
                if now - last_keepalive >= KEEPALIVE_SECONDS:
                    last_keepalive = now
                    try:
                        self.vc._connection.send_packet(KEEPALIVE)
                    except Exception as exc:  # UDP socket mid-reconnect; the next tick retries
                        log.debug("meeting-scribe keepalive failed: %s", exc)
                s = self.deps.settings()
                if now - self.t0 >= s.limits_max_duration_minutes * 60:
                    reason = "max_duration"
                    break
                if self.humans():
                    empty_since = None
                    self._note_members()
                else:
                    empty_since = empty_since if empty_since is not None else now
                    if now - empty_since >= s.autoleave_grace_seconds:
                        reason = "empty"
                        break
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("meeting-scribe drain loop crashed (guild %s)", self.guild_id)
        await self._finalize(reason)

    # -- stop -----------------------------------------------------------------------------------
    async def stop(self, reason: str = "stopped") -> Optional[Meeting]:
        await self._ready.wait()  # a stop during connect waits for the voice lock phase to settle
        await self._finalize(reason)
        return self.meeting

    async def wait(self) -> None:
        await self._finished.wait()

    @property
    def finished(self) -> bool:
        """Teardown complete: tracks closed and the row handed to the pipeline (or failed)."""
        return self._finished.is_set()

    async def _finalize(self, reason: str) -> None:
        async with self._final_lock:
            task, current = self._task, asyncio.current_task()
            if task is not None and task is not current and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if self._teardown is None:
                if self.done:  # failed during connect: already torn down
                    self._finished.set()
                    return
                self.done = True
                self.reason = reason
                self._teardown = asyncio.ensure_future(self._tear_down(reason))
        await asyncio.shield(self._teardown)

    async def _tear_down(self, reason: str) -> None:
        try:
            try:
                self._drain()
            except Exception:
                log.exception("meeting-scribe final drain failed")
            self._stop_receiver()
            self._check_missing_audio()
            await asyncio.to_thread(self._close_writers)
            await self.consent.restore_nickname()
            await self._release_voice()
            try:
                await asyncio.to_thread(self._finish, reason in PARTIAL_REASONS)
            except Exception:  # the row stays 'recording'; the owner's recover() closes it later
                log.exception("meeting-scribe: recording %s could not be finished",
                              self.meeting.id if self.meeting else "-")
            text = t("capture.stopped" if self.heard else "capture.stopped_empty", self.lang,
                     reason=t(f"capture.reason_{reason}", self.lang), id=self.meeting.id if self.meeting else "-")
            if self.missing_audio:
                names = ", ".join(safe_name(self._speakers[u].name if u in self._speakers else u)
                                  for u in self.missing_audio)
                text += "\n⚠️ " + t("notes.missing_audio", self.lang, names=names)
            await self.consent.announce(text)
        except Exception:
            log.exception("meeting-scribe: finalize of guild %s failed", self.guild_id)
        finally:
            self._finished.set()

    def finalize_sync(self, reason: str = "shutdown") -> None:
        """Best-effort finalize without the event loop (gateway shutdown / plugin unload)."""
        if self.done:
            return
        self.done = True
        self.reason = reason
        try:
            self._stop_receiver()
            self._check_missing_audio()
            self._close_writers()
            self._finish(True)
        finally:
            self._finished_threadsafe()

    def _finished_threadsafe(self) -> None:
        loop = getattr(self._finished, "_loop", None)
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._finished.set)
        else:
            self._finished.set()

    def _stop_receiver(self) -> None:
        if self.receiver is None:
            return
        try:
            self.receiver.stop()
        except Exception as exc:  # the socket may already be gone after a disconnect
            log.debug("meeting-scribe receiver stop: %s", exc)

    def _close_writers(self) -> None:
        for uid, writer in list(self._writers.items()):
            try:
                writer.close()
            except Exception:
                log.exception("meeting-scribe: closing track %s failed", uid)
            if writer.error:
                log.warning("meeting-scribe: track %s had errors: %s", uid, writer.error)

    def _check_missing_audio(self) -> None:
        """People unmuted in the channel > ``MISSING_AUDIO_SECONDS`` whose voice reached no track."""
        if self.receiver is None:
            return
        report = self.receiver.voice_report()
        for label, uid in report.resolved.items():  # SPEAKING came after the audio went unidentified
            sp = self._speaker_for(uid)
            if sp is not None and label in self._speakers:
                self._speakers[label] = Speaker(label, sp.name)
        captured = {str(u) for u in self._writers} | {str(u) for u in self._writer_errors}
        captured |= {str(u) for u in report.resolved.values()}
        missing = tuple(uid for uid, secs in self._unmuted_seconds.items()
                        if secs > MISSING_AUDIO_SECONDS and uid not in captured)
        self.missing_audio = missing
        if missing or report.undecided or report.unidentified:
            names = {uid: self._speakers[uid].name if uid in self._speakers else uid for uid in missing}
            log.warning("meeting-scribe %s: audio not captured for %s; ssrc never identified: %s; "
                        "recorded as unidentified: %s; identified without SPEAKING: %s",
                        self.meeting.id if self.meeting else "-",
                        ", ".join(f"{n} ({u})" for u, n in names.items()) or "nobody",
                        ", ".join(f"{s} ({n} packets)" for s, n in report.undecided.items()) or "none",
                        ", ".join(f"{lbl}=ssrc {s}" for lbl, s in report.unidentified.items()) or "none",
                        ", ".join(f"ssrc {s}->{u} ({how})" for s, (u, how) in report.identified.items()) or "none")

    @property
    def heard(self) -> bool:
        """Whether audio of any person (never a bot) reached a track writer during the recording.

        A writer that failed to start still counts: audio arrived, the pipeline decides the rest."""
        return bool(self._writers or self._writer_errors)

    def _finish(self, partial: bool) -> None:
        if self.meeting is None:
            return
        self.meeting = replace(self.meeting, speakers=tuple(self._speakers.values()))
        self.deps.service.finish_recording(self.meeting.id, speakers=tuple(self._speakers.values()),
                                           partial=partial, heard=self.heard, missing_audio=self.missing_audio)

    async def _release_voice(self) -> None:
        vc = self.vc
        if vc is None:
            return
        if self.adapter._voice_clients.get(self.guild_id) is vc:
            self.adapter._voice_clients.pop(self.guild_id, None)
        try:  # force: also tears down a client that is mid-reconnect (is_connected() False)
            await vc.disconnect(force=True)
        except Exception as exc:  # already torn down by discord.py
            log.debug("meeting-scribe voice disconnect: %s", exc)
