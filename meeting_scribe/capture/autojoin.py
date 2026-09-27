"""Auto-join (DESIGN §4): start recording when people gather in a voice channel.

``on_voice_state_update`` is registered with ``bot.add_listener``. Every event re-evaluates the
channels it touches; a channel with at least ``autojoin_min_humans`` humans (bots never count)
gets ONE watcher task that waits ``autojoin_grace_seconds`` while re-checking the conditions, so
bursts of join/leave/mute events debounce to a single start. ``busy(guild)`` (a live or starting
session, or any other voice client such as ``/voice join``) blocks a start: one voice connection
per guild. Auto-leave is not decided here — the session polls the same human count every tick.

Cooldown (review W2): after a manual ``/meeting stop`` or a ``max_duration`` stop the channel is
put on cooldown until its human count drops below ``min_humans`` (people left, the meeting is
over); otherwise the bot's own leave event would re-join 20 s after the user said stop. Voice
events of bots (including our own) are ignored. ``close()`` may be called from any thread: the
watchers are cancelled on their own loop (``call_soon_threadsafe``).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional, Protocol

from ..config import Settings

log = logging.getLogger(__name__)
COOLDOWN_REASONS = frozenset({"stopped", "max_duration"})


class Starter(Protocol):
    async def start_in(self, channel: Any, *, started_by: Optional[str] = None) -> Any: ...

    def busy(self, guild: Any) -> bool: ...


def humans_in(channel: Any) -> int:
    return sum(1 for m in getattr(channel, "members", []) or [] if not getattr(m, "bot", False))


def _matches(channel: Any, entries: tuple[str, ...]) -> bool:
    wanted = {e.strip().lstrip("#").casefold() for e in entries}
    return str(channel.id) in wanted or str(getattr(channel, "name", "")).casefold() in wanted


class AutoJoiner:
    def __init__(self, starter: Starter, settings: Callable[[], Settings], *, clock: Callable[[], float] = time.monotonic,
                 poll: float = 1.0) -> None:
        self._starter = starter
        self._settings = settings
        self._clock = clock
        self._poll = poll
        self._watchers: dict[int, asyncio.Task] = {}
        self._cooldown: set[int] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def eligible(self, channel: Any) -> bool:
        s = self._settings()
        if not s.autojoin_enabled or channel is None or getattr(channel, "guild", None) is None:
            return False
        if s.autojoin_channels and not _matches(channel, s.autojoin_channels):
            return False
        if _matches(channel, s.autojoin_ignore_channels):
            return False
        enough = humans_in(channel) >= s.autojoin_min_humans
        if channel.id in self._cooldown:
            if enough:
                return False
            self._cooldown.discard(channel.id)  # the meeting emptied out: auto-join may fire again
        return enough and not self._starter.busy(channel.guild)

    def note_session_end(self, session: Any) -> None:
        """Controller callback: a deliberate stop must not be undone by auto-join."""
        if getattr(session, "reason", None) in COOLDOWN_REASONS:
            self._cooldown.add(session.channel.id)

    async def on_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        self._loop = asyncio.get_running_loop()
        for channel in {id(c): c for c in (getattr(before, "channel", None), getattr(after, "channel", None))
                        if c is not None}.values():
            if getattr(member, "bot", False):
                self._clear_cooldown_if_quiet(channel)
                continue
            self._consider(channel)

    def _clear_cooldown_if_quiet(self, channel: Any) -> None:
        if channel.id in self._cooldown and humans_in(channel) < self._settings().autojoin_min_humans:
            self._cooldown.discard(channel.id)

    def _consider(self, channel: Any) -> None:
        task = self._watchers.get(channel.id)
        if task is not None and not task.done():
            return  # the watcher re-checks on every poll
        if self.eligible(channel):
            self._watchers[channel.id] = asyncio.ensure_future(self._watch(channel))

    async def _watch(self, channel: Any) -> None:
        since = self._clock()
        try:
            while True:
                if not self.eligible(channel):
                    return
                if self._clock() - since >= self._settings().autojoin_grace_seconds:
                    break
                await asyncio.sleep(self._poll)
            log.info("meeting-scribe: auto-joining %s (%d humans)", channel.name, humans_in(channel))
            await self._starter.start_in(channel)
        except asyncio.CancelledError:
            raise
        except Exception:  # permissions, busy races: log and wait for the next voice event
            log.exception("meeting-scribe: auto-join of %s failed", getattr(channel, "name", channel))
        finally:
            if self._watchers.get(channel.id) is asyncio.current_task():
                del self._watchers[channel.id]

    def close(self) -> None:
        loop = self._loop
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if loop is not None and not on_loop and loop.is_running():
            loop.call_soon_threadsafe(self._cancel_all)
        else:
            self._cancel_all()

    def _cancel_all(self) -> None:
        for task in list(self._watchers.values()):
            task.cancel()
        self._watchers.clear()
