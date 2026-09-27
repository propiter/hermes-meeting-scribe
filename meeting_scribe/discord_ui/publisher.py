"""Discord message primitives shared by the notes sink (DESIGN §14, §16) — run on the gateway loop.

Every message we post is remembered in a mutable ``deliveries`` pointer so a reprocess, a button
click or a 📁 move EDITS it instead of posting a duplicate. A pointer is ``{"channel", "message"}``
(plus extra fields per kind). Editing a deleted message raises; the caller then posts a fresh copy.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional, Protocol, Sequence

from .render import ButtonSpec, MessageSpec
from .render_tasks import TaskPanel

log = logging.getLogger(__name__)
SINK = "discord"


class ViewFactory(Protocol):
    def view(self, buttons: Sequence[ButtonSpec]) -> Any: ...

    def panel_view(self, panel: TaskPanel) -> Any: ...

    def send_kwargs(self) -> dict[str, Any]: ...


class Pointers:
    """Async access to the ``deliveries`` pointers of one meeting (SQLite runs in a worker thread)."""

    def __init__(self, repo: Any, meeting_id: str) -> None:
        self.repo = repo
        self.meeting_id = meeting_id

    def key(self, suffix: str) -> str:
        return f"mtg:{self.meeting_id}:{suffix}"

    async def load(self, suffix: str) -> Optional[dict[str, Any]]:
        row = await asyncio.to_thread(self.repo.get_delivery, SINK, self.key(suffix))
        return json.loads(row["external_id"]) if row and row.get("external_id") else None

    async def save(self, suffix: str, ptr: dict[str, Any], url: str = "") -> None:
        await asyncio.to_thread(self.repo.upsert_delivery, self.meeting_id, SINK, self.key(suffix),
                                external_id=json.dumps(ptr), url=url)

    async def drop(self, suffix: str) -> None:
        await asyncio.to_thread(self.repo.delete_delivery, SINK, self.key(suffix))

    async def with_prefix(self, prefix: str) -> dict[str, dict[str, Any]]:
        rows = await asyncio.to_thread(self.repo.list_deliveries, self.meeting_id, sink=SINK,
                                       prefix=self.key(prefix))
        start = len(self.key(prefix))
        return {r["key"][start:]: json.loads(r["external_id"]) for r in rows if r.get("external_id")}


class Messages:
    def __init__(self, adapter: Any, views: ViewFactory) -> None:
        self.adapter = adapter
        self.views = views

    async def channel(self, cid: Any) -> Any:
        return await self.adapter._resolve_channel(cid)

    async def send(self, channel: Any, *, spec: Optional[MessageSpec] = None, panel: Optional[TaskPanel] = None,
                   dm_user: Any = None) -> Any:
        target = dm_user if dm_user is not None else channel
        if panel is not None:  # components v2: the text lives inside the view, no ``content``
            return await target.send(view=self.views.panel_view(panel), **self.views.send_kwargs())
        assert spec is not None
        return await target.send(spec.content, view=self.views.view(spec.buttons), **self.views.send_kwargs())

    async def edit(self, channel: Any, message_id: Any, *, spec: Optional[MessageSpec] = None,
                   panel: Optional[TaskPanel] = None) -> Any:
        msg = channel.get_partial_message(int(message_id))
        if panel is not None:
            await msg.edit(view=self.views.panel_view(panel))
        else:
            assert spec is not None
            await msg.edit(content=spec.content, view=self.views.view(spec.buttons))
        return msg

    async def delete(self, channel_id: Any, message_id: Any) -> None:
        try:
            channel = await self.channel(channel_id)
            await channel.get_partial_message(int(message_id)).delete()
        except Exception as exc:  # already gone / no permission: the pointer is dropped anyway
            log.info("meeting-scribe: could not delete message %s: %s", message_id, exc)

    async def edit_or_send(self, ptr: Optional[dict[str, Any]], channel: Any, *, spec: Optional[MessageSpec] = None,
                           panel: Optional[TaskPanel] = None) -> dict[str, Any]:
        """Edit ``ptr``'s message when it lives in ``channel``; otherwise post there (and delete the old one)."""
        if ptr and str(ptr.get("channel")) == str(channel.id) and ptr.get("message"):
            try:
                await self.edit(channel, ptr["message"], spec=spec, panel=panel)
                return {"channel": channel.id, "message": ptr["message"]}
            except Exception as exc:  # deleted by someone: post a fresh copy
                log.info("meeting-scribe: message %s gone (%s); re-posting", ptr.get("message"), exc)
        msg = await self.send(channel, spec=spec, panel=panel)
        if ptr and ptr.get("message") and str(ptr.get("channel")) != str(channel.id):
            await self.delete(ptr["channel"], ptr["message"])
        return {"channel": channel.id, "message": msg.id, "url": getattr(msg, "jump_url", "") or ""}
