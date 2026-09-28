"""Capture controller assigned to ``runtime.capture`` (DESIGN §13).

Slash-command handlers are synchronous and Hermes runs them on its executor pool, while every
Discord object lives on the gateway event loop. :meth:`CaptureManager._dispatch` bridges the two:
from a worker thread it runs the coroutine on the adapter loop with
``asyncio.run_coroutine_threadsafe`` and waits; if it is (unusually) called on the loop thread
itself it schedules the coroutine and answers immediately instead of deadlocking the loop.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
import weakref
from typing import Any, Callable, Coroutine, Optional

from ..audio.ffmpeg import Ffmpeg, FfmpegNotFound
from ..commands import Caller
from ..config import Settings
from ..domain.text import is_ascii_digits
from ..i18n import t
from ..spaces import GuildUnassigned
from .compat import CompatResult, probe
from .receiver import scribe_receiver_class
from .session import Busy, RecordingSession, SessionDeps, Writer
from .tracks import TrackWriter

log = logging.getLogger(__name__)
WriterFactory = Callable[[Optional[Ffmpeg], Any, float, int], Writer]
_CHANNEL_MENTION = re.compile(r"^<#([0-9]+)>$")


def compat_for_adapter(adapter: Any) -> CompatResult:
    """Probe the classes of the adapter module actually loaded in this gateway."""
    mod = sys.modules.get(type(adapter).__module__)
    try:
        from discord.voice_state import VoiceConnectionState
    except Exception as exc:  # discord.py missing/restructured: report instead of crashing
        log.warning("meeting-scribe: discord.voice_state unavailable: %s", exc)
        conn: Optional[type] = None
    else:
        conn = VoiceConnectionState
    return probe(getattr(mod, "VoiceReceiver", None), type(adapter), conn, getattr(mod, "_component_check_auth", None))


def default_writer(ff: Optional[Ffmpeg], path: Any, t0: float, kbps: int) -> Writer:
    if ff is None:
        raise FfmpegNotFound("ffmpeg is required for live capture")
    return TrackWriter(ff, path, t0=t0, bitrate_kbps=kbps)


default_writer.requires_ffmpeg = True  # type: ignore[attr-defined]  # start_in fails fast without it


def is_voice_channel(channel: Any) -> bool:
    return channel is not None and callable(getattr(channel, "connect", None)) and hasattr(channel, "members")


class AlreadyRecording(RuntimeError):
    def __init__(self, session: RecordingSession) -> None:
        super().__init__(session.channel.name)
        self.session = session


class CaptureManager:
    START_TIMEOUT = 60.0
    STOP_TIMEOUT = 90.0

    def __init__(self, *, service: Callable[[], Any], settings: Callable[..., Settings],
                 ffmpeg: Callable[[], Optional[Ffmpeg]], writer_factory: WriterFactory = default_writer,
                 compat: Optional[Callable[[Any], CompatResult]] = None, tick: float = 0.5,
                 clock: Callable[[], float] = time.monotonic,
                 space_of: Optional[Callable[[Any], Optional[str]]] = None) -> None:
        """``space_of(guild)``: the space owning a server, ``None`` when it belongs to none (never
        recorded). Without it (unit tests of the capture alone) meetings carry no space."""
        self._service = service
        self._settings = settings
        self._space_of = space_of
        self._ffmpeg = ffmpeg
        self._writer_factory = writer_factory
        self._compat = compat or (lambda adapter: compat_for_adapter(adapter))  # late-bound for probes
        self._tick = tick
        self._clock = clock
        self._adapter_ref: Optional[weakref.ReferenceType] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._compat_result: Optional[CompatResult] = None
        self._receiver_cls: Optional[type] = None
        self._sessions: dict[int, RecordingSession] = {}
        self._starting: set[int] = set()
        self.on_session_end: list[Callable[[RecordingSession], None]] = []

    # -- wiring ---------------------------------------------------------------------------------
    def attach(self, bot: Any, adapter: Any) -> CompatResult:
        """Called from the Discord platform-handler factory (on the gateway loop) on each connect."""
        self._adapter_ref = weakref.ref(adapter)
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = getattr(bot, "loop", None)
        self._compat_result = self._compat(adapter)
        base = getattr(sys.modules.get(type(adapter).__module__), "VoiceReceiver", None)
        self._receiver_cls = scribe_receiver_class(base) if self._compat_result.ok and base else None
        if not self._compat_result.ok:
            log.warning("meeting-scribe: live capture disabled: %s", self._compat_result.summary())
        return self._compat_result

    @property
    def adapter(self) -> Any:
        return self._adapter_ref() if self._adapter_ref is not None else None

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """The gateway loop the adapter runs on (set by :meth:`attach`)."""
        return self._loop

    @property
    def compat_result(self) -> Optional[CompatResult]:
        return self._compat_result

    @property
    def lang(self) -> str:
        return self._settings().ui_language

    def space_of(self, guild: Any) -> Optional[str]:
        """The space of ``guild`` ("" without space resolution); ``None``: the server is not recorded."""
        return "" if self._space_of is None else self._space_of(guild)

    def settings_for(self, space: str) -> Settings:
        return self._settings(space) if space else self._settings()

    def status(self) -> tuple[bool, str]:
        if self.adapter is None:
            return True, "capture installed; waiting for the Discord adapter to connect"
        res = self._compat_result
        if res is None or not res.ok:
            # Admin-facing (doctor / status CLI): the technical detail belongs here, not in the chat.
            return False, f"live capture disabled: {res.summary() if res else 'adapter not probed yet'}"
        return True, f"capture ready ({res.summary()}); {len(self.live_meeting_ids())} live recording(s)"

    # -- public controller API (sync) -----------------------------------------------------------
    def start(self, caller: Caller, target: Optional[str]) -> str:
        return self._dispatch(lambda: self._start(caller, target), t("capture.starting", self.lang),
                              self.START_TIMEOUT)

    def stop(self, caller: Caller) -> str:
        return self._dispatch(lambda: self._stop(caller), t("capture.stopping", self.lang), self.STOP_TIMEOUT)

    def live_meeting_ids(self) -> set[str]:
        # Until teardown FINISHES (not merely begins): the row is still 'recording' while tracks are
        # flushed, and recover() must not close it as an orphan meanwhile.
        return {s.meeting.id for s in self._sessions.values() if s.meeting is not None and not s.finished}

    def session_for(self, guild_id: int) -> Optional[RecordingSession]:
        s = self._sessions.get(int(guild_id))
        return s if s is not None and not s.done else None

    def busy(self, guild: Any) -> bool:
        """A live/starting session or ANY voice client of this guild (ours, ``/voice join``, or one
        discord.py still holds mid-reconnect in ``guild.voice_client``) blocks a new start."""
        adapter = self.adapter
        vc = adapter._voice_clients.get(int(guild.id)) if adapter is not None else None
        return (int(guild.id) in self._starting or self.session_for(guild.id) is not None
                or (vc is not None and vc.is_connected()) or getattr(guild, "voice_client", None) is not None)

    def _dispatch(self, make: Callable[[], Coroutine[Any, Any, str]], pending: str, timeout: float) -> str:
        loop = self._loop
        if self.adapter is None or loop is None or loop.is_closed():
            return t("capture.not_connected", self.lang)
        try:
            running: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            task = loop.create_task(make())
            task.add_done_callback(_log_task_error)
            return pending
        fut = asyncio.run_coroutine_threadsafe(make(), loop)
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()
            return t("capture.timeout", self.lang)

    # -- coroutines on the gateway loop ---------------------------------------------------------
    async def _caller_guild(self, caller: Caller) -> tuple[Any, Optional[str]]:
        if (caller.platform or "").lower() != "discord":
            return None, t("capture.discord_only", self.lang)
        try:
            here = await self.adapter._resolve_channel(caller.chat_id)
        except Exception as exc:  # unknown/partial channel: treat like a DM
            log.info("meeting-scribe: cannot resolve channel %s: %s", caller.chat_id, exc)
            here = None
        guild = getattr(here, "guild", None)
        return (guild, None) if guild is not None else (None, t("capture.no_guild", self.lang))

    def _target_channel(self, guild: Any, caller: Caller, target: Optional[str]) -> tuple[Any, Optional[str]]:
        if target:
            raw = target.strip()
            m = _CHANNEL_MENTION.match(raw)
            cid = m.group(1) if m else (raw if is_ascii_digits(raw) else None)
            if cid is not None:
                ch = guild.get_channel(int(cid))
            else:
                name = raw.lstrip("#").casefold()
                ch = next((c for c in guild.channels if is_voice_channel(c) and c.name.casefold() == name), None)
            if ch is None:
                return None, t("capture.target_not_found", self.lang, target=raw)
            if not is_voice_channel(ch):
                return None, t("capture.target_not_voice", self.lang, channel=ch.name)
            return ch, None
        member = guild.get_member(int(caller.user_id)) if is_ascii_digits(str(caller.user_id)) else None
        voice = getattr(getattr(member, "voice", None), "channel", None)
        return (voice, None) if voice is not None else (None, t("capture.join_voice_first", self.lang))

    async def _start(self, caller: Caller, target: Optional[str]) -> str:
        res = self._compat_result
        if res is None or not res.ok or self._receiver_cls is None:
            log.warning("meeting-scribe: /meeting start refused, live capture disabled: %s",
                        res.summary() if res else "adapter not probed yet")
            return t("capture.incompatible", self.lang)
        guild, err = await self._caller_guild(caller)
        if err:
            return err
        channel, err = self._target_channel(guild, caller, target)
        if err:
            return err
        try:
            session = await self.start_in(channel, started_by=caller.user_id)
        except GuildUnassigned:
            return t("capture.unassigned", self.lang, guild=guild.id)
        except AlreadyRecording as exc:
            s = exc.session
            key = "capture.already" if int(s.channel.id) == int(channel.id) else "capture.other_channel"
            return t(key, self.lang, channel=s.channel.name, id=s.meeting.id if s.meeting else "-")
        except Busy as exc:
            return str(exc)
        except FfmpegNotFound as exc:
            log.warning("meeting-scribe: /meeting start refused: %s", exc)
            return t("capture.ffmpeg_missing", self.lang)
        return t("capture.started", self.lang, channel=channel.name, id=session.meeting.id if session.meeting else "-")

    async def start_in(self, channel: Any, *, started_by: Optional[str] = None) -> RecordingSession:
        """Start recording ``channel`` (used by ``/meeting start`` and auto-join)."""
        gid = int(channel.guild.id)
        live = self.session_for(gid)
        if live is not None:
            raise AlreadyRecording(live)
        if gid in self._starting or self._receiver_cls is None:
            raise Busy(t("capture.busy", self.lang))
        ff = self._ffmpeg()
        if ff is None and getattr(self._writer_factory, "requires_ffmpeg", False):
            raise FfmpegNotFound("ffmpeg is required for live capture")  # before joining the channel
        self._starting.add(gid)  # before the first await: a concurrent start sees it (review W7)
        try:
            space = await asyncio.to_thread(self.space_of, channel.guild)
            if space is None:
                raise GuildUnassigned(f"Discord server {gid} belongs to no space")
            kbps = self._settings().audio_bitrate_kbps  # machine-wide
            deps = SessionDeps(service=self._service(), settings=lambda: self.settings_for(space),
                               receiver_cls=self._receiver_cls,
                               writer_factory=lambda path, t0: self._writer_factory(ff, path, t0, kbps),
                               clock=self._clock, tick=self._tick, space=space)
            session = RecordingSession(self.adapter, channel, deps, started_by=started_by)
            # Registered BEFORE start (review W7): /meeting stop and live_meeting_ids() see it at once.
            self._sessions[gid] = session
            asyncio.ensure_future(self._reap(session))
            await session.start()
        finally:
            self._starting.discard(gid)
        return session

    async def _reap(self, session: RecordingSession) -> None:
        await session.wait()
        if self._sessions.get(session.guild_id) is session:
            del self._sessions[session.guild_id]
        for cb in list(self.on_session_end):
            try:
                cb(session)
            except Exception:
                log.exception("meeting-scribe session-end listener failed")

    async def _stop(self, caller: Caller) -> str:
        guild, err = await self._caller_guild(caller)
        if err:
            return err
        session = self.session_for(guild.id)
        if session is None:
            return t("capture.not_recording", self.lang)
        meeting = await session.stop("stopped")
        return t("capture.stop_ok" if session.heard else "capture.stop_empty", self.lang,
                 id=meeting.id if meeting else "-")

    # -- shutdown -------------------------------------------------------------------------------
    def shutdown(self, timeout: float = 20.0) -> None:
        """Finalize every live recording as partial (plugin unload / gateway shutdown)."""
        loop = self._loop
        for session in list(self._sessions.values()):
            if session.done:
                continue
            try:
                on_loop = asyncio.get_running_loop() is loop
            except RuntimeError:
                on_loop = False
            if loop is not None and loop.is_running() and not on_loop:
                try:
                    asyncio.run_coroutine_threadsafe(session.stop("shutdown"), loop).result(timeout)
                    continue
                except Exception:
                    log.exception("meeting-scribe: graceful stop failed; finalizing synchronously")
            session.finalize_sync("shutdown")
        self._sessions.clear()


def _log_task_error(task: "asyncio.Task[Any]") -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("meeting-scribe background command failed", exc_info=task.exception())

