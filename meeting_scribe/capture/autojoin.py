"""Auto-join (DESIGN §4): start recording when people gather in a voice channel.

``on_voice_state_update`` is registered with ``bot.add_listener``. Every event re-evaluates the
channels it touches; a channel with at least ``autojoin.min_humans`` humans (bots never count)
gets ONE watcher task that waits ``autojoin.grace_seconds`` while re-checking the conditions, so
bursts of join/leave/mute events debounce to a single start. ``busy(guild)`` (a live or starting
session, or any other voice client such as ``/voice join``) blocks a start: one voice connection
per guild. Auto-leave is not decided here — the session polls the same human count every tick.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional, Protocol

from ..config import Settings

log = logging.getLogger(__name__)


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

    def eligible(self, channel: Any) -> bool:
        s = self._settings()
        if not s.autojoin_enabled or channel is None or getattr(channel, "guild", None) is None:
            return False
        if s.autojoin_channels and not _matches(channel, s.autojoin_channels):
            return False
        if _matches(channel, s.autojoin_ignore_channels):
            return False
        return humans_in(channel) >= s.autojoin_min_humans and not self._starter.busy(channel.guild)

    async def on_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        for channel in {id(c): c for c in (getattr(before, "channel", None), getattr(after, "channel", None))
                        if c is not None}.values():
            self._consider(channel)

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
        for task in self._watchers.values():
            task.cancel()
        self._watchers.clear()
