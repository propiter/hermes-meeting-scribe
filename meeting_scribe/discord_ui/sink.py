"""Discord notes sink (DESIGN §8, §16; satisfies :class:`meeting_scribe.sinks.base.DiscordNotesSink`).

``deliver`` runs on the pipeline thread and hands the publishing coroutine to the gateway loop
(``run_coroutine_threadsafe``) with a timeout. The layout (summary + task index in the meeting chat,
one message per task in its project's channel thread, assignee DMs) is built by
:class:`~meeting_scribe.discord_ui.task_publisher.TaskPublisher`; every message has a pointer in
``deliveries`` so reprocesses and button clicks edit in place.

Loop-side entry points used by the buttons: :meth:`refresh_item` (after an action: that task's
message, its assignee's DM and the index counts), :meth:`task_panel` (📋 My tasks), :meth:`move_options`
and :meth:`move_item` (📁).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import Settings
from ..domain.models import SOURCE_GOOGLE_MEET, Meeting, Notes, SinkResult, is_discord_user_id
from ..domain.names import clean_channel_name
from ..storage.artifacts import read_notes, read_transcript, render_transcript_md
from .board import Board, move_options
from .publisher import Pointers, ViewFactory
from .render import RenderOptions
from .render_tasks import render_panel
from .task_publisher import TaskPublisher

log = logging.getLogger(__name__)
SINK = "discord"

__all__ = ["DiscordNotesSink", "ViewFactory", "SINK"]


class DiscordNotesSink:
    name = SINK

    def __init__(self, *, settings: Callable[[], Settings], service: Callable[[], Any], adapter: Callable[[], Any],
                 loop: Callable[[], Optional[asyncio.AbstractEventLoop]], options: Callable[[Meeting], RenderOptions],
                 views: ViewFactory, timeout: float = 120.0) -> None:
        self._settings = settings
        self._service = service
        self._adapter = adapter
        self._loop = loop
        self._options = options
        self._views = views
        self._timeout = timeout
        self._locks: dict[str, asyncio.Lock] = {}

    def enabled(self) -> bool:
        return self._settings().delivery_discord_enabled

    # -- pipeline thread -----------------------------------------------------------------------
    def deliver(self, meeting: Meeting, notes: Notes, folder: Path) -> SinkResult:
        adapter, loop = self._adapter(), self._loop()
        if adapter is None or loop is None or loop.is_closed():
            return SinkResult(SINK, False, errors=("discord not connected yet; will retry",), deferred=True)
        if not self._targets(meeting):  # e.g. a Meet import with no channel configured: skip, don't loop
            log.info("meeting-scribe: no Discord channel for meeting %s; Discord delivery skipped", meeting.id)
            return SinkResult(SINK, True, skipped=("no Discord notes channel configured",))
        fut = asyncio.run_coroutine_threadsafe(self.publish(meeting, notes), loop)
        try:
            url = fut.result(self._timeout)
        except Exception as exc:  # timeout, permissions, unknown channel: reported, the job retries
            fut.cancel()
            return SinkResult(SINK, False, errors=(f"{type(exc).__name__}: {exc}",))
        return SinkResult(SINK, True, (url,))

    # -- gateway loop ---------------------------------------------------------------------------
    def _publisher(self, meeting: Meeting) -> TaskPublisher:
        adapter = self._adapter()
        if adapter is None:
            raise ConnectionError("discord not connected")
        return TaskPublisher(adapter=adapter, views=self._views, settings=self._settings(),
                             repo=self._service().repo, options=self._options(meeting), targets=self._targets,
                             transcript_text=self._transcript_text)

    def _transcript_text(self, meeting: Meeting) -> Optional[str]:
        """The Markdown transcript (``[mm:ss] Name: text``), rendered with the current title (thread)."""
        folder = self._service().folder(meeting)
        utterances = read_transcript(folder)
        if not utterances:
            return None
        return render_transcript_md(meeting, utterances, meeting.language or self._settings().ui_language)

    def _targets(self, meeting: Meeting) -> list[str]:
        """Notes channel candidates, in order. Imported (Google Meet) meetings have no voice chat:
        ``google_meet_discord_channel`` → ``delivery_discord_channel`` → home (DESIGN §17)."""
        adapter = self._adapter()
        home = getattr(getattr(getattr(adapter, "config", None), "home_channel", None), "chat_id", None)
        s = self._settings()
        if meeting.source == SOURCE_GOOGLE_MEET:
            order = (s.google_meet_discord_channel, s.delivery_discord_channel, meeting.text_channel_id, home)
        else:
            order = (s.delivery_discord_channel, meeting.text_channel_id, meeting.channel_id, home)
        out: list[str] = []
        for cid in order:
            if cid and not str(cid).isdigit():
                log.warning("meeting-scribe: ignoring non-numeric Discord channel id %r (run doctor)", cid)
                continue
            if cid and str(cid) not in out:
                out.append(str(cid))
        return out

    def _lock(self, meeting_id: str) -> asyncio.Lock:
        """One publication at a time per meeting (delivery retry, refresh, move): no racing duplicates."""
        lock = self._locks.get(meeting_id)
        if lock is None:
            lock = self._locks[meeting_id] = asyncio.Lock()
        return lock

    async def publish(self, meeting: Meeting, notes: Notes) -> str:
        async with self._lock(meeting.id):
            # the only path that attaches the transcript: the pipeline's DELIVER stage
            return await self._publisher(meeting).publish(meeting, notes, send_dms=True, attach_transcript=True)

    async def _load(self, meeting_id: str) -> Optional[tuple[Meeting, Notes]]:
        svc = self._service()
        meeting = await asyncio.to_thread(svc.repo.get_meeting, meeting_id)
        if meeting is None:
            return None
        notes = await asyncio.to_thread(read_notes, svc.folder(meeting))
        return (meeting, notes) if notes is not None else None

    async def board(self, meeting_id: str) -> Optional[tuple[TaskPublisher, Board]]:
        loaded = await self._load(meeting_id)
        if loaded is None:
            return None
        pub = self._publisher(loaded[0])
        return pub, await pub.board(*loaded)

    async def refresh(self, meeting_id: str) -> None:
        """Re-render everything of a meeting in place (edits; nothing new is DMed)."""
        async with self._lock(meeting_id):
            await self._refresh(meeting_id)

    async def _refresh(self, meeting_id: str) -> None:
        loaded = await self._load(meeting_id)
        if loaded is not None:
            await self._publisher(loaded[0]).publish(*loaded, send_dms=False)

    async def refresh_item(self, meeting_id: str, item_id: str) -> None:
        """After a button action: that task's message, its assignee's DM panel and the index counts."""
        async with self._lock(meeting_id):
            await self._refresh_item(meeting_id, item_id)

    async def _refresh_item(self, meeting_id: str, item_id: str) -> None:
        got = await self.board(meeting_id)
        if got is None:
            return
        pub, board = got
        view = board.view(item_id)
        ptrs = Pointers(pub.repo, meeting_id)
        if view is None or not await pub.edit_task(board.meeting, view, ptrs):
            await self._refresh(meeting_id)
            return
        if is_discord_user_id(view.item.owner_speaker_id) and pub.settings.delivery_dm_assignees:
            await pub.dm(board, view.item.owner_speaker_id, ptrs, send=False)
        await pub.refresh_index(board, ptrs)

    async def task_panel(self, meeting_id: str, user_id: str, scope: str, page: int, *, is_owner: bool) -> Any:
        got = await self.board(meeting_id)
        if got is None:
            raise LookupError(meeting_id)
        pub, board = got
        panel = render_panel(board.meeting, board.views, user_id=user_id, scope=scope, page=page, o=pub.o,
                             is_owner=is_owner)
        return self._views.panel_view(panel)

    async def move_options(self, meeting_id: str, item_id: str) -> list[tuple[str, str]]:
        got = await self.board(meeting_id)
        if got is None:
            raise LookupError(meeting_id)
        return move_options(got[1], item_id, self._settings().channel_name_ignore_prefixes)

    async def move_item(self, meeting_id: str, item_id: str, channel_id: str, *, learn: bool = True) -> str:
        """📁: pin the task to ``channel_id`` (``learn``: owners teach routing), re-post it there, drop the old one."""
        async with self._lock(meeting_id):
            return await self._move_item(meeting_id, item_id, channel_id, learn)

    async def _move_item(self, meeting_id: str, item_id: str, channel_id: str, learn: bool) -> str:
        got = await self.board(meeting_id)
        if got is None:
            raise LookupError(meeting_id)
        _pub, board = got
        chan = next((c for c in board.channels if c.id == str(channel_id) and c.kind != "category"), None)
        if chan is None or not chan.can_post:
            raise LookupError(f"channel {channel_id} is not available")
        name = clean_channel_name(chan.name, self._settings().channel_name_ignore_prefixes) or chan.name
        await asyncio.to_thread(lambda: self._service().move_item(meeting_id, item_id, chan.id, name, learn=learn))
        await self._refresh(meeting_id)
        return f"<#{chan.id}>"
