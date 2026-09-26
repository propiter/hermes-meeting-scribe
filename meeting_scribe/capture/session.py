"""One live recording in one guild (DESIGN §4).

Lifecycle: ``start()`` joins with our OWN ``channel.connect()`` under the adapter's per-guild voice
lock and registers the client in ``adapter._voice_clients`` (so Hermes' ``/voice status|leave``
and shutdown see it) — but never in ``_voice_receivers``/``_voice_listen_tasks``/
``_voice_text_channels``/``_voice_timeout_tasks``, so Hermes starts no agent turns, no TTS and no
inactivity timer. A drain loop (0.5 s) moves timed frames from the receiver into per-speaker
:class:`TrackWriter`s, sends the UDP keepalive, re-reads the DAVE session, and decides when to
stop: explicit stop, no humans for ``autoleave.grace_seconds``, ``limits.max_duration_minutes``,
or the voice client disappearing (network loss, ``/voice leave``, adapter disconnect → partial).
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
from ..i18n import t

log = logging.getLogger(__name__)
KEEPALIVE = b"\xf8\xff\xfe"
KEEPALIVE_SECONDS = 15.0
DAVE_REFRESH_SECONDS = 5.0
PARTIAL_REASONS = frozenset({"disconnected", "shutdown", "error"})


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
    clock: Callable[[], float] = time.time
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    tick: float = 0.5


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
        self._writers: dict[int, Writer] = {}
        self._speakers: dict[str, Speaker] = {}
        self._task: Optional[asyncio.Task] = None
        self._final_lock = asyncio.Lock()
        self._finished = asyncio.Event()
        self._nick_before: Optional[str] = None
        self._nick_changed = False

    @property
    def guild_id(self) -> int:
        return int(self.guild.id)

    @property
    def lang(self) -> str:
        return self.deps.settings().ui_language

    # -- start ----------------------------------------------------------------------------------
    async def start(self) -> Meeting:
        lock = self.adapter._voice_locks.setdefault(self.guild_id, asyncio.Lock())
        async with lock:
            existing = self.adapter._voice_clients.get(self.guild_id) or getattr(self.guild, "voice_client", None)
            if existing is not None and existing.is_connected():
                raise Busy(t("capture.busy", self.lang))
            self.vc = await self.channel.connect()
            self.adapter._voice_clients[self.guild_id] = self.vc
            try:
                self.receiver = self.deps.receiver_cls(self.vc, clock=self.deps.clock)
                self.receiver.start()
                self.t0 = self.deps.clock()
                self._note_members()
                self.meeting = await asyncio.to_thread(self.deps.service.begin_recording, self._new_meeting())
            except BaseException:
                await self._release_voice()
                raise
        await self._announce(t("capture.announce", self.lang, channel=self.channel.name))
        await self._set_nickname()
        self._task = asyncio.ensure_future(self._run())
        return self.meeting

    def _new_meeting(self) -> Meeting:
        category = getattr(getattr(self.channel, "category", None), "name", "") or ""
        text_id = getattr(self.text_channel, "id", None)
        return Meeting(id=short_id(), guild_id=str(self.guild.id), channel_id=str(self.channel.id),
                       channel_name=self.channel.name, started_at=self.deps.now(), state=MeetingState.RECORDING,
                       title=self.channel.name, speakers=tuple(self._speakers.values()),
                       guild_name=getattr(self.guild, "name", "") or "", category_name=category,
                       text_channel_id=str(text_id) if text_id is not None else None, started_by=self.started_by)

    # -- speakers -------------------------------------------------------------------------------
    def humans(self) -> list[Any]:
        return [m for m in getattr(self.channel, "members", []) if not getattr(m, "bot", False)]

    def _note_members(self) -> None:
        for m in self.humans():
            self._speakers.setdefault(str(m.id), Speaker(str(m.id), m.display_name))

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
    def _connected(self) -> bool:
        return self.vc is not None and self.vc.is_connected() and self.adapter._voice_clients.get(self.guild_id) is self.vc

    def _drain(self) -> None:
        if self.receiver is None or self.meeting is None:
            return
        for user_id, frames in self.receiver.drain().items():
            if self._speaker_for(user_id) is None:
                continue
            writer = self._writers.get(user_id)
            if writer is None:
                path = self.deps.service.track_path(self.meeting, str(user_id))
                writer = self._writers[user_id] = self.deps.writer_factory(path, self.t0)
            writer.write(frames)

    async def _run(self) -> None:
        clock = self.deps.clock
        last_keepalive = last_refresh = self.t0
        empty_since: Optional[float] = None
        reason = "error"
        try:
            while not self.done:
                await asyncio.sleep(self.deps.tick)
                if self.done:
                    return
                if not self._connected():
                    reason = "disconnected"
                    break
                self._drain()
                now = clock()
                if now - last_keepalive >= KEEPALIVE_SECONDS:
                    last_keepalive = now
                    try:
                        self.vc._connection.send_packet(KEEPALIVE)
                    except Exception as exc:  # UDP socket mid-reconnect; the next tick retries
                        log.debug("meeting-scribe keepalive failed: %s", exc)
                if now - last_refresh >= DAVE_REFRESH_SECONDS:
                    last_refresh = now
                    self.receiver.refresh_connection()
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
        await self._finalize(reason)
        return self.meeting

    async def wait(self) -> None:
        await self._finished.wait()

    async def _finalize(self, reason: str) -> None:
        async with self._final_lock:
            task, current = self._task, asyncio.current_task()
            if task is not None and task is not current and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if self.done:
                self._finished.set()
                return
            self.reason = reason
            try:
                self._drain()
            except Exception:
                log.exception("meeting-scribe final drain failed")
            self.done = True
            self._stop_receiver()
            await asyncio.to_thread(self._close_writers)
            await self._restore_nickname()
            await self._release_voice()
            await asyncio.to_thread(self._finish, reason in PARTIAL_REASONS)
            await self._announce(t("capture.stopped", self.lang, reason=t(f"capture.reason_{reason}", self.lang),
                                   id=self.meeting.id if self.meeting else "-"))
            self._finished.set()

    def finalize_sync(self, reason: str = "shutdown") -> None:
        """Best-effort finalize without the event loop (gateway shutdown / plugin unload)."""
        if self.done:
            return
        self.done = True
        self.reason = reason
        self._stop_receiver()
        self._close_writers()
        self._finish(True)

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

    def _finish(self, partial: bool) -> None:
        if self.meeting is None:
            return
        self.meeting = replace(self.meeting, speakers=tuple(self._speakers.values()))
        self.deps.service.finish_recording(self.meeting.id, speakers=tuple(self._speakers.values()),
                                           partial=partial)

    async def _release_voice(self) -> None:
        vc = self.vc
        if vc is None:
            return
        if self.adapter._voice_clients.get(self.guild_id) is vc:
            self.adapter._voice_clients.pop(self.guild_id, None)
        if vc.is_connected():
            try:
                await vc.disconnect()
            except Exception as exc:  # already torn down by discord.py
                log.debug("meeting-scribe voice disconnect: %s", exc)

    # -- consent --------------------------------------------------------------------------------
    async def _announce(self, text: str) -> None:
        if not self.deps.settings().consent_announce:
            return
        target = self.text_channel or self.channel
        try:
            await target.send(text)
        except Exception as exc:  # missing Send Messages in the voice text chat must not stop recording
            log.warning("meeting-scribe: announcement failed in %s: %s", getattr(target, "id", "?"), exc)

    async def _set_nickname(self) -> None:
        prefix = self.deps.settings().consent_nickname_prefix
        me = getattr(self.guild, "me", None)
        if not prefix or me is None:
            return
        current = getattr(me, "nick", None)
        base = current or getattr(me, "display_name", "") or ""
        if base.startswith(prefix):
            return
        try:
            await me.edit(nick=(prefix + base)[:32])
        except Exception as exc:  # needs Manage Nicknames; optional by design
            log.info("meeting-scribe: nickname prefix not applied: %s", exc)
            return
        self._nick_before, self._nick_changed = current, True

    async def _restore_nickname(self) -> None:
        if not self._nick_changed:
            return
        self._nick_changed = False
        try:
            await self.guild.me.edit(nick=self._nick_before)
        except Exception as exc:
            log.info("meeting-scribe: nickname restore failed: %s", exc)
