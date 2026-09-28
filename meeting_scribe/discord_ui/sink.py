"""Discord notes sink (DESIGN §8, §16; satisfies :class:`meeting_scribe.sinks.base.DiscordNotesSink`).

``deliver`` runs on the pipeline thread and hands the publishing coroutine to the gateway loop
(``run_coroutine_threadsafe``) with a timeout. The layout (summary + task index in the meeting chat,
one message per task in its project's channel thread, assignee DMs) is built by
:class:`~meeting_scribe.discord_ui.task_publisher.TaskPublisher`; every message has a pointer in
``deliveries`` so reprocesses and button clicks edit in place.

Loop-side entry points used by the buttons: :meth:`refresh_item` (after an action: that task's
message, its assignee's DM and the index counts), :meth:`task_panel` (📋 My tasks), :meth:`move_options`
and :meth:`move_item` (📁); for private meetings (DESIGN §19.2) :meth:`private_place`, :meth:`share`
and :meth:`share_all`.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from .. import privacy
from ..config import Settings
from ..domain.errors import ChannelUnavailable, ForumTagRequired, NotPrivate
from ..domain.models import KV_MOVE_FROM_DM, Meeting, Notes, SinkResult, is_discord_user_id
from ..domain.names import clean_channel_name
from ..storage.artifacts import read_notes, read_transcript, render_transcript_md
from .board import Board, move_options
from .destination import (REPORT_KV, ROUTES_REPORT_KV, Destination, DestinationPending, Resolved, is_forum, pick_tags,
                          requires_tag, resolve, resolve_routes)
from .private_share import ShareReport, share_all, share_dm, share_project, sync_copies, withdraw_public
from .publisher import Pointers, ViewFactory
from .render import RenderOptions
from .render_tasks import render_panel
from .task_publisher import TaskPublisher

log = logging.getLogger(__name__)
SINK = "discord"

__all__ = ["DiscordNotesSink", "ViewFactory", "SINK", "DestinationPending"]


class DiscordNotesSink:
    name = SINK

    def __init__(self, *, settings: Callable[..., Settings], service: Callable[[], Any], adapter: Callable[[], Any],
                 loop: Callable[[], Optional[asyncio.AbstractEventLoop]], options: Callable[[Meeting], RenderOptions],
                 views: ViewFactory, timeout: float = 120.0,
                 space_guilds: Optional[Callable[[str], Optional[frozenset[str]]]] = None) -> None:
        """``settings(space)``: the meeting space's settings. ``space_guilds(space)``: the servers a
        meeting of ``space`` may be published to (``None``: no restriction — an install with one
        space); a server of another team is never a destination (DESIGN §23)."""
        self._space_settings = settings
        self._space_guilds = space_guilds
        self._service = service
        self._adapter = adapter
        self._loop = loop
        self._options = options
        self._views = views
        self._timeout = timeout
        self._locks: dict[str, asyncio.Lock] = {}

    def _settings(self, meeting: Optional[Meeting] = None) -> Settings:
        space = getattr(meeting, "space", "") if meeting is not None else ""
        return self._space_settings(space) if space else self._space_settings()

    def enabled(self, meeting: Meeting) -> bool:
        return self._settings(meeting).delivery_discord_enabled

    def allowed_guilds(self, meeting: Meeting) -> Optional[frozenset[str]]:
        if self._space_guilds is None or not meeting.space:
            return None
        return self._space_guilds(meeting.space)

    # -- pipeline thread -----------------------------------------------------------------------
    def deliver(self, meeting: Meeting, notes: Notes, folder: Path) -> SinkResult:
        adapter, loop = self._adapter(), self._loop()
        if adapter is None or loop is None or loop.is_closed():
            return SinkResult(SINK, False, errors=("discord not connected yet; will retry",), deferred=True)
        fut = asyncio.run_coroutine_threadsafe(self.publish(meeting, notes), loop)
        try:
            url = fut.result(self._timeout)
        except DestinationPending as exc:  # nothing to post to yet: wait, never spend attempts
            log.warning("meeting-scribe: meeting %s %s", meeting.id, exc)
            return SinkResult(SINK, False, errors=(str(exc),), deferred=True, waiting=True)
        except Exception as exc:  # timeout, permissions, unknown channel: reported, the job retries
            fut.cancel()
            return SinkResult(SINK, False, errors=(f"{type(exc).__name__}: {exc}",))
        return SinkResult(SINK, True, (url,))

    # -- gateway loop ---------------------------------------------------------------------------
    def _publisher(self, meeting: Meeting) -> TaskPublisher:
        adapter = self._adapter()
        if adapter is None:
            raise ConnectionError("discord not connected")
        return TaskPublisher(adapter=adapter, views=self._views, settings=self._settings(meeting),
                             repo=self._service().repo, options=self._options(meeting), destination=self.destination,
                             transcript_text=self._transcript_text, private=self.is_private)

    def is_private(self, meeting: Meeting) -> bool:
        """A private rule matches it, or it was published as private (sticky, DESIGN §19.2)."""
        return privacy.is_private(self._service().repo, self._settings(meeting), meeting)

    def _transcript_text(self, meeting: Meeting) -> Optional[str]:
        """The Markdown transcript (``[mm:ss] Name: text``), rendered with the current title (thread)."""
        folder = self._service().folder(meeting)
        utterances = read_transcript(folder)
        if not utterances:
            return None
        return render_transcript_md(meeting, utterances, meeting.language or self._settings(meeting).ui_language)

    def destination(self, meeting: Meeting) -> Destination:
        """Loop-side: notes channel candidates, the server, and the channel for tasks without project."""
        client = getattr(self._adapter(), "_client", None)
        dest = resolve(client, meeting, self._settings(meeting), allowed_guilds=self.allowed_guilds(meeting))
        rec = privacy.record(self._service().repo, meeting.id)
        if rec is None or dest.private:
            return dest
        # published as private but no private rule matches any more: only its private channel, never
        # the channels a normal meeting would use (fail closed)
        channel = str(rec.get("channel") or "")
        held = Destination(targets=[channel] if channel else [], guild=dest.guild, guild_source=dest.guild_source,
                           rule=str(rec.get("rule") or ""), private=True)
        held.steps.append(Resolved("meeting_routes", str(rec.get("rule") or ""), "ok" if channel else "missing",
                                   channel or None, detail="" if channel else "the private rule of this meeting "
                                   "was removed before its channel was known"))
        if not channel:
            held.problem = ("waiting: this meeting is private and its private channel is unknown; add a private "
                            "rule for it to meeting_routes (it is never published elsewhere)")
        return held

    def guild_for(self, meeting: Meeting) -> object:
        """Loop-side: the server whose channels are the meeting's project candidates."""
        return self.destination(meeting).guild

    async def _save_report(self, meeting: Meeting, dest: Destination) -> None:
        """Last resolution per source, and every ``meeting_routes`` rule of the meeting's space, for
        ``doctor`` / ``config list`` in other processes. A meeting decided by a rule does not overwrite
        the source's report (that one describes the plain settings)."""
        try:
            import json

            repo = self._service().repo
            if not dest.rule:
                report = {**dest.report(), "meeting": meeting.id}
                await asyncio.to_thread(repo.kv_set, f"{REPORT_KV}.{meeting.source}", json.dumps(report))
            client = getattr(self._adapter(), "_client", None)
            routes = resolve_routes(client, self._settings(meeting), self.allowed_guilds(meeting))
            await asyncio.to_thread(repo.kv_set, ROUTES_REPORT_KV + (meeting.space or ""),
                                    json.dumps(routes) if routes else None)
        except Exception as exc:  # diagnostics only
            log.debug("meeting-scribe: could not store the destination report: %s", exc)

    def _lock(self, meeting_id: str) -> asyncio.Lock:
        """One publication at a time per meeting (delivery retry, refresh, move): no racing duplicates."""
        lock = self._locks.get(meeting_id)
        if lock is None:
            lock = self._locks[meeting_id] = asyncio.Lock()
        return lock

    async def publish(self, meeting: Meeting, notes: Notes) -> str:
        async with self._lock(meeting.id):
            dest = self.destination(meeting)
            await self._save_report(meeting, dest)
            pub = self._publisher(meeting)
            ptr = await pub.msgs_pointer(meeting)
            private = await pub.is_private(meeting)
            if private:
                await asyncio.to_thread(privacy.remember, self._service().repo, meeting.id, dest.rule, "")
            if not dest.targets and (not ptr or private):  # a private meeting never stays outside its channel
                if ptr:  # remove what is outside its known private channel; what is inside stays
                    place = await asyncio.to_thread(privacy.allowed_places, pub.repo, meeting, self._settings(meeting))
                    await withdraw_public(pub, Pointers(pub.repo, meeting.id), place)
                raise DestinationPending(dest.problem)
            # the only path that attaches the transcript (and may move notes out of a DM): DELIVER
            move = await asyncio.to_thread(self._service().repo.kv_get, KV_MOVE_FROM_DM + meeting.id)
            url = await self._publisher(meeting).publish(meeting, notes, send_dms=True, attach_transcript=True,
                                                         move_from_dm=bool(move))
            if move:  # done (moved, refused or nothing to move): a later delivery never moves by itself
                await asyncio.to_thread(self._service().repo.kv_set, KV_MOVE_FROM_DM + meeting.id, None)
            if private:
                await self._remember_channel(meeting)
            return url

    async def _remember_channel(self, meeting: Meeting) -> None:
        ptr = await Pointers(self._service().repo, meeting.id).load("notes") or {}
        channel = str(ptr.get("forum") or ptr.get("channel") or "")
        await asyncio.to_thread(privacy.remember, self._service().repo, meeting.id, "", channel)

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
        if board.private:
            await sync_copies(pub, board, ptrs)
        elif is_discord_user_id(view.item.owner_speaker_id) and pub.settings.delivery_dm_assignees:
            await pub.dm(board, view.item.owner_speaker_id, ptrs, send=False)
        await pub.refresh_index(board, ptrs)

    # -- private meetings (DESIGN §19.2) ------------------------------------------------------------
    async def private_place(self, meeting_id: str) -> Optional[set[str]]:
        """The channel ids a private meeting lives in (``None``: not private) — button authorization."""
        meeting = await asyncio.to_thread(self._service().repo.get_meeting, meeting_id)
        if meeting is None or not await asyncio.to_thread(self.is_private, meeting):
            return None
        repo = self._service().repo
        place = await asyncio.to_thread(privacy.allowed_places, repo, meeting, self._settings(meeting))
        return place | await self._publisher(meeting).private_place(meeting, Pointers(repo, meeting_id))

    async def _private_board(self, meeting_id: str) -> tuple[TaskPublisher, Board]:
        got = await self.board(meeting_id)
        if got is None:
            raise LookupError(meeting_id)
        if not got[1].private:
            raise NotPrivate(f"meeting {meeting_id} is not private")
        return got

    async def share(self, meeting_id: str, item_id: str, how: str) -> str:
        """``how``: ``dm`` (to its assignee) or ``project`` (its project channel). Returns ``dm`` / the
        channel id / ``""`` when it had already been done."""
        async with self._lock(meeting_id):
            pub, board = await self._private_board(meeting_id)
            ptrs = Pointers(pub.repo, meeting_id)
            if how == "dm":
                done = "dm" if await share_dm(pub, board, item_id, ptrs) else ""
            else:
                view = board.view(item_id)
                already = view is not None and view.sharing is not None and view.sharing.channel == view.sharing.target
                channel = await share_project(pub, board, item_id, ptrs)
                done = "" if already else channel
            await self._refresh_item(meeting_id, item_id)
            return done

    async def share_all(self, meeting_id: str) -> ShareReport:
        async with self._lock(meeting_id):
            pub, board = await self._private_board(meeting_id)
            report = await share_all(pub, board, Pointers(pub.repo, meeting_id))
            await self._refresh(meeting_id)
            return report

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
        return move_options(got[1], item_id, self._settings(got[1].meeting).channel_name_ignore_prefixes)

    async def _check_forum_move(self, meeting: Meeting, channel_id: str, project: str) -> None:
        """A forum that requires a tag would refuse the task's post: say so BEFORE moving anything."""
        s = self._settings(meeting)
        forum = await self._adapter()._resolve_channel(channel_id)
        if is_forum(forum) and requires_tag(forum) and not pick_tags(forum, (project, *s.delivery_forum_tags),
                                                                      s.delivery_forum_default_tag):
            raise ForumTagRequired(f"forum {channel_id} requires a tag and none matches {project!r}")

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
            raise ChannelUnavailable(f"channel {channel_id} is not available")
        name = clean_channel_name(chan.name, self._settings(board.meeting).channel_name_ignore_prefixes) or chan.name
        if chan.kind == "forum":
            await self._check_forum_move(board.meeting, chan.id, name)
        await asyncio.to_thread(lambda: self._service().move_item(meeting_id, item_id, chan.id, name, learn=learn))
        await self._refresh(meeting_id)
        return f"<#{chan.id}>"
