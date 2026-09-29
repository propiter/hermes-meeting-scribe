"""Publishing a meeting's tasks on the gateway loop (DESIGN §16).

Layout per delivery:

* meeting chat: the summary (``notes`` pointer, as in 0.1) and, last, the task index with the one
  "📋 My tasks" button (``index`` pointer). Tasks without a project channel go to the meeting chat
  too (into a thread under the summary when ``delivery_discord_thread`` and the channel allows it).
* each project channel: an anchor message + a thread (``thread:<channel>`` pointer; directly in the
  channel when ``delivery_project_threads`` is off or the thread cannot be created), then ONE
  message per task with its own buttons (``task:<item>`` pointer holding the routed ``target``).
* assignee DMs (``delivery_dm_assignees``, default on): one panel per assignee (``dm:<user>``),
  with a link to the meeting's notes. A closed DM (50007) is logged and listed in the index; it
  never fails the delivery.

Forum/media channels (DESIGN §19.1): the notes channel is ONE post per meeting named
``<date> · <title>`` whose first message is the summary; its other parts, the transcript, the tasks
without project and the index go inside the post. A project channel that is a forum gets one post
per meeting too (instead of anchor + thread). Pointers keep the post id (``thread``/``channel``),
its first message and the applied tags; a deleted post is created again, a post whose first
message was deleted keeps being used. A forum that requires a tag and refuses the post leaves the
delivery waiting (:class:`DestinationPending`) — never another channel, never a DM.

PRIVATE meetings (DESIGN §19.2, :mod:`private_share`): the summary, the transcript, EVERY task and the
index stay in the private notes channel; no project channel, no fallback channel, no assignee DM. Each
task shows share buttons; what was shared is kept current by :func:`private_share.sync_copies`.

Every pointer is saved right after its message exists, so a retry after a partial failure edits
instead of duplicating (review W8). A task whose route changed (📁 move, new learned mapping) is
re-posted in its new place and the old message deleted.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional, Sequence

from .. import privacy
from ..config import Settings
from ..domain.models import KV_DM_NOTES, KV_MOVE_FROM_DM, Meeting, Notes, is_discord_user_id
from ..i18n import t
from .board import Board, build_board
from .dm_delivery import DmDelivery
from .destination import (MAX_FORUM_TAGS, Destination, DestinationPending, channel_key, explicit_channel, is_forum,
                          pick_tags, same_guild, tag_names, tag_rejected)
from .guild import snapshot_channels
from .private_share import sync_copies, with_sharing, withdraw_public
from .publisher import Messages, Pointers, ViewFactory, is_missing
from .mentions import may_mention, participants_line
from .render import MessageSpec, RenderOptions, render_header
from .render_tasks import TaskView, render_index, render_panel, render_task
from .transcript_file import SUFFIX as TRANSCRIPT_SUFFIX, mark_legacy, publish_transcript

log = logging.getLogger(__name__)
POST_NAME_LIMIT = 100  # Discord caps thread (forum post) names at 100 characters
MOVE_SUFFIX = "dm_move"  # a move out of a DM in progress: {from, channel, key, attach, old: {suffix: ptr}}
LEFTOVER_SUFFIX = "dm_leftover"  # DM messages a finished move kept (no confirmed replacement): {from, old, why}


class DmMoveError(RuntimeError):
    """The server channel a DM meeting should move to cannot be used; nothing in the DM was touched."""


def transcript_intent(notes_ptr: Optional[dict], transcript_ptr: Optional[dict]) -> bool:
    """Does the full transcript belong with this summary? An explicit ``attach`` in the summary
    pointer wins. Pointers written before that key existed decide from the transcript pointer: one
    really delivered (``done`` with its messages) means yes; the legacy marker, or none, means no."""
    if notes_ptr and "attach" in notes_ptr:
        return bool(notes_ptr["attach"])
    tp = transcript_ptr or {}
    if tp.get("skipped") == "legacy":
        return False
    return bool(tp.get("done") and tp.get("messages"))


def move_intent(move: dict) -> bool:
    """The transcript intent of a move, re-derived from the DM pointers it carries: a move saved by
    an earlier build recorded ``attach: false`` for summaries that predate the key."""
    old = move.get("old") or {}
    if "notes" in old:
        return transcript_intent(old["notes"], old.get(TRANSCRIPT_SUFFIX))
    return bool(move.get("attach"))


def _message_ids(p: dict) -> list:
    return list(p.get("messages") or ()) + ([p["message"]] if p.get("message") else [])


class TaskPublisher:
    def __init__(self, *, adapter: Any, views: ViewFactory, settings: Settings, repo: Any,
                 options: RenderOptions, destination: Callable[[Meeting], Destination],
                 transcript_text: Optional[Callable[[Meeting], Optional[str]]] = None,
                 private: Callable[[Meeting], bool] = lambda _m: False) -> None:
        """``private(meeting)``: the meeting is private (DESIGN §19.2); called in a worker thread."""
        self.msgs = Messages(adapter, views)
        self._private = private
        self.views = views
        self._transcript_text = transcript_text
        self.adapter = adapter
        self.settings = settings
        self.repo = repo
        self.o = options
        self._destination = destination

    # -- board ----------------------------------------------------------------------------------
    async def msgs_pointer(self, meeting: Meeting) -> Optional[dict]:
        """The stored summary pointer (the meeting was already posted), if any."""
        return await Pointers(self.repo, meeting.id).load("notes")

    async def is_private(self, meeting: Meeting) -> bool:
        return bool(await asyncio.to_thread(self._private, meeting))

    async def board(self, meeting: Meeting, notes: Notes) -> Board:
        guild = self._destination(meeting).guild
        channels = snapshot_channels(guild, need_threads=self.settings.delivery_project_threads) if guild else []
        board = await asyncio.to_thread(build_board, self.repo, self.settings, meeting, notes, channels)
        if not await self.is_private(meeting):
            return board
        ptrs = Pointers(self.repo, meeting.id)
        # a direct-messages meeting never sends a task to someone: every participant already has it all
        dm_on = self.settings.delivery_dm_assignees and not await self.is_dm(meeting)
        return await with_sharing(board, ptrs, dm_on=dm_on, notes_place=await self.private_place(meeting, ptrs))

    async def is_dm(self, meeting: Meeting) -> bool:
        """Delivered by direct message only (DESIGN §19.3)."""
        return bool(await asyncio.to_thread(privacy.is_dm, self.repo, self.settings, meeting))

    async def private_place(self, meeting: Meeting, ptrs: Pointers) -> set[str]:
        """The private notes channel of a meeting: its anchored channel (recorded once published, never
        moved by a rule edit), else the rule's channel(s), and — once posted — the notes channel, its forum
        post and the thread holding its tasks. Withdrawing public copies never touches this set."""
        rec = await asyncio.to_thread(privacy.record, self.repo, meeting.id) or {}
        place = {str(rec["channel"])} if rec.get("channel") else {str(c) for c in self._destination(meeting).targets}
        ptr = await ptrs.load("notes") or {}
        if {str(ptr.get(k)) for k in ("channel", "forum") if ptr.get(k)} & place:
            place |= {str(ptr[k]) for k in ("channel", "forum", "thread") if ptr.get(k)}
        return place

    @staticmethod
    def task_target(view: TaskView, private: bool) -> str:
        """What a ``task:<item>`` pointer records as its place: the project channel, or ``private``."""
        return "private" if private else (view.route.channel_id or "")

    # -- meeting chat ---------------------------------------------------------------------------
    async def _chat_channel(self, meeting: Meeting) -> Any:
        dest = self._destination(meeting)
        for cid in dest.targets:
            try:
                channel = await self.msgs.channel(cid)
            except Exception as exc:  # deleted channel, missing access: try the next fallback
                log.info("meeting-scribe: notes channel %s unavailable: %s", cid, exc)
                continue
            if not self._in_server(channel, dest):
                continue
            return channel
        if dest.rule:  # a meeting_routes rule: its channel or nothing — wait with the reason (DESIGN §19.2)
            raise DestinationPending(f"waiting for the Discord channel of rule {dest.rule!r} in meeting_routes: "
                                     "the channel is not reachable (deleted, or the bot cannot see it). "
                                     "The meeting is never published elsewhere")
        raise LookupError("no reachable Discord channel for notes "
                          f"({dest.problem or 'every candidate channel is unavailable'})")

    @staticmethod
    def _in_server(channel: Any, dest: Destination) -> bool:
        """Never a DM (nobody else would see it) and never another server than the meeting's (review M1)."""
        guild = getattr(channel, "guild", None)
        if guild is None:
            log.warning("meeting-scribe: channel %s is not in a server; skipped", getattr(channel, "id", "?"))
            return False
        if dest.guild is not None and not same_guild(guild, dest.guild):
            log.warning("meeting-scribe: channel %s is in server %s, not %s; skipped", getattr(channel, "id", "?"),
                        getattr(guild, "id", "?"), getattr(dest.guild, "id", "?"))
            return False
        return True

    async def header(self, meeting: Meeting, notes: Notes, ptrs: Pointers, *, chat: Any = None,
                     attach: Optional[bool] = None) -> tuple[Any, dict]:
        """Post/edit the summary parts; returns the chat channel and the ``notes`` pointer. ``chat`` forces
        where a NEW summary goes and ``attach`` its transcript intent (a move out of a DM keeps both)."""
        ptr = await ptrs.load("notes")
        mention = bool(getattr(self.settings, "delivery_mention_participants", False))
        line = str((ptr or {}).get("people") or "") if mention else ""
        specs = render_header(meeting, notes, self.o.lang, line, ())  # the published line never pings again
        channel = None
        if ptr and ptr.get("messages") and ptr.get("forum"):
            channel, first_id = await self._open_post(ptr["channel"], ptr["messages"][0], specs[0])
            if channel is not None:
                ptr = {**ptr, "messages": [first_id, *ptr["messages"][1:]]}
                ptr = await self._sync_post(channel, ptr, self._post_name(meeting, notes),
                                            self._notes_tags(meeting, notes), ptrs, "notes")
        elif ptr and ptr.get("messages"):
            try:
                channel = await self.msgs.channel(ptr["channel"])
                await self.msgs.edit(channel, ptr["messages"][0], spec=specs[0])
            except Exception as exc:
                if not is_missing(exc):
                    raise  # transient: the job retries, nothing is duplicated
                log.info("meeting-scribe: stored notes message gone (%s); re-posting", exc)
                channel = None
        if channel is None:
            if ptr and "attach" in ptr and attach is None:
                attach = bool(ptr["attach"])  # a re-post keeps the transcript intent of the original summary
            if ptr and ptr.get("forum"):
                await self._forget_post_contents(ptr, ptrs)
            channel = chat if chat is not None else await self._chat_channel(meeting)
            if mention and not ptr:  # the very first publication only: a re-post never pings (§19.4)
                who = await asyncio.to_thread(participants_line, self.repo, meeting, channel, self.adapter,
                                              private=await self.is_private(meeting), lang=self.o.lang)
                line = who.line
                specs = render_header(meeting, notes, self.o.lang, line, who.users)
            # the transcript may be attached to THIS summary (set once, when it is first posted)
            intent = bool(self.settings.delivery_discord_transcript) if attach is None else bool(attach)
            if is_forum(channel):
                forum = channel
                channel, first, tags = await self._new_post(forum, self._post_name(meeting, notes), specs[0],
                                                            self._notes_tags(meeting, notes))
                ptr = {"v": 2, "channel": channel.id, "thread": None, "forum": forum.id, "messages": [first.id],
                       "url": self._post_url(channel, first), "attach": intent,
                       "name": self._post_name(meeting, notes), "tags": tags}
            else:
                first = await self.msgs.send(channel, spec=specs[0])
                ptr = {"v": 2, "channel": channel.id, "thread": None, "messages": [first.id], "url": first.jump_url,
                       "attach": intent}
            if line:
                ptr["people"] = line
            await ptrs.save("notes", ptr, ptr["url"])
        if ptr.get("v") != 2:
            ptr = await self._migrate_legacy(channel, ptr, ptrs)
        ids: list[int] = list(ptr["messages"])
        for i, spec in enumerate(specs[1:], start=1):
            if i < len(ids):
                try:
                    await self.msgs.edit(channel, ids[i], spec=spec)
                    continue
                except Exception as exc:
                    if not is_missing(exc):
                        raise
                    log.info("meeting-scribe: notes part %s gone (%s); sending a new one", ids[i], exc)
            msg = await self.msgs.send(channel, spec=spec)
            ids[i:i + 1] = [msg.id]
            await ptrs.save("notes", {**ptr, "messages": ids}, ptr.get("url", ""))
        for surplus in ids[len(specs):]:  # the summary got shorter (never the first: a post's opening message)
            await self.msgs.delete(channel.id, surplus)
        ptr = {**ptr, "messages": ids[:len(specs)]}
        await ptrs.save("notes", ptr, ptr.get("url", ""))
        return channel, ptr

    async def _migrate_legacy(self, channel: Any, ptr: dict, ptrs: Pointers) -> dict:
        """0.1 kept the header in the channel and every task + button row in ``thread``: drop those."""
        tail_home = ptr.get("thread") or channel.id
        for mid in list(ptr.get("messages") or ())[1:]:
            await self.msgs.delete(tail_home, mid, notice=t("tasks.legacy_moved", self.o.lang))
        ptr = {**ptr, "v": 2, "messages": list(ptr.get("messages") or ())[:1]}
        await ptrs.save("notes", ptr, ptr.get("url", ""))
        return ptr

    # -- forum posts (DESIGN §19.1) -------------------------------------------------------------------
    def _post_name(self, meeting: Meeting, notes: Notes) -> str:
        """``<date> · <title>``: the forum post of a meeting (Discord caps thread names at 100)."""
        title = (notes.meeting_title or meeting.title or meeting.channel_name or t("tasks.thread_default", self.o.lang))
        return f"{meeting.started_at:%Y-%m-%d} · {' '.join(title.split())}"[:POST_NAME_LIMIT]

    def _notes_tags(self, meeting: Meeting, notes: Notes) -> list[str]:
        """Tag names wanted on the notes post: the meeting's project and ``delivery_forum_tags``."""
        return [n for n in (notes.project, meeting.project, *self.settings.delivery_forum_tags) if n]

    @staticmethod
    def _post_url(thread: Any, first: Any) -> str:
        return str(getattr(thread, "jump_url", "") or getattr(first, "jump_url", "") or "")

    async def _new_post(self, forum: Any, name: str, spec: MessageSpec, wanted: Sequence[str]) -> tuple[Any, Any, list]:
        """Create a post; ``(thread, first message, applied tag ids)``. A forum that requires a tag and
        refuses the post leaves the delivery waiting with the exact fix (never another channel)."""
        tags = pick_tags(forum, wanted, self.settings.delivery_forum_default_tag)
        try:
            thread, first = await self.msgs.create_post(forum, name=name, spec=spec, tags=tags)
        except Exception as exc:
            if not tag_rejected(exc):
                raise
            available = ", ".join(tag_names(forum)) or "none"
            raise DestinationPending(
                f"forum #{getattr(forum, 'name', forum.id)} requires a tag on every post and none of its tags "
                f"({available}) matches the meeting's project or delivery_forum_tags. Set one with "
                "`hermes meeting-scribe config set delivery_forum_default_tag \"<tag>\"`") from exc
        log.info("meeting-scribe: new forum post %s in %s", getattr(thread, "id", "?"), getattr(forum, "id", "?"))
        return thread, first, [str(getattr(tag, "id", "")) for tag in tags]

    async def _open_post(self, thread_id: Any, message_id: Any, spec: MessageSpec) -> tuple[Any, Any]:
        """``(post, first message id)`` of an existing forum post, its first message edited in place;
        ``(None, None)`` when the post was deleted. A post whose first message was deleted is kept:
        that message is posted again inside it (the rest of the post stays where it is)."""
        try:
            thread = await self.msgs.channel(thread_id)
        except Exception as exc:
            if not is_missing(exc):
                raise
            log.info("meeting-scribe: forum post %s gone (%s); creating a new one", thread_id, exc)
            return None, None
        try:
            await self.msgs.edit(thread, message_id, spec=spec)
            return thread, message_id
        except Exception as exc:
            if not is_missing(exc):
                raise
            log.info("meeting-scribe: first message of forum post %s gone (%s); posting it again", thread_id, exc)
        msg = await self.msgs.send(thread, spec=spec)
        return thread, msg.id

    async def _forget_post_contents(self, ptr: dict, ptrs: Pointers) -> None:
        """A deleted forum post took its transcript with it: forget that pointer so DELIVER posts the
        transcript again in the new post (tasks and index are re-posted by their own pointers)."""
        transcript = await ptrs.load(TRANSCRIPT_SUFFIX)
        if transcript and str(transcript.get("channel")) == str(ptr.get("channel")):
            await ptrs.drop(TRANSCRIPT_SUFFIX)

    async def _sync_post(self, thread: Any, ptr: dict, name: str, wanted: Sequence[str], ptrs: Pointers,
                         suffix: str) -> dict:
        """Rename the post / update our tags when the title or project changed (tags someone added by
        hand stay). A refused edit is logged and retried on the next publish; it never fails a delivery."""
        forum = getattr(thread, "parent", None)
        if forum is None:  # parent not cached: the tags cannot be matched, only the name is kept current
            ours = list(ptr.get("tags") or ())
            changes: dict[str, Any] = {"name": name}
        else:
            ours = [str(getattr(x, "id", "")) for x in pick_tags(forum, wanted, self.settings.delivery_forum_default_tag)]
            previous = {str(x) for x in ptr.get("tags") or ()}
            by_id = {str(getattr(x, "id", "")): x for x in list(getattr(forum, "available_tags", None) or ())}
            kept = [x for x in list(getattr(thread, "applied_tags", None) or ())  # tags added by hand stay
                    if str(getattr(x, "id", "")) not in previous and str(getattr(x, "id", "")) not in ours]
            changes = {"name": name, "applied_tags": ([by_id[i] for i in ours if i in by_id] + kept)[:MAX_FORUM_TAGS]}
        if ptr.get("name") == name and list(ptr.get("tags") or ()) == ours:
            return ptr
        try:
            await thread.edit(**changes)
        except Exception as exc:
            if is_missing(exc):
                raise
            log.info("meeting-scribe: could not rename/tag forum post %s: %s", getattr(thread, "id", "?"), exc)
            return ptr
        ptr = {**ptr, "name": name, "tags": ours}
        await ptrs.save(suffix, ptr, ptr.get("url", ""))
        return ptr

    async def chat_task_target(self, channel: Any, ptr: dict, ptrs: Pointers, meeting: Meeting) -> Any:
        if ptr.get("forum"):  # the meeting's forum post already is its thread
            return channel
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
        title = (meeting.title or meeting.channel_name or t("tasks.thread_default", self.o.lang))[:80]
        return t("tasks.thread_name", self.o.lang, title=title, date=f"{meeting.started_at:%Y-%m-%d}")[:100]

    # -- project channels -----------------------------------------------------------------------
    async def project_target(self, channel_id: str, meeting: Meeting, notes: Notes, count: int,
                             ptrs: Pointers, project: str = "") -> Any:
        channel = await self.msgs.channel(channel_id)
        title = notes.meeting_title or meeting.title or meeting.channel_name
        spec = MessageSpec(t("tasks.project_anchor", self.o.lang, title=title,
                             date=f"{meeting.started_at:%Y-%m-%d}", id=meeting.id, count=count))
        suffix = f"thread:{channel_id}"
        ptr = await ptrs.load(suffix) or {}
        if is_forum(channel):
            return await self._project_post(channel, meeting, notes, spec, ptr, suffix, ptrs, project)
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

    async def _project_post(self, forum: Any, meeting: Meeting, notes: Notes, spec: MessageSpec, ptr: dict,
                            suffix: str, ptrs: Pointers, project: str) -> Any:
        """A project forum: one post per meeting (its first message is the anchor), tasks inside it."""
        name = self._post_name(meeting, notes)
        wanted = [n for n in (project, *self.settings.delivery_forum_tags) if n]
        if ptr.get("forum") and ptr.get("thread"):
            thread, first_id = await self._open_post(ptr["thread"], ptr.get("message"), spec)
            if thread is not None:
                ptr = {**ptr, "message": first_id}
                await ptrs.save(suffix, ptr, ptr.get("url", ""))
                await self._sync_post(thread, ptr, name, wanted, ptrs, suffix)
                return thread
        thread, first, tags = await self._new_post(forum, name, spec, wanted)
        url = self._post_url(thread, first)
        await ptrs.save(suffix, {"channel": thread.id, "message": first.id, "thread": thread.id, "forum": forum.id,
                                 "url": url, "name": name, "tags": tags}, url)
        return thread

    @staticmethod
    def _notes_place(chat: Any) -> set[str]:
        """The notes channel, and — when the notes are a forum post — that forum: tasks routed there go
        into the meeting's post, never into a second post of the same forum."""
        parent = getattr(chat, "parent_id", None) or getattr(getattr(chat, "parent", None), "id", None)
        forum_post = parent is not None and is_forum(getattr(chat, "parent", None))
        return {str(chat.id)} | ({str(parent)} if forum_post else set())

    async def _fallback_target(self, meeting: Meeting, notes: Notes, chat: Any, count: int, ptrs: Pointers) -> Any:
        """``delivery_fallback_channel`` for tasks without a project channel (None = the notes chat)."""
        dest = self._destination(meeting)
        fallback = dest.fallback_channel
        if not fallback or str(fallback) in self._notes_place(chat):
            return None
        try:
            if not self._in_server(await self.msgs.channel(fallback), dest):
                return None
            return await self.project_target(fallback, meeting, notes, count, ptrs)
        except DestinationPending:
            raise  # a forum refused the post: the caller reports it, the tasks wait in the notes post
        except Exception as exc:  # deleted / no access: the notes chat, as before
            log.info("meeting-scribe: fallback channel %s unavailable (%s)", fallback, exc)
            return None

    # -- tasks ----------------------------------------------------------------------------------
    @staticmethod
    def shown(channel: Any, private: bool) -> Callable[[str], bool]:
        """Who a message in ``channel`` may show as a mention (DESIGN §19.4): a Discord member who can
        view it; anyone else is named."""
        return lambda uid: is_discord_user_id(uid) and may_mention(channel, uid, private=private)

    def _task_spec(self, meeting: Meeting, view: TaskView, channel: Any) -> Any:
        owner = str(view.item.owner_speaker_id or "")
        return render_task(meeting, view, self.o,
                           mention=self.shown(channel, view.sharing is not None)(owner))

    async def place_task(self, meeting: Meeting, view: TaskView, target: Any, ptrs: Pointers) -> dict:
        suffix = f"task:{view.item.id}"
        ptr = await ptrs.load(suffix)
        private = view.sharing is not None
        # a message left behind in a project channel by a meeting that became private must not keep its text
        notice = (t("share.moved_private", self.o.lang) if private else
                  t("tasks.moved_notice", self.o.lang, title=view.item.title,
                    channel=f"<#{view.route.channel_id or target.id}>"))
        placed = await self.msgs.edit_or_send(ptr, target, spec=self._task_spec(meeting, view, target),
                                              moved_notice=notice)
        new = {**placed, "target": self.task_target(view, private)}
        await ptrs.save(suffix, new, placed.get("url", ""))
        return new

    async def edit_task(self, meeting: Meeting, view: TaskView, ptrs: Pointers) -> bool:
        ptr = await ptrs.load(f"task:{view.item.id}")
        if not ptr or (ptr.get("target") or "") != self.task_target(view, view.sharing is not None):
            return False  # never posted or it must move: the caller re-publishes
        try:
            channel = await self.msgs.channel(ptr["channel"])
            await self.msgs.edit(channel, ptr["message"], spec=self._task_spec(meeting, view, channel))
            return True
        except Exception as exc:
            if not is_missing(exc):
                raise
            log.info("meeting-scribe: task message %s gone (%s)", ptr.get("message"), exc)
            return False

    # -- DMs ------------------------------------------------------------------------------------
    async def _user(self, uid: str) -> Any:
        client = getattr(self.adapter, "_client", None)
        user = client.get_user(int(uid)) if hasattr(client, "get_user") else None
        return user or await client.fetch_user(int(uid))

    async def dm(self, board: Board, uid: str, ptrs: Pointers, *, send: bool) -> bool:
        """Post (``send``) or refresh the assignee's DM panel; ``False`` when the DM could not be sent."""
        notes_ptr = await ptrs.load("notes") or {}
        panel = render_panel(board.meeting, board.views, user_id=uid, scope="m", page=0, o=self.o, is_owner=False,
                             link=str(notes_ptr.get("url") or ""))
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
        spec = render_index(board.meeting, board.views, threads, dm_failed, self.o, private=board.private,
                            shown=self.shown(chat, board.private))
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
        await self.msgs.edit(chat, ptr["message"], spec=render_index(board.meeting, board.views, threads,
                                                                     ptr.get("dm_failed") or (), self.o,
                                                                     private=board.private,
                                                                     shown=self.shown(chat, board.private)))

    async def _drop_stale_tasks(self, board: Board, ptrs: Pointers) -> None:
        """A reprocess that no longer finds a task deletes its message (and its pointer)."""
        alive = {v.item.id for v in board.views}
        for item_id, ptr in (await ptrs.with_prefix("task:")).items():
            if item_id not in alive:
                await self.msgs.delete(ptr.get("channel"), ptr.get("message"))
                await ptrs.drop(f"task:{item_id}")

    # -- whole meeting --------------------------------------------------------------------------
    async def _transcript(self, meeting: Meeting, chat: Any, notes_ptr: dict, ptrs: Pointers, *,
                          strict: bool = False) -> None:
        """DELIVER only (never a button refresh). A summary posted without the attachment intent —
        before the upgrade, or with the setting off — never gets the full transcript afterwards.
        ``strict`` (a move out of a DM): a failed upload raises, so the DM copy is not deleted and
        the job retries; otherwise it never fails the delivery (DESIGN §17.3)."""
        if not self.settings.delivery_discord_transcript or self._transcript_text is None:
            return
        if not notes_ptr.get("attach") and await ptrs.load(TRANSCRIPT_SUFFIX) is None:
            await mark_legacy(ptrs)
            return
        try:
            ptr = await publish_transcript(self.msgs, ptrs, chat, meeting, self._transcript_text,
                                           getattr(self.views, "file", None), self.o.lang,
                                           max_bytes=int(self.settings.delivery_transcript_max_mb) * 1024 * 1024)
        except Exception as exc:
            if strict:
                raise RuntimeError(f"the transcript of meeting {meeting.id} could not be re-posted while moving "
                                   f"it out of the DM ({type(exc).__name__}: {exc}); the DM copy is kept") from exc
            if not is_missing(exc):
                log.info("meeting-scribe: transcript attachment skipped: %s", exc)
            return
        if strict and ptr is not None and not ptr.get("done"):  # a transient upload error, logged inside
            raise RuntimeError(f"the transcript of meeting {meeting.id} could not be re-posted while moving it "
                               "out of the DM (upload failed); the DM copy is kept and the delivery retries")

    # -- notes an older version posted in a DM (DESIGN §19) ---------------------------------------
    async def _kv(self, key: str, value: Optional[str]) -> None:
        await asyncio.to_thread(self.repo.kv_set, key, value)

    async def _dm_of(self, ptr: Optional[dict]) -> Any:
        """The DM channel holding ``ptr``'s message, ``None`` when it is a server channel (or unknown)."""
        if not ptr or not ptr.get("channel"):
            return None
        try:
            channel = await self.msgs.channel(ptr["channel"])
        except Exception as exc:
            if is_missing(exc):  # the DM is gone: the normal path re-posts
                return None
            raise  # transient: retry later rather than guess
        return channel if getattr(channel, "guild", None) is None else None

    def _dm_hint(self, meeting: Meeting, target: str = "") -> str:
        cmd = f"`hermes meeting-scribe reprocess {meeting.id} --from deliver`"
        if target:
            return (f"the notes of meeting {meeting.id} are in a direct message (posted by an older version); "
                    f"run {cmd} to move them to {target}")
        key = channel_key(meeting)
        return (f"the notes of meeting {meeting.id} are in a direct message (posted by an older version) and stay "
                f"there: no notes channel is configured. Run `hermes meeting-scribe config set {key} "
                f"\"#channel-name\"` (or a channel id), then {cmd}")

    async def _open_target(self, meeting: Meeting, cid: Any, key: str) -> Any:
        """Open and check the server channel BEFORE anything in the DM changes."""
        try:
            channel = await self.msgs.channel(cid)
        except Exception as exc:
            raise DmMoveError(f"cannot move meeting {meeting.id} out of the DM: channel {cid} ({key}) is not "
                              f"reachable ({type(exc).__name__}: {exc}); the DM copy is kept") from exc
        if not self._in_server(channel, self._destination(meeting)):
            raise DmMoveError(f"cannot move meeting {meeting.id} out of the DM: channel {cid} ({key}) is not a "
                              "channel of the meeting's server; the DM copy is kept")
        return channel

    async def _start_move(self, meeting: Meeting, dm: Any, ptrs: Pointers) -> Optional[dict]:
        """Only for an explicit ``reprocess --from deliver`` and a CONFIGURED channel (never the automatic
        one: a private DM must not end up in #general). Returns the move, ``None`` when refused."""
        step = explicit_channel(self._destination(meeting))
        if step is None:
            log.warning("meeting-scribe: meeting %s stays in a DM: no notes channel configured", meeting.id)
            await self._kv(KV_DM_NOTES + meeting.id, self._dm_hint(meeting))
            await self._kv(KV_MOVE_FROM_DM + meeting.id, None)
            return None
        target = await self._open_target(meeting, step.channel_id, step.key)
        notes_ptr = await ptrs.load("notes") or {}
        old: dict[str, dict] = {}
        for suffix in ("notes", "index", TRANSCRIPT_SUFFIX):
            p = await ptrs.load(suffix)
            if p and str(p.get("channel")) == str(dm.id):  # the legacy marker has no channel: it stays
                old[suffix] = p
        for item, p in (await ptrs.with_prefix("task:")).items():
            if str(p.get("channel")) == str(dm.id):
                old[f"task:{item}"] = p
        move = {"from": dm.id, "channel": target.id, "key": step.key,
                "attach": transcript_intent(notes_ptr, await ptrs.load(TRANSCRIPT_SUFFIX)), "old": old}
        await ptrs.save(MOVE_SUFFIX, move)  # first: an interrupted move resumes from here
        for suffix in old:  # the DM messages stay until the new ones exist (their ids live in ``move``)
            await ptrs.drop(suffix)
        log.warning("meeting-scribe: moving meeting %s from a DM to channel %s", meeting.id, target.id)
        return move

    async def _replaced(self, suffix: str, dm_id: Any, ptrs: Pointers, alive: set[str]) -> tuple[bool, str]:
        """Is the new copy of ``suffix`` confirmed outside the DM? ``(ok, why-not)``."""
        if suffix.startswith("task:") and suffix[len("task:"):] not in alive:
            return True, ""  # the task no longer exists: nothing replaces it, the DM copy is stale
        new = await ptrs.load(suffix)
        if not new or not _message_ids(new) or str(new.get("channel")) == str(dm_id):
            if suffix == TRANSCRIPT_SUFFIX and new and new.get("skipped"):
                return False, f"its copy in the server channel was not posted (skipped: {new['skipped']})"
            return False, "its copy in the server channel was not confirmed"
        if suffix == TRANSCRIPT_SUFFIX and (not new.get("done") or new.get("skipped")):
            return False, f"its copy in the server channel is incomplete ({new.get('skipped') or 'not done'})"
        return True, ""

    def _leftover_hint(self, meeting: Meeting, why: dict[str, str]) -> str:
        kinds = "; ".join(f"{suffix}: {reason}" for suffix, reason in why.items())
        return (f"meeting {meeting.id} was moved out of a direct message, but some DM messages were kept because "
                f"their replacement is not confirmed ({kinds}). They are removed by a later "
                f"`hermes meeting-scribe reprocess {meeting.id} --from deliver` once replaced; delete them by hand "
                "if they must go now")

    async def _finish_move(self, meeting: Meeting, move: dict, ptrs: Pointers, *, alive: set[str]) -> None:
        """Last step, after the new summary, tasks and index exist and their pointers are saved. A DM
        message is deleted only when its replacement is confirmed; the rest is kept and explained."""
        kept: dict[str, dict] = {}
        why: dict[str, str] = {}
        for suffix, p in (move.get("old") or {}).items():
            ok, reason = await self._replaced(suffix, move.get("from"), ptrs, alive)
            if not ok:
                kept[suffix], why[suffix] = p, reason
                continue
            for mid in _message_ids(p):
                await self.msgs.delete(p.get("channel") or move.get("from"), mid)
        await ptrs.drop(MOVE_SUFFIX)
        await self._kv(KV_MOVE_FROM_DM + meeting.id, None)
        if kept:
            await ptrs.save(LEFTOVER_SUFFIX, {"from": move.get("from"), "old": kept, "why": why})
            await self._kv(KV_DM_NOTES + meeting.id, self._leftover_hint(meeting, why))
            log.warning("meeting-scribe: meeting %s moved out of the DM; kept there: %s", meeting.id, why)
            return
        await ptrs.drop(LEFTOVER_SUFFIX)
        await self._kv(KV_DM_NOTES + meeting.id, None)
        log.warning("meeting-scribe: meeting %s moved out of the DM", meeting.id)

    async def _rollback_move(self, meeting: Meeting, move: dict, ptrs: Pointers) -> None:
        """Back to "lives in the DM" (its messages were never deleted): nothing new was posted yet."""
        for suffix, p in (move.get("old") or {}).items():
            await ptrs.save(suffix, p, p.get("url", "") if suffix == "notes" else "")
        await ptrs.drop(MOVE_SUFFIX)
        log.warning("meeting-scribe: move of meeting %s out of the DM rolled back; the DM copy is kept", meeting.id)

    async def _move_target(self, meeting: Meeting, move: dict, ptrs: Pointers, *, deliver: bool) -> Any:
        """The server channel of a move in progress, or ``None`` after rolling it back (a button refresh
        before the new summary exists, or an unreachable channel) — the DM keeps working meanwhile."""
        posted = await ptrs.load("notes") is not None
        if not posted and not deliver:
            await self._rollback_move(meeting, move, ptrs)
            return None
        try:
            return await self._open_target(meeting, move["channel"], move.get("key", ""))
        except DmMoveError:
            if not posted:
                await self._rollback_move(meeting, move, ptrs)
            raise

    async def _dm_state(self, meeting: Meeting, ptrs: Pointers, *, move_from_dm: bool) -> Optional[dict]:
        """The move to carry on (started now or earlier), else ``None`` (the meeting is edited in place)."""
        move = await ptrs.load(MOVE_SUFFIX)
        if move is not None:
            return move
        dm = await self._dm_of(await ptrs.load("notes"))
        if dm is None:
            if await ptrs.load(LEFTOVER_SUFFIX) is None:  # a kept DM message keeps its explanation
                await self._kv(KV_DM_NOTES + meeting.id, None)
            return None
        if move_from_dm:
            return await self._start_move(meeting, dm, ptrs)
        step = explicit_channel(self._destination(meeting))
        await self._kv(KV_DM_NOTES + meeting.id,
                       self._dm_hint(meeting, f"<#{step.channel_id}>" if step is not None else ""))
        return None

    async def publish(self, meeting: Meeting, notes: Notes, *, send_dms: bool, attach_transcript: bool = False,
                      move_from_dm: bool = False, identity_refresh: bool = False) -> str:
        """``attach_transcript`` marks the pipeline's DELIVER; ``move_from_dm`` an explicit
        ``reprocess --from deliver`` (the only way a meeting leaves a DM). A move is finished — DM
        messages deleted — only by a DELIVER, after everything new was posted. A direct-messages
        meeting (DESIGN §19.3) takes its own path and never touches a channel."""
        if await self.is_dm(meeting):
            return (await DmDelivery(self).publish(meeting, notes, deliver=attach_transcript and not identity_refresh)).url
        ptrs = Pointers(self.repo, meeting.id)
        if identity_refresh:
            if await ptrs.load(MOVE_SUFFIX):
                raise DestinationPending("identity refresh waits for the explicit DM move to finish")
            existing = await ptrs.load("notes")
            if not existing:
                return ""  # identity correction is not permission to start a new publication
            legacy_dm = await self._dm_of(existing)
            if legacy_dm is not None:
                specs = render_header(meeting, notes, self.o.lang)
                for mid, spec in zip(existing.get("messages") or (), specs):
                    await self.msgs.edit(legacy_dm, mid, spec=spec)
                return str(existing.get("url") or "")  # no new DM delivery or legacy migration
        if await self.is_private(meeting):  # anything posted before it became private leaves public places
            await withdraw_public(self, ptrs, await self.private_place(meeting, ptrs))
        move = (None if identity_refresh else
                await self._dm_state(meeting, ptrs, move_from_dm=move_from_dm and attach_transcript))
        chat = await self._move_target(meeting, move, ptrs, deliver=attach_transcript) if move else None
        if move is not None and chat is None:
            move = None
        chat, notes_ptr = await self.header(meeting, notes, ptrs, chat=chat,
                                            attach=move_intent(move) if move else None)
        if attach_transcript:
            await self._transcript(meeting, chat, notes_ptr, ptrs,
                                   strict=bool(move and TRANSCRIPT_SUFFIX in (move.get("old") or {})))
        board = await self.board(meeting, notes)
        groups: dict[Optional[str], list[TaskView]] = {}
        for view in board.views:  # a private meeting keeps every task in its notes channel
            groups.setdefault(None if board.private else view.route.channel_id, []).append(view)
        threads: dict[Optional[str], Any] = {}
        refused: list[str] = []  # forums that refused a post (required tag): the delivery waits after the rest
        for cid, views in groups.items():
            target = None
            try:
                if cid is not None and str(cid) in self._notes_place(chat):
                    target = chat  # the project channel is the notes forum: its tasks live in the meeting's post
                elif cid is not None:
                    try:
                        target = await self.project_target(cid, meeting, notes, len(views), ptrs,
                                                           project=views[0].route.project or "")
                    except DestinationPending:
                        raise
                    except Exception as exc:  # channel vanished since the snapshot: meeting chat
                        log.info("meeting-scribe: project channel %s unavailable (%s)", cid, exc)
                if target is None and cid is None and not board.private:
                    target = await self._fallback_target(meeting, notes, chat, len(views), ptrs)
            except DestinationPending as exc:  # these tasks wait for the forum; never another channel
                log.warning("meeting-scribe: meeting %s: %s", meeting.id, exc)
                refused.append(str(exc))
                continue
            if target is None:
                target = await self.chat_task_target(chat, notes_ptr, ptrs, meeting)
            threads[cid] = str(target.id)
            for view in views:
                await self.place_task(meeting, view, target, ptrs)
        await self._drop_stale_tasks(board, ptrs)
        dm_failed: list[str] = []
        # Only Discord users can be DMed; imported speakers (``gmeet:…``) are skipped (DESIGN §17).
        assignees = [str(u) for u in dict.fromkeys(v.item.owner_speaker_id for v in board.views
                                                   if is_discord_user_id(v.item.owner_speaker_id))]
        if board.private:  # no panels: only what a member shared (kept current here)
            assignees = []
            await sync_copies(self, board, ptrs)
        elif self.settings.delivery_dm_assignees:
            for uid in assignees:
                if not await self.dm(board, uid, ptrs, send=send_dms):
                    dm_failed.append(uid)
        for uid in (await ptrs.with_prefix("dm:")):  # lost every task since the last run: empty panel
            if uid not in assignees:
                await self.dm(board, uid, ptrs, send=False)
        await self.index(board, chat, threads, dm_failed, ptrs)
        if attach_transcript and not identity_refresh:
            alive = {v.item.id for v in board.views}
            if move is not None:
                await self._finish_move(meeting, move, ptrs, alive=alive)
            else:
                leftover = await ptrs.load(LEFTOVER_SUFFIX)
                if leftover is not None:  # a kept DM message whose replacement may exist by now
                    await self._finish_move(meeting, leftover, ptrs, alive=alive)
            if refused:  # everything else is posted; the refused tasks are posted once the forum accepts them
                raise DestinationPending("; ".join(dict.fromkeys(refused)))
        return str(notes_ptr.get("url") or "")
