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
from ..domain.models import KV_MOVE_FROM_DM, Meeting, MeetingState, Notes, SinkResult, is_discord_user_id
from ..domain.names import clean_channel_name
from ..i18n import t
from ..storage.artifacts import read_notes, read_transcript, render_transcript_md
from .board import Board, move_options
from .destination import (REPORT_KV, ROUTES_REPORT_KV, Destination, DestinationPending, Resolved, is_forum, pick_tags,
                          requires_tag, resolve, resolve_routes)
from .guild import viewable_by
from .private_share import ShareReport, share_all, share_dm, share_project, sync_copies, withdraw_public
from .publisher import Pointers, ViewFactory
from .render import MessageSpec, RenderOptions, safe_name
from .render_tasks import render_panel
from .task_publisher import TaskPublisher

log = logging.getLogger(__name__)
SINK = "discord"
UNHEARD_POINTER = "unheard"  # the notice of a meeting discarded with people unheard

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
    def republish(self, meeting: Meeting, notes: Notes, folder: Path) -> SinkResult:
        return self._deliver(meeting, notes, identity=True)

    def deliver(self, meeting: Meeting, notes: Notes, folder: Path) -> SinkResult:
        return self._deliver(meeting, notes, identity=False)

    def _deliver(self, meeting: Meeting, notes: Notes, *, identity: bool) -> SinkResult:
        adapter, loop = self._adapter(), self._loop()
        if adapter is None or loop is None or loop.is_closed():
            return SinkResult(SINK, False, errors=("discord not connected yet; will retry",), deferred=True)
        operation = self.republish_identity(meeting, notes) if identity else self.publish(meeting, notes)
        fut = asyncio.run_coroutine_threadsafe(operation, loop)
        try:
            url = fut.result(self._timeout)
        except DestinationPending as exc:  # nothing to post to yet: wait, never spend attempts
            log.warning("meeting-scribe: meeting %s %s", meeting.id, exc)
            return SinkResult(SINK, False, errors=(str(exc),), deferred=True, waiting=True)
        except Exception as exc:  # timeout, permissions, unknown channel: reported, the job retries
            fut.cancel()
            return SinkResult(SINK, False, errors=(f"{type(exc).__name__}: {exc}",))
        if identity and not url:  # nothing was ever published: nothing edited, and nothing claimed as delivered
            return SinkResult(SINK, True, skipped=("not published yet",))
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
        """Loop-side: notes channel candidates, the server, and the channel for tasks without project.

        A meeting published as private is ANCHORED to its recorded channel (DESIGN §19.2): a rule edited,
        removed or pointing to a renamed channel never moves it. When the rule now points elsewhere (or
        cannot be used) the destination is still the anchor — buttons and refreshes keep working there —
        but ``held`` makes DELIVER wait with the reason until the admin runs ``private-move``."""
        client = getattr(self._adapter(), "_client", None)
        dest = resolve(client, meeting, self._settings(meeting), allowed_guilds=self.allowed_guilds(meeting))
        rec = privacy.record(self._service().repo, meeting.id)
        if rec is None:
            return dest
        anchor = str(rec.get("channel") or "")
        rule = str(rec.get("rule") or dest.rule or "")
        if privacy.is_dm_record(rec):  # anchored to direct messages: no channel, whatever the rules say now
            return Destination(guild=dest.guild, guild_source=dest.guild_source, rule=rule, private=True, dm=True)
        if not anchor:
            if dest.private:
                return dest  # not published yet: the private rule's channel
            held = Destination(guild=dest.guild, guild_source=dest.guild_source, rule=rule, private=True)
            held.steps.append(Resolved("meeting_routes", rule, "missing", detail="the private rule of this meeting "
                                       "was removed before its channel was known"))
            held.problem = ("waiting: this meeting is private and its private channel is unknown; add a private "
                            "rule for it to meeting_routes (it is never published elsewhere)")
            return held
        held = Destination(targets=[anchor], guild=dest.guild, guild_source=dest.guild_source, rule=rule, private=True)
        held.steps.append(Resolved("meeting_routes", rule, "ok", anchor, detail="private meeting anchored to its "
                                   "channel"))
        if dest.rule and (not dest.private or anchor not in dest.targets):
            now = f"<#{dest.targets[0]}>" if dest.targets else "a channel that cannot be used"
            held.held = True
            held.problem = (f"waiting: private meeting {meeting.id} stays in its channel <#{anchor}>, but rule "
                            f"{dest.rule!r} of meeting_routes now points to {now}. Nothing is moved automatically: "
                            f"restore the rule, or move it with `hermes meeting-scribe private-move {meeting.id} "
                            "<channel id>`")
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

    async def republish_identity(self, meeting: Meeting, notes: Notes) -> str:
        async with self._lock(meeting.id):
            return await self._publisher(meeting).publish(meeting, notes, send_dms=False,
                                                           attach_transcript=True, identity_refresh=True)

    async def publish(self, meeting: Meeting, notes: Notes) -> str:
        async with self._lock(meeting.id):
            dest = self.destination(meeting)
            await self._save_report(meeting, dest)
            pub = self._publisher(meeting)
            ptr = await pub.msgs_pointer(meeting)
            private = await pub.is_private(meeting)
            rule = privacy.rule_for(self._settings(meeting), meeting)
            if private and (rule is None or privacy.marks_private(rule)):
                await asyncio.to_thread(privacy.remember, self._service().repo, meeting.id, dest.rule, "",
                                        dm=bool(rule is not None and rule.dm))
            if dest.dm:  # direct messages only (DESIGN §19.3): never a channel, never the paths below
                return await pub.publish(meeting, notes, send_dms=True, attach_transcript=True)
            # a private meeting that waits (anchored and its rule changed, or no usable channel) still leaves
            # every public place; what is inside its private channel stays
            if dest.held or (not dest.targets and (not ptr or private)):
                if ptr and private:
                    place = await pub.private_place(meeting, Pointers(pub.repo, meeting.id))
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

    async def notice_unheard(self, meeting_id: str) -> Optional[str]:
        """A recording discarded as ``empty`` although people were in the call (``missing_audio``):
        post one short notice where its notes would have gone, so it is not lost in silence
        (DESIGN §4.1). Follows the meeting's rules: a private meeting only in its private channel, a
        direct-messages meeting nowhere (no channel is ever used for it), a meeting waiting for a
        destination nowhere; the voice chat is skipped when the stop announcement already said it
        there. Posted once (pointer ``unheard``). Returns the message URL, or ``None``."""
        svc = self._service()
        meeting = await asyncio.to_thread(svc.repo.get_meeting, meeting_id)
        if meeting is None or meeting.state is not MeetingState.EMPTY or not meeting.missing_audio:
            return None
        if not self.enabled(meeting):
            return None
        ptrs = Pointers(svc.repo, meeting.id)
        async with self._lock(meeting.id):
            if await ptrs.load(UNHEARD_POINTER) is not None:
                return None
            dest = self.destination(meeting)
            if dest.dm or dest.held or not dest.targets:
                log.info("meeting-scribe: %s: no channel for the missing-audio notice (%s)", meeting.id,
                         "direct messages only" if dest.dm else dest.problem or "held")
                return None
            pub = self._publisher(meeting)
            try:
                channel = await pub._chat_channel(meeting)
            except (LookupError, DestinationPending) as exc:
                log.info("meeting-scribe: %s: missing-audio notice not posted: %s", meeting.id, exc)
                return None
            settings = self._settings(meeting)
            voice_chat = {str(c) for c in (meeting.text_channel_id, meeting.channel_id) if c}
            if settings.consent_announce and str(getattr(channel, "id", "")) in voice_chat:
                return None  # the stop announcement already said it in this chat
            lang = settings.ui_language
            names = ", ".join(safe_name(n) for n in meeting.missing_audio_names)
            spec = MessageSpec(t("notes.unheard", lang, title=safe_name(meeting.title or meeting.channel_name),
                                 id=meeting.id, names=names))
            if is_forum(channel):
                name = f"{meeting.started_at:%Y-%m-%d} · {' '.join((meeting.title or meeting.channel_name).split())}"
                try:
                    channel, message, _tags = await pub._new_post(channel, name[:100], spec, ())
                except DestinationPending as exc:
                    log.warning("meeting-scribe: %s: missing-audio notice not posted: %s", meeting.id, exc)
                    return None
            else:
                message = await pub.msgs.send(channel, spec=spec)
            url = str(getattr(message, "jump_url", "") or "")
            await ptrs.save(UNHEARD_POINTER, {"channel": channel.id, "message": message.id}, url)
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

    # -- task assignments (DESIGN §16.2) -------------------------------------------------------------
    def announce_now(self, meeting_id: str) -> int:
        """Pipeline thread: show the queued assignments of ``meeting_id`` in Discord (see :meth:`announce`)."""
        loop = self._loop()
        if self._adapter() is None or loop is None or loop.is_closed():
            return 0  # kept queued until Discord is connected
        return asyncio.run_coroutine_threadsafe(self.announce(meeting_id), loop).result(self._timeout)

    async def announce(self, meeting_id: str) -> int:
        """Show every queued assignment of a meeting: its card edited IN PLACE (an edit notifies nobody),
        ONE mention of the new assignee when someone else gave it to them and they were never notified
        of this task, the new assignee's DM panel (posted once, edited afterwards), the panels of whoever
        lost it, and the index counts. Returns how many tasks were shown."""
        from ..pipeline.task_assign import pending_announcements, take_announcements

        async with self._lock(meeting_id):
            pending = await asyncio.to_thread(pending_announcements, self._service().repo, meeting_id)
            if not pending:
                return 0
            got = await self.board(meeting_id)
            if got is None:
                await asyncio.to_thread(take_announcements, self._service().repo, meeting_id, pending)
                return 0
            pub, board = got
            ptrs = Pointers(pub.repo, meeting_id)
            if await pub.is_dm(board.meeting):
                await self._refresh(meeting_id)  # every participant's copy is re-rendered (edits; no pings)
            elif await ptrs.load("notes"):  # never published: its first delivery shows the assignee
                for item_id, rec in pending.items():
                    await self._announce_one(pub, board, ptrs, item_id, rec)
                await pub.refresh_index(board, ptrs)
            await asyncio.to_thread(take_announcements, self._service().repo, meeting_id, pending)
            return len(pending)

    async def _announce_one(self, pub: TaskPublisher, board: Any, ptrs: Pointers, item_id: str, rec: dict) -> None:
        view = board.view(item_id)
        if view is None:
            return
        if not await pub.edit_task(board.meeting, view, ptrs):
            await self._refresh(board.meeting.id)  # never posted there yet: laid out again, pinging nobody
        target = str(rec.get("to") or "")
        if rec.get("ping") and is_discord_user_id(target):
            await pub.ping_assignee(board, view, target, ptrs)
        if board.private:
            await sync_copies(pub, board, ptrs)
            return
        if pub.settings.delivery_dm_assignees:
            if is_discord_user_id(target):  # an existing panel is edited, never duplicated; a mere refresh
                await pub.dm(board, target, ptrs, send=not rec.get("refresh"))  # never posts a new one
            for uid in rec.get("from") or ():
                if is_discord_user_id(uid) and await ptrs.load(f"dm:{uid}"):
                    await pub.dm(board, str(uid), ptrs, send=False)

    # -- private meetings (DESIGN §19.2) ------------------------------------------------------------
    async def private_place(self, meeting_id: str) -> Optional[set[str]]:
        """The channel ids a private meeting lives in (``None``: not private) — button authorization."""
        meeting = await asyncio.to_thread(self._service().repo.get_meeting, meeting_id)
        if meeting is None or not await asyncio.to_thread(self.is_private, meeting):
            return None
        repo = self._service().repo
        place = await asyncio.to_thread(privacy.allowed_places, repo, meeting, self._settings(meeting))
        return place | await self._publisher(meeting).private_place(meeting, Pointers(repo, meeting_id))

    async def dm_recipients(self, meeting_id: str) -> Optional[dict[str, str]]:
        """``{recipient: their DM channel}`` of a direct-messages meeting (``None``: not one) — buttons."""
        meeting = await asyncio.to_thread(self._service().repo.get_meeting, meeting_id)
        if meeting is None or not await asyncio.to_thread(privacy.is_dm, self._service().repo,
                                                          self._settings(meeting), meeting):
            return None
        return await asyncio.to_thread(privacy.dm_copies, self._service().repo, meeting_id)

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

    async def move_options(self, meeting_id: str, item_id: str, *, viewer: str) -> list[tuple[str, str]]:
        """📁 destinations for ``viewer`` (the clicker): only channels they can see; none in a
        direct-messages meeting (it never lives in a channel, DESIGN §19.3)."""
        got = await self.board(meeting_id)
        if got is None:
            raise LookupError(meeting_id)
        pub, board = got
        if await pub.is_dm(board.meeting):
            return []
        mine = self._viewable(pub, board.meeting, viewer)
        options = move_options(board, item_id, self._settings(board.meeting).channel_name_ignore_prefixes)
        return [(cid, name) for cid, name in options if cid in mine]

    @staticmethod
    def _viewable(pub: TaskPublisher, meeting: Meeting, viewer: str) -> set[str]:
        guild = pub._destination(meeting).guild
        return viewable_by(guild, viewer) if guild is not None else set()

    async def _check_forum_move(self, meeting: Meeting, channel_id: str, project: str) -> None:
        """A forum that requires a tag would refuse the task's post: say so BEFORE moving anything."""
        s = self._settings(meeting)
        forum = await self._adapter()._resolve_channel(channel_id)
        if is_forum(forum) and requires_tag(forum) and not pick_tags(forum, (project, *s.delivery_forum_tags),
                                                                      s.delivery_forum_default_tag):
            raise ForumTagRequired(f"forum {channel_id} requires a tag and none matches {project!r}")

    async def move_item(self, meeting_id: str, item_id: str, channel_id: str, *, viewer: str,
                        learn: bool = True) -> str:
        """📁: pin the task to ``channel_id`` (``learn``: owners teach routing), re-post it there, drop the
        old one. ``viewer`` (who asked) must be able to see that channel; a direct-messages meeting never
        moves a task into a channel."""
        async with self._lock(meeting_id):
            return await self._move_item(meeting_id, item_id, channel_id, viewer, learn)

    async def _move_item(self, meeting_id: str, item_id: str, channel_id: str, viewer: str, learn: bool) -> str:
        got = await self.board(meeting_id)
        if got is None:
            raise LookupError(meeting_id)
        pub, board = got
        if await pub.is_dm(board.meeting):
            raise ChannelUnavailable(f"meeting {meeting_id} goes by direct message only: its tasks are not moved")
        chan = next((c for c in board.channels if c.id == str(channel_id) and c.kind != "category"), None)
        if chan is None or not chan.can_post or chan.id not in self._viewable(pub, board.meeting, viewer):
            raise ChannelUnavailable(f"channel {channel_id} is not available")
        name = clean_channel_name(chan.name, self._settings(board.meeting).channel_name_ignore_prefixes) or chan.name
        if chan.kind == "forum":
            await self._check_forum_move(board.meeting, chan.id, name)
        await asyncio.to_thread(lambda: self._service().move_item(meeting_id, item_id, chan.id, name, learn=learn))
        await self._refresh(meeting_id)
        return f"<#{chan.id}>"
