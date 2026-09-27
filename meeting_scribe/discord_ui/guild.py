"""Guild channels as data (DESIGN §16): snapshot for routing, and a project catalog for analysis.

Everything touching the discord.py cache runs on the gateway loop. The catalog is called from the
pipeline thread (analyze stage), so it marshals the snapshot with ``run_coroutine_threadsafe``;
without a connected adapter it simply offers nothing.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional, Sequence

from ..domain.models import Candidate, Meeting
from ..domain.names import clean_channel_name
from .routing import ChannelInfo

log = logging.getLogger(__name__)
SOURCE = "discord"
TEXT_KINDS = frozenset({"text", "news"})
SNAPSHOT_TIMEOUT = 10.0


def _kind(channel: Any) -> Optional[str]:
    kind = str(getattr(channel, "type", "") or "")
    kind = kind.rsplit(".", 1)[-1]
    if kind == "category":
        return "category"
    return "text" if kind in TEXT_KINDS else None


def can_post(channel: Any, member: Any, *, need_threads: bool) -> bool:
    try:
        perms = channel.permissions_for(member)
    except Exception:  # partial objects in tests / uncached member: assume we can and let Discord decide
        return True
    ok = bool(getattr(perms, "view_channel", False) and getattr(perms, "send_messages", False))
    return ok and (not need_threads or bool(getattr(perms, "create_public_threads", False)))


def snapshot_channels(guild: Any, *, need_threads: bool) -> list[ChannelInfo]:
    me = getattr(guild, "me", None)
    out: list[ChannelInfo] = []
    for ch in list(getattr(guild, "channels", None) or ()):
        kind = _kind(ch)
        if kind is None:
            continue
        cat = getattr(ch, "category_id", None)
        out.append(ChannelInfo(str(ch.id), str(getattr(ch, "name", "") or ""), kind,
                               str(cat) if cat else None, int(getattr(ch, "position", 0) or 0),
                               kind == "category" or can_post(ch, me, need_threads=need_threads)))
    return out


def guild_of(adapter: Any, meeting: Meeting) -> Any:
    client = getattr(adapter, "_client", None)
    getter = getattr(client, "get_guild", None)
    try:
        return getter(int(meeting.guild_id)) if callable(getter) else None
    except (TypeError, ValueError):
        return None


def to_candidates(channels: Sequence[ChannelInfo], ignore_prefixes: Sequence[str]) -> list[Candidate]:
    out: list[Candidate] = []
    for c in channels:
        name = clean_channel_name(c.name, ignore_prefixes)
        if name:
            out.append(Candidate(f"{SOURCE}:{c.id}", name, SOURCE, {"channel_id": c.id, "kind": c.kind}))
    return out


class DiscordChannelCatalog:
    """``ProjectCatalog`` over the meeting guild's text channels and categories."""

    name = SOURCE

    def __init__(self, *, adapter: Callable[[], Any], loop: Callable[[], Optional[asyncio.AbstractEventLoop]],
                 ignore_prefixes: Callable[[], Sequence[str]]) -> None:
        self._adapter = adapter
        self._loop = loop
        self._ignore = ignore_prefixes

    def candidates(self, meeting: Meeting) -> list[Candidate]:
        adapter, loop = self._adapter(), self._loop()
        if adapter is None or loop is None or loop.is_closed():
            return []

        async def snap() -> list[ChannelInfo]:
            guild = guild_of(adapter, meeting)
            return snapshot_channels(guild, need_threads=False) if guild is not None else []
        try:
            channels = asyncio.run_coroutine_threadsafe(snap(), loop).result(SNAPSHOT_TIMEOUT)
        except Exception as exc:  # loop busy/closing: fewer candidates, never a failed analysis
            log.info("meeting-scribe: Discord channel catalog unavailable: %s", exc)
            return []
        return to_candidates(channels, tuple(self._ignore()))
