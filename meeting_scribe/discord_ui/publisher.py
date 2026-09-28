"""Discord message primitives shared by the notes sink (DESIGN §14, §16) — run on the gateway loop.

Every message we post is remembered in a mutable ``deliveries`` pointer so a reprocess, a button
click or a 📁 move EDITS it instead of posting a duplicate. A pointer is ``{"channel", "message"}``
(plus extra fields per kind). Only a CONFIRMED missing message (404 / Unknown Message / Channel) is
re-posted; any other failure (5xx, rate limit, timeout) propagates so the job retries instead of
duplicating. A message that cannot be deleted is disarmed (buttons removed, pointing elsewhere).

Forum and media channels accept no message of their own: :meth:`Messages.create_post` opens a post
(a thread whose first message carries the content); everything else is sent inside that post.
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
ARCHIVED_CODE = 50083  # "Operation cannot be performed on an archived thread"


class ViewFactory(Protocol):
    def view(self, buttons: Sequence[ButtonSpec]) -> Any: ...

    def panel_view(self, panel: TaskPanel) -> Any: ...

    def send_kwargs(self) -> dict[str, Any]: ...

    def mention_kwargs(self, users: Sequence[str]) -> dict[str, Any]: ...


def is_missing(exc: BaseException) -> bool:
    """Discord says the message/channel is gone (discord.NotFound: HTTP 404, codes 10003/10008)."""
    if isinstance(exc, LookupError):  # the adapter's "unknown channel" and our fakes
        return True
    return getattr(exc, "status", None) == 404 or getattr(exc, "code", None) in (10003, 10008)


def is_archived(exc: BaseException) -> bool:
    """The message lives in an archived thread (an old forum post or project thread): unarchive first."""
    return getattr(exc, "code", None) == ARCHIVED_CODE


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
        return await target.send(spec.content, view=self.views.view(spec.buttons), **self._mentions(spec))

    def _mentions(self, spec: MessageSpec) -> dict[str, Any]:
        """``allowed_mentions`` of a message: exactly the users its spec names, nobody by default. Text
        written by the model or typed by people (a summary, a task title, a Meet name) can hold ``<@id>``:
        it may show a mention but never notifies anyone the spec did not name (DESIGN §19.4)."""
        return self.views.mention_kwargs(spec.mentions)

    async def create_post(self, forum: Any, *, name: str, spec: MessageSpec, tags: Sequence[Any] = ()) -> tuple[Any, Any]:
        """A new post in a forum/media channel: ``(thread, first message)`` (discord.py 2.x
        ``ForumChannel.create_thread`` returns ``ThreadWithMessage``)."""
        kwargs = dict(self._mentions(spec))
        view = self.views.view(spec.buttons)
        if view is not None:
            kwargs["view"] = view
        if tags:
            kwargs["applied_tags"] = list(tags)
        thread, message = await forum.create_thread(name=name, content=spec.content, **kwargs)
        return thread, message

    async def edit(self, channel: Any, message_id: Any, *, spec: Optional[MessageSpec] = None,
                   panel: Optional[TaskPanel] = None, attachments: Optional[list] = None) -> Any:
        """``attachments=[]`` also removes the message's files (a withdrawn transcript part)."""
        try:
            return await self._edit(channel, message_id, spec=spec, panel=panel, attachments=attachments)
        except Exception as exc:
            if not is_archived(exc) or not callable(getattr(channel, "edit", None)):
                raise
        log.info("meeting-scribe: thread %s is archived; unarchiving it to edit message %s",
                 getattr(channel, "id", "?"), message_id)
        await channel.edit(archived=False)
        return await self._edit(channel, message_id, spec=spec, panel=panel, attachments=attachments)

    async def _edit(self, channel: Any, message_id: Any, *, spec: Optional[MessageSpec] = None,
                    panel: Optional[TaskPanel] = None, attachments: Optional[list] = None) -> Any:
        msg = channel.get_partial_message(int(message_id))
        extra = {} if attachments is None else {"attachments": attachments}
        if panel is not None:
            await msg.edit(view=self.views.panel_view(panel), **extra)
        else:
            assert spec is not None
            extra.update(self.views.mention_kwargs(()))  # an edit never pings (DESIGN §19.4)
            await msg.edit(content=spec.content, view=self.views.view(spec.buttons), **extra)
        return msg

    async def delete(self, channel_id: Any, message_id: Any, *, notice: str = "") -> bool:
        """Delete a message; if Discord refuses, disarm it (``notice``, no buttons). ``False`` = left behind."""
        try:
            channel = await self.channel(channel_id)
            await channel.get_partial_message(int(message_id)).delete()
            return True
        except Exception as exc:
            if is_missing(exc):
                return True
            log.info("meeting-scribe: could not delete message %s: %s", message_id, exc)
        try:  # no Manage Messages / transient: at least remove the buttons so nobody acts twice
            await self.edit(await self.channel(channel_id), message_id, spec=MessageSpec(notice or "↪️"))
        except Exception as exc:
            log.info("meeting-scribe: could not disarm message %s: %s", message_id, exc)
            return False
        return True

    async def edit_or_send(self, ptr: Optional[dict[str, Any]], channel: Any, *, spec: Optional[MessageSpec] = None,
                           panel: Optional[TaskPanel] = None, moved_notice: str = "") -> dict[str, Any]:
        """Edit ``ptr``'s message when it lives in ``channel``; otherwise post there (and delete the old one)."""
        if ptr and str(ptr.get("channel")) == str(channel.id) and ptr.get("message"):
            try:
                await self.edit(channel, ptr["message"], spec=spec, panel=panel)
                return {"channel": channel.id, "message": ptr["message"]}
            except Exception as exc:
                if not is_missing(exc):
                    raise  # transient: retry later rather than duplicate
                log.info("meeting-scribe: message %s gone (%s); re-posting", ptr.get("message"), exc)
        msg = await self.send(channel, spec=spec, panel=panel)
        if ptr and ptr.get("message") and str(ptr.get("channel")) != str(channel.id):
            await self.delete(ptr["channel"], ptr["message"], notice=moved_notice)
        return {"channel": channel.id, "message": msg.id, "url": getattr(msg, "jump_url", "") or ""}
