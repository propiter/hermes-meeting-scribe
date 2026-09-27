"""Publishing a meeting's tasks on the gateway loop (DESIGN §16).

Layout per delivery:

* meeting chat: the summary (``notes`` pointer, as in 0.1) and, last, the task index with the one
  "📋 My tasks" button (``index`` pointer). Tasks without a project channel go to the meeting chat
  too (into a thread under the summary when ``delivery_discord_thread`` and the channel allows it).
* each project channel: an anchor message + a thread (``thread:<channel>`` pointer; directly in the
  channel when ``delivery_project_threads`` is off or the thread cannot be created), then ONE
  message per task with its own buttons (``task:<item>`` pointer holding the routed ``target``).
* assignee DMs (``delivery_dm_assignees``, default on): one panel per assignee (``dm:<user>``).
  A closed DM (50007) is logged and listed in the index; it never fails the delivery.

Every pointer is saved right after its message exists, so a retry after a partial failure edits
instead of duplicating (review W8). A task whose route changed (📁 move, new learned mapping) is
re-posted in its new place and the old message deleted.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional, Sequence

from ..config import Settings
from ..domain.models import Meeting, Notes
from ..i18n import t
from .board import Board, build_board
from .guild import guild_of, snapshot_channels
from .publisher import Messages, Pointers, ViewFactory
from .render import MessageSpec, RenderOptions, render_header
from .render_tasks import TaskView, render_index, render_panel, render_task

log = logging.getLogger(__name__)


class TaskPublisher:
    def __init__(self, *, adapter: Any, views: ViewFactory, settings: Settings, repo: Any,
                 options: RenderOptions, targets: Callable[[Meeting], Sequence[str]]) -> None:
        self.msgs = Messages(adapter, views)
        self.adapter = adapter
        self.settings = settings
        self.repo = repo
        self.o = options
        self._targets = targets

    # -- board ----------------------------------------------------------------------------------
    async def board(self, meeting: Meeting, notes: Notes) -> Board:
        guild = guild_of(self.adapter, meeting)
        channels = snapshot_channels(guild, need_threads=self.settings.delivery_project_threads) if guild else []
        return await asyncio.to_thread(build_board, self.repo, self.settings, meeting, notes, channels)

    # -- meeting chat ---------------------------------------------------------------------------
    async def _chat_channel(self, meeting: Meeting) -> Any:
        for cid in self._targets(meeting):
            try:
                return await self.msgs.channel(cid)
            except Exception as exc:  # deleted channel, missing access: try the next fallback
                log.info("meeting-scribe: notes channel %s unavailable: %s", cid, exc)
        raise LookupError("no reachable Discord channel for notes (delivery_discord_channel / voice chat / home)")

    async def header(self, meeting: Meeting, notes: Notes, ptrs: Pointers) -> tuple[Any, dict]:
        """Post/edit the summary parts; returns the chat channel and the ``notes`` pointer."""
        specs = render_header(meeting, notes, self.o.lang)
        ptr = await ptrs.load("notes")
        channel = None
        if ptr and ptr.get("messages"):
            try:
                channel = await self.msgs.channel(ptr["channel"])
                await self.msgs.edit(channel, ptr["messages"][0], spec=specs[0])
            except Exception as exc:  # gone: post a fresh copy
                log.info("meeting-scribe: stored notes message unavailable (%s); re-posting", exc)
                channel, ptr = None, None
        if channel is None or ptr is None:
            channel = await self._chat_channel(meeting)
            first = await self.msgs.send(channel, spec=specs[0])
            ptr = {"channel": channel.id, "thread": None, "messages": [first.id], "url": first.jump_url}
            await ptrs.save("notes", ptr, first.jump_url)
        ids: list[int] = list(ptr["messages"])
        for i, spec in enumerate(specs[1:], start=1):
            if i < len(ids):
                try:
                    await self.msgs.edit(channel, ids[i], spec=spec)
                    continue
                except Exception as exc:
                    log.info("meeting-scribe: notes part %s gone (%s); sending a new one", ids[i], exc)
            msg = await self.msgs.send(channel, spec=spec)
            ids[i:i + 1] = [msg.id]
            await ptrs.save("notes", {**ptr, "messages": ids}, ptr.get("url", ""))
        for surplus in ids[len(specs):]:  # 0.1 notes also held the task messages: drop them
            await self.msgs.delete(channel.id, surplus)
        ptr = {**ptr, "messages": ids[:len(specs)]}
        await ptrs.save("notes", ptr, ptr.get("url", ""))
        return channel, ptr

    async def chat_task_target(self, channel: Any, ptr: dict, ptrs: Pointers, meeting: Meeting) -> Any:
        if ptr.get("thread"):
            try:
                return await self.msgs.channel(ptr["thread"])
            except Exception as exc:
                log.info("meeting-scribe: notes thread gone (%s)", exc)
        if self.settings.delivery_discord_thread:
            try:
                first = channel.get_partial_message(int(ptr["messages"][0]))
                thread = await first.create_thread(name=self._thread_name(meeting), auto_archive_duration=1440)
                await ptrs.save("notes", {**ptr, "thread": thread.id}, ptr.get("url", ""))
                ptr["thread"] = thread.id
                return thread
            except Exception as exc:  # voice text chats / missing Create Public Threads: stay in channel
                log.info("meeting-scribe: no thread for meeting-chat tasks (%s)", exc)
        return channel

    def _thread_name(self, meeting: Meeting) -> str:
        title = (meeting.title or meeting.channel_name or "meeting")[:80]
        return t("tasks.thread_name", self.o.lang, title=title, date=f"{meeting.started_at:%Y-%m-%d}")[:100]

    # -- project channels -----------------------------------------------------------------------
    async def project_target(self, channel_id: str, meeting: Meeting, notes: Notes, count: int,
                             ptrs: Pointers) -> Any:
        channel = await self.msgs.channel(channel_id)
        title = notes.meeting_title or meeting.title or meeting.channel_name
        spec = MessageSpec(f"🎙️ **{title}** · {meeting.started_at:%Y-%m-%d} · `{meeting.id}` — "
                           f"📋 {t('tasks.index_title', self.o.lang)}: {count}")
        suffix = f"thread:{channel_id}"
        ptr = await ptrs.load(suffix) or {}
        placed = await self.msgs.edit_or_send(ptr, channel, spec=spec)
        ptr = {**ptr, **placed}
        await ptrs.save(suffix, ptr)
        if not self.settings.delivery_project_threads:
            return channel
        if ptr.get("thread"):
            try:
                return await self.msgs.channel(ptr["thread"])
            except Exception as exc:
                log.info("meeting-scribe: project thread gone (%s); starting a new one", exc)
        try:
            anchor = channel.get_partial_message(int(ptr["message"]))
            thread = await anchor.create_thread(name=self._thread_name(meeting), auto_archive_duration=1440)
        except Exception as exc:  # permissions changed since the snapshot: post in the channel itself
            log.info("meeting-scribe: cannot start a thread in %s (%s); posting in the channel", channel_id, exc)
            return channel
        await ptrs.save(suffix, {**ptr, "thread": thread.id})
        return thread

    # -- tasks ----------------------------------------------------------------------------------
    async def place_task(self, meeting: Meeting, view: TaskView, target: Any, ptrs: Pointers) -> dict:
        suffix = f"task:{view.item.id}"
        ptr = await ptrs.load(suffix)
        placed = await self.msgs.edit_or_send(ptr, target, spec=render_task(meeting, view, self.o))
        new = {**placed, "target": view.route.channel_id or ""}
        await ptrs.save(suffix, new, placed.get("url", ""))
        return new

    async def edit_task(self, meeting: Meeting, view: TaskView, ptrs: Pointers) -> bool:
        ptr = await ptrs.load(f"task:{view.item.id}")
        if not ptr or (ptr.get("target") or None) != view.route.channel_id:
            return False  # never posted or it must move: the caller re-publishes
        try:
            channel = await self.msgs.channel(ptr["channel"])
            await self.msgs.edit(channel, ptr["message"], spec=render_task(meeting, view, self.o))
            return True
        except Exception as exc:
            log.info("meeting-scribe: task message %s unavailable (%s)", ptr.get("message"), exc)
            return False

    # -- DMs ------------------------------------------------------------------------------------
    async def _user(self, uid: str) -> Any:
        client = getattr(self.adapter, "_client", None)
        user = client.get_user(int(uid)) if hasattr(client, "get_user") else None
        return user or await client.fetch_user(int(uid))

    async def dm(self, board: Board, uid: str, ptrs: Pointers, *, send: bool) -> bool:
        """Post (``send``) or refresh the assignee's DM panel; ``False`` when the DM could not be sent."""
        panel = render_panel(board.meeting, board.views, user_id=uid, scope="m", page=0, o=self.o, is_owner=False)
        ptr = await ptrs.load(f"dm:{uid}")
        try:
            user = await self._user(uid)
            if ptr:
                try:
                    await self.msgs.edit(await user.create_dm(), ptr["message"], panel=panel)
                    return True
                except Exception as exc:
                    log.info("meeting-scribe: DM panel of %s gone (%s)", uid, exc)
                    if not send:
                        return True
            if not send:
                return True
            msg = await self.msgs.send(None, panel=panel, dm_user=user)
        except Exception as exc:  # DMs closed (50007), unknown user, rate limit: soft
            log.info("meeting-scribe: DM to %s failed: %s", uid, exc)
            return False
        await ptrs.save(f"dm:{uid}", {"channel": msg.channel.id, "message": msg.id})
        return True

    # -- index ----------------------------------------------------------------------------------
    async def index(self, board: Board, chat: Any, threads: dict, dm_failed: Sequence[str], ptrs: Pointers) -> None:
        spec = render_index(board.meeting, board.views, threads, dm_failed, self.o)
        ptr = await ptrs.load("index")
        placed = await self.msgs.edit_or_send(ptr, chat, spec=spec)
        await ptrs.save("index", {**placed, "threads": {str(k): v for k, v in threads.items()},
                                  "dm_failed": list(dm_failed)})

    async def refresh_index(self, board: Board, ptrs: Pointers) -> None:
        ptr = await ptrs.load("index")
        if not ptr:
            return
        threads = {(None if k == "None" else k): v for k, v in (ptr.get("threads") or {}).items()}
        chat = await self.msgs.channel(ptr["channel"])
        await self.msgs.edit(chat, ptr["message"],
                             spec=render_index(board.meeting, board.views, threads, ptr.get("dm_failed") or (), self.o))

    async def _drop_stale_tasks(self, board: Board, ptrs: Pointers) -> None:
        """A reprocess that no longer finds a task deletes its message (and its pointer)."""
        alive = {v.item.id for v in board.views}
        for item_id, ptr in (await ptrs.with_prefix("task:")).items():
            if item_id not in alive:
                await self.msgs.delete(ptr.get("channel"), ptr.get("message"))
                await ptrs.drop(f"task:{item_id}")

    # -- whole meeting --------------------------------------------------------------------------
    async def publish(self, meeting: Meeting, notes: Notes, *, send_dms: bool) -> str:
        ptrs = Pointers(self.repo, meeting.id)
        chat, notes_ptr = await self.header(meeting, notes, ptrs)
        board = await self.board(meeting, notes)
        groups: dict[Optional[str], list[TaskView]] = {}
        for view in board.views:
            groups.setdefault(view.route.channel_id, []).append(view)
        threads: dict[Optional[str], Any] = {}
        for cid, views in groups.items():
            target = None
            if cid is not None:
                try:
                    target = await self.project_target(cid, meeting, notes, len(views), ptrs)
                except Exception as exc:  # channel vanished since the snapshot: meeting chat
                    log.info("meeting-scribe: project channel %s unavailable (%s)", cid, exc)
            if target is None:
                target = await self.chat_task_target(chat, notes_ptr, ptrs, meeting)
            threads[cid] = str(target.id)
            for view in views:
                await self.place_task(meeting, view, target, ptrs)
        await self._drop_stale_tasks(board, ptrs)
        dm_failed: list[str] = []
        if self.settings.delivery_dm_assignees:
            for uid in dict.fromkeys(v.item.owner_speaker_id for v in board.views if v.item.owner_speaker_id):
                if not await self.dm(board, str(uid), ptrs, send=send_dms):
                    dm_failed.append(str(uid))
        await self.index(board, chat, threads, dm_failed, ptrs)
        return str(notes_ptr.get("url") or "")
