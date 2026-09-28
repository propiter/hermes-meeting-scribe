"""Withdrawing what a meeting left in public places once it became private (DESIGN §19.2).

Everything is DELETED when Discord allows it. When it refuses (a thread or forum post needs Manage
Threads to be deleted, even the bot's own), the content is emptied instead: each of the bot's messages
inside is deleted (a bot may always delete its own messages) or, failing that, edited to a neutral text
without buttons or files, and the thread is renamed to a neutral name, archived and locked. What could
not be finished is remembered as a ``withdraw:<kind>:<channel>:<message>`` pointer and retried on every
later publish of the meeting; ``doctor`` lists it with the permission the bot is missing
(``PENDING_KV``). The private channel itself is never passed here (``private_share.withdraw_public``).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Optional

from ..i18n import t
from .publisher import Pointers, is_missing
from .render import MessageSpec
from .render_tasks import TaskPanel

if TYPE_CHECKING:
    from .task_publisher import TaskPublisher

log = logging.getLogger(__name__)
PREFIX = "withdraw:"
PENDING_KV = "privacy.withdraw_pending."  # + meeting id -> {"items": n, "missing": [permission labels]}
MANAGE_THREADS, MANAGE_MESSAGES = "Manage Threads", "Manage Messages"
HISTORY_LIMIT = 500  # messages scanned in a thread being emptied (the bot's own)


class Withdrawal:
    """One withdrawal pass for one meeting: every step is recorded until Discord confirms it."""

    def __init__(self, pub: "TaskPublisher", ptrs: Pointers) -> None:
        self.pub = pub
        self.msgs = pub.msgs
        self.ptrs = ptrs
        self.lang = pub.o.lang

    # -- entry points -----------------------------------------------------------------------------
    async def message(self, channel_id: Any, message_id: Any, *, panel: bool = False) -> None:
        await self._run({"kind": "panel" if panel else "message", "channel": str(channel_id),
                         "message": str(message_id)})

    async def thread(self, thread_id: Any) -> None:
        await self._run({"kind": "thread", "channel": str(thread_id)})

    async def retry(self) -> None:
        """Steps a previous pass could not finish (missing permission, Discord unavailable)."""
        for entry in (await self.ptrs.with_prefix(PREFIX)).values():
            await self._run(entry)

    async def report(self) -> None:
        """What is left, for ``doctor`` (another process): count and missing permissions."""
        left = list((await self.ptrs.with_prefix(PREFIX)).values())
        value = None
        if left:
            missing = sorted({str(e.get("missing")) for e in left if e.get("missing")})
            value = json.dumps({"items": len(left), "missing": missing})
        await asyncio.to_thread(self.pub.repo.kv_set, PENDING_KV + self.ptrs.meeting_id, value)

    # -- steps --------------------------------------------------------------------------------------
    def _key(self, entry: dict) -> str:
        return f"{PREFIX}{entry['kind']}:{entry['channel']}:{entry.get('message') or ''}"

    async def _run(self, entry: dict) -> None:
        kind = entry["kind"]
        missing = await (self._thread(entry["channel"]) if kind == "thread"
                         else self._message(entry["channel"], entry["message"], panel=kind == "panel"))
        if missing is None:
            await self.ptrs.drop(self._key(entry))
            return
        log.warning("meeting-scribe: meeting %s: %s %s not fully withdrawn (missing %s); retried later",
                    self.ptrs.meeting_id, kind, entry.get("message") or entry["channel"], missing)
        await self.ptrs.save(self._key(entry), {**entry, "missing": missing})

    async def _message(self, channel_id: str, message_id: str, *, panel: bool) -> Optional[str]:
        """``None`` when the message is gone or holds nothing any more; else the missing permission."""
        try:
            channel = await self.msgs.channel(channel_id)
        except Exception as exc:
            return None if is_missing(exc) else MANAGE_MESSAGES
        try:
            await channel.get_partial_message(int(message_id)).delete()
            return None
        except Exception as exc:
            if is_missing(exc):
                return None
            log.info("meeting-scribe: could not delete message %s (%s); emptying it", message_id, exc)
        return None if await self._empty(channel, message_id, panel=panel) else MANAGE_MESSAGES

    async def _empty(self, channel: Any, message_id: Any, *, panel: bool) -> bool:
        """Edit a message to the neutral text: no buttons, no attachments."""
        text = t("share.withdrawn", self.lang)
        try:
            if panel:  # components v2 (an assignee's DM panel): the text lives inside the view
                await self.msgs.edit(channel, message_id, panel=TaskPanel(text, (), (), 0, 1))
            else:
                await self.msgs.edit(channel, message_id, spec=MessageSpec(text), attachments=[])
            return True
        except Exception as exc:
            if is_missing(exc):
                return True
            log.info("meeting-scribe: could not empty message %s: %s", message_id, exc)
            return False

    async def _thread(self, thread_id: str) -> Optional[str]:
        """Delete a thread / forum post; if refused, empty it, rename it and archive + lock it."""
        try:
            thread = await self.msgs.channel(thread_id)
        except Exception as exc:
            return None if is_missing(exc) else MANAGE_THREADS
        try:
            await thread.delete()
            return None
        except Exception as exc:
            if is_missing(exc):
                return None
            log.info("meeting-scribe: could not delete thread %s (%s); emptying it", thread_id, exc)
        emptied = await self._empty_thread(thread)
        try:
            await thread.edit(name=t("share.withdrawn_name", self.lang), archived=True, locked=True)
        except Exception as exc:
            if is_missing(exc):
                return None
            log.info("meeting-scribe: could not rename/lock thread %s: %s", thread_id, exc)
            return MANAGE_THREADS
        return None if emptied else MANAGE_MESSAGES

    async def _empty_thread(self, thread: Any) -> bool:
        """Every message the bot posted in the thread: deleted, or emptied when that is refused."""
        me = getattr(getattr(thread, "guild", None), "me", None)
        if me is None:
            return False
        try:
            if getattr(thread, "archived", False):
                await thread.edit(archived=False)
            ours = [m async for m in thread.history(limit=HISTORY_LIMIT)
                    if str(getattr(getattr(m, "author", None), "id", "")) == str(me.id)]
        except Exception as exc:
            log.info("meeting-scribe: could not read thread %s: %s", getattr(thread, "id", "?"), exc)
            return False
        ok = True
        for msg in ours:
            ok = await self._message(str(thread.id), str(msg.id), panel=False) is None and ok
        return ok
