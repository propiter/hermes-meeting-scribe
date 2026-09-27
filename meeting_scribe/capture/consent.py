"""Consent signals of a recording (DESIGN §4): the announcement and the ``[REC]`` nickname prefix.

Both are optional by design (missing Send Messages / Manage Nicknames must not stop a recording).
The nickname is self-healing (review W9): a prefix left behind by a crash or a failed restore
during gateway shutdown is stripped and restored at the end of the next recording, instead of
being treated as "already applied by someone else" and left forever.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from ..config import Settings

log = logging.getLogger(__name__)


class Consent:
    ANNOUNCE_TIMEOUT = 10.0  # a 429 sleep must not stall start/finalize (review W5/W6)

    def __init__(self, guild: Any, channel: Any, text_channel: Any, settings: Callable[[], Settings]) -> None:
        self.guild = guild
        self.channel = channel
        self.text_channel = text_channel
        self._settings = settings
        self.nick_before: Optional[str] = None
        self.nick_changed = False

    async def announce(self, text: str) -> None:
        if not self._settings().consent_announce:
            return
        target = self.text_channel or self.channel
        try:
            await asyncio.wait_for(target.send(text), self.ANNOUNCE_TIMEOUT)
        except Exception as exc:  # missing Send Messages in the voice text chat must not stop recording
            log.warning("meeting-scribe: announcement failed in %s: %s", getattr(target, "id", "?"), exc)

    async def set_nickname(self) -> None:
        prefix = self._settings().consent_nickname_prefix
        me = getattr(self.guild, "me", None)
        if not prefix or me is None:
            return
        current: Optional[str] = getattr(me, "nick", None)
        if current is not None and current.startswith(prefix):
            # A leftover prefix (crash / failed restore): keep it, restore the stripped name later.
            stripped = current[len(prefix):]
            global_names = {getattr(me, "global_name", None), getattr(me, "name", None)} - {None}
            self.nick_before = None if (not stripped or stripped in global_names) else stripped
            self.nick_changed = True
            return
        base = current or getattr(me, "display_name", "") or ""
        try:
            await me.edit(nick=(prefix + base)[:32])
        except Exception as exc:  # needs Manage Nicknames; optional by design
            log.info("meeting-scribe: nickname prefix not applied: %s", exc)
            return
        self.nick_before, self.nick_changed = current, True

    async def restore_nickname(self) -> None:
        if not self.nick_changed:
            return
        self.nick_changed = False
        try:
            await self.guild.me.edit(nick=self.nick_before)
        except Exception as exc:
            log.info("meeting-scribe: nickname restore failed: %s", exc)
