"""Gateway side of the channel catalog (DESIGN §19.3): keep ``Repository.set_guild_channels`` current.

At every connect the factory snapshots every server of the bot; afterwards discord.py's channel events
(create, delete, update — a rename or a permission change) re-snapshot that server once things settle
(:data:`DEBOUNCE` seconds after the last event), so a burst of edits costs one write. All of it runs on
the gateway loop: the snapshot reads the discord.py cache, the write is a single SQLite upsert.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from ..channel_catalog import snapshot_guild

log = logging.getLogger(__name__)
DEBOUNCE = 5.0
EVENTS = ("on_guild_channel_create", "on_guild_channel_delete", "on_guild_channel_update", "on_guild_join")


class ChannelWatch:
    def __init__(self, repo: Callable[[], Any], *, delay: float = DEBOUNCE) -> None:
        self._repo = repo
        self.delay = delay
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self.handlers: dict[str, Callable[..., Any]] = {name: self._handler(name) for name in EVENTS}

    def record(self, guild: Any) -> None:
        """Write the catalog of ``guild`` now; storage errors are logged (doctor reports storage)."""
        self._timers.pop(str(getattr(guild, "id", "")), None)
        try:
            self._repo().set_guild_channels(str(guild.id), snapshot_guild(guild))
        except Exception:
            log.exception("meeting-scribe: saving the channels of server %s failed", getattr(guild, "id", "?"))

    def record_all(self, guilds: Any) -> None:
        for guild in list(guilds or ()):
            self.record(guild)

    def schedule(self, guild: Optional[Any]) -> None:
        """Debounced :meth:`record` (the gateway loop must be running)."""
        if guild is None:
            return
        key = str(guild.id)
        old = self._timers.pop(key, None)
        if old is not None:
            old.cancel()
        self._timers[key] = asyncio.get_running_loop().call_later(self.delay, self.record, guild)

    def cancel(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()

    def attach(self, bot: Any) -> None:
        for name, handler in self.handlers.items():
            bot.add_listener(handler, name)

    def detach(self, bot: Any) -> None:
        for name, handler in self.handlers.items():
            try:
                bot.remove_listener(handler, name)
            except Exception as exc:  # the client is already torn down
                log.debug("meeting-scribe: removing %s failed: %s", name, exc)
        self.cancel()

    def _handler(self, name: str) -> Callable[..., Any]:
        async def handler(*args: Any) -> None:
            target = args[-1]  # update: (before, after); join: (guild,); create/delete: (channel,)
            self.schedule(target if name == "on_guild_join" else getattr(target, "guild", None))
        handler.__name__ = name
        return handler
