"""Private meetings on Discord (DESIGN §19.2): tasks stay in the private channel until shared.

Pointers (``deliveries``, sink ``discord``) of a private meeting, besides the usual ``notes``/``index``/
``task:<item>`` (all in the private channel):

* ``share:<item>`` — what was decided for the task: ``{"dm": bool, "channel": "<project channel id>"}``;
* ``stask:<item>`` — the shared copy in the project channel ``{"channel", "message", "target", "forum"?}``;
* ``sdm:<item>`` — the copy sent to the assignee ``{"channel", "message", "user"}``.

The copy pointer is saved BEFORE the decision, so a retry after a crash edits instead of posting twice;
a decision already recorded makes the button a no-op (idempotent). A reprocess edits the shared copies
in place (only the task text: never the summary, the quote or a link to the private channel) and
removes the copies of tasks that no longer exist.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Optional

from ..domain.errors import ChannelUnavailable, DirectMessageUnavailable, ForumTagRequired, ItemDismissed
from ..domain.models import ActionStatus, is_discord_user_id
from .board import Board
from .destination import is_forum, pick_tags, tag_rejected
from .publisher import Pointers, is_missing
from .render_tasks import Sharing, TaskView, render_shared_dm, render_shared_task

if TYPE_CHECKING:
    from .task_publisher import TaskPublisher

log = logging.getLogger(__name__)
SHARE, COPY, DM_COPY = "share:", "stask:", "sdm:"
POST_NAME_LIMIT = 100


@dataclass
class ShareReport:
    dms: int = 0
    channels: int = 0
    failed: list[str] = field(default_factory=list)  # task titles that could not be shared


async def with_sharing(board: Board, ptrs: Pointers, *, dm_on: bool, notes_place: set[str]) -> Board:
    """The board of a private meeting: each view carries its sharing state and possible targets."""
    shares = await ptrs.with_prefix(SHARE)
    names = {c.id: c.name for c in board.channels}
    views = []
    for view in board.views:
        s = shares.get(view.item.id) or {}
        target = view.route.channel_id or ""
        if target in notes_place:
            target = ""  # the project IS the private channel: nothing to publish elsewhere
        views.append(replace(view, sharing=Sharing(
            dm=bool(s.get("dm")), channel=str(s.get("channel") or ""), target=target,
            target_name=f"#{names[target]}" if target in names else "",
            can_dm=dm_on and is_discord_user_id(view.item.owner_speaker_id))))
    return replace(board, views=tuple(views), private=True)


async def _decide(ptrs: Pointers, item_id: str, **changes: Any) -> None:
    current = await ptrs.load(SHARE + item_id) or {}
    await ptrs.save(SHARE + item_id, {**current, **changes})


def _usable(view: Optional[TaskView], item_id: str) -> TaskView:
    if view is None or view.sharing is None:
        raise KeyError(item_id)
    if view.item.status is ActionStatus.DISMISSED:
        raise ItemDismissed(f"task {item_id} was dismissed")
    return view


async def share_dm(pub: "TaskPublisher", board: Board, item_id: str, ptrs: Pointers) -> bool:
    """Send the task to its assignee; ``False`` when it had already been sent."""
    view = _usable(board.view(item_id), item_id)
    assert view.sharing is not None
    if view.sharing.dm:
        return False
    if not view.sharing.can_dm:
        raise DirectMessageUnavailable(f"task {item_id} has no Discord assignee (or DMs are off)")
    uid = str(view.item.owner_speaker_id)
    spec = render_shared_dm(board.meeting, view, pub.o.lang)
    ptr = await ptrs.load(DM_COPY + item_id)
    try:
        user = await pub._user(uid)
        if ptr and not await _edited(pub, await user.create_dm(), ptr, spec):
            ptr = None
        if not ptr:
            msg = await pub.msgs.send(None, spec=spec, dm_user=user)
            ptr = {"channel": msg.channel.id, "message": msg.id, "user": uid}
    except Exception as exc:  # DMs closed (50007), unknown user: the clicker is told, nothing recorded
        log.info("meeting-scribe: sharing task %s with %s failed: %s", item_id, uid, exc)
        raise DirectMessageUnavailable(str(exc)) from exc
    await ptrs.save(DM_COPY + item_id, ptr)
    await _decide(ptrs, item_id, dm=True)
    return True


async def _edited(pub: "TaskPublisher", channel: Any, ptr: dict, spec: Any) -> bool:
    """Edit a copy in place; ``False`` when it was deleted (the caller posts it again)."""
    try:
        await pub.msgs.edit(channel, ptr["message"], spec=spec)
        return True
    except Exception as exc:
        if not is_missing(exc):
            raise
        return False


async def share_project(pub: "TaskPublisher", board: Board, item_id: str, ptrs: Pointers) -> str:
    """Publish the task (its text only) in its project channel; returns that channel id."""
    view = _usable(board.view(item_id), item_id)
    s = view.sharing
    assert s is not None
    if not s.target:
        raise ChannelUnavailable(f"task {item_id} has no project channel")
    if s.channel == s.target:
        return s.target
    try:
        channel = await pub.msgs.channel(s.target)
    except Exception as exc:
        raise ChannelUnavailable(f"channel {s.target} is not reachable ({exc})") from exc
    if not pub._in_server(channel, pub._destination(board.meeting)):
        raise ChannelUnavailable(f"channel {s.target} is not a channel of the meeting's server")
    spec = render_shared_task(board.meeting, view, pub.o.lang)
    ptr = await ptrs.load(COPY + item_id)
    if is_forum(channel):
        new = await _forum_copy(pub, channel, view, spec, ptr, s.target)
    else:
        if ptr and ptr.get("forum"):  # the task moved from a forum to a text channel: drop the old post
            await remove_copy(pub, ptr)
            ptr = None
        placed = await pub.msgs.edit_or_send(ptr, channel, spec=spec)
        new = {**placed, "target": s.target}
    await ptrs.save(COPY + item_id, new, str(new.get("url") or ""))
    await _decide(ptrs, item_id, channel=s.target)
    return s.target


async def _forum_copy(pub: "TaskPublisher", forum: Any, view: TaskView, spec: Any, ptr: Optional[dict],
                      target: str) -> dict[str, Any]:
    """A project forum: the shared task is a post of its own (named after the task)."""
    if ptr and str(ptr.get("forum")) == str(forum.id):
        try:
            post = await pub.msgs.channel(ptr["channel"])
        except Exception as exc:
            if not is_missing(exc):
                raise
            post = None
        if post is not None and await _edited(pub, post, ptr, spec):
            return {**ptr, "target": target}
    elif ptr:
        await remove_copy(pub, ptr)
    wanted = [n for n in (view.route.project, *pub.settings.delivery_forum_tags) if n]
    tags = pick_tags(forum, wanted, pub.settings.delivery_forum_default_tag)
    try:
        thread, first = await pub.msgs.create_post(forum, name=view.item.title[:POST_NAME_LIMIT] or "-", spec=spec,
                                                   tags=tags)
    except Exception as exc:
        if tag_rejected(exc):
            raise ForumTagRequired(f"forum {forum.id} requires a tag and none matches the task") from exc
        raise
    url = str(getattr(thread, "jump_url", "") or getattr(first, "jump_url", "") or "")
    return {"channel": thread.id, "message": first.id, "forum": forum.id, "target": target, "url": url}


async def share_all(pub: "TaskPublisher", board: Board, ptrs: Pointers) -> ShareReport:
    """The normal distribution, now: every open task to its assignee and to its project channel."""
    report = ShareReport()
    for view in board.views:
        s = view.sharing
        if s is None or view.item.status is ActionStatus.DISMISSED:
            continue
        ok = True
        if s.can_dm and not s.dm:
            try:
                report.dms += await share_dm(pub, board, view.item.id, ptrs)
            except (DirectMessageUnavailable, LookupError) as exc:
                log.info("meeting-scribe: share all: DM of %s failed: %s", view.item.id, exc)
                ok = False
        if s.target and s.channel != s.target:
            try:
                await share_project(pub, board, view.item.id, ptrs)
                report.channels += 1
            except LookupError as exc:  # ChannelUnavailable / ForumTagRequired
                log.info("meeting-scribe: share all: publishing %s failed: %s", view.item.id, exc)
                ok = False
        if not ok:
            report.failed.append(view.item.title)
    return report


async def sync_copies(pub: "TaskPublisher", board: Board, ptrs: Pointers) -> None:
    """Keep the shared copies current (reprocess, a button): edit them; delete those of gone tasks."""
    alive = {v.item.id: v for v in board.views}
    for prefix in (COPY, DM_COPY):
        for item_id, ptr in (await ptrs.with_prefix(prefix)).items():
            view = alive.get(item_id)
            if view is None:
                await remove_copy(pub, ptr)
                await ptrs.drop(prefix + item_id)
                await ptrs.drop(SHARE + item_id)
                continue
            spec = (render_shared_task(board.meeting, view, pub.o.lang) if prefix == COPY
                    else render_shared_dm(board.meeting, view, pub.o.lang))
            try:
                await pub.msgs.edit(await pub.msgs.channel(ptr["channel"]), ptr["message"], spec=spec)
            except Exception as exc:  # deleted by someone, DM closed: the decision stands, nothing re-posted
                if not is_missing(exc):
                    raise
                log.info("meeting-scribe: shared copy of task %s gone (%s)", item_id, exc)


async def remove_copy(pub: "TaskPublisher", ptr: dict) -> None:
    """Delete a message we posted; a post/thread of ours (``forum`` set: its parent) goes whole."""
    if ptr.get("forum"):
        try:
            post = await pub.msgs.channel(ptr["channel"])
            await post.delete()
            return
        except Exception as exc:
            if is_missing(exc):
                return
            log.info("meeting-scribe: could not delete forum post %s (%s); deleting its message", ptr["channel"], exc)
    await pub.msgs.delete(ptr.get("channel"), ptr.get("message"))


async def withdraw_public(pub: "TaskPublisher", ptrs: Pointers, place: set[str]) -> None:
    """A meeting published before it became private (a rule added later): remove everything it left
    outside the private channel ``place`` — summary, transcript, index, assignee panels, project anchors.
    The task messages move by themselves (``place_task``) and the rest is posted again in the private
    channel."""
    for suffix in ("notes", "index", "transcript"):
        ptr = await ptrs.load(suffix)
        if not ptr or not ptr.get("channel") or {str(ptr.get(k)) for k in ("channel", "forum") if ptr.get(k)} & place:
            continue
        if ptr.get("forum") and suffix == "notes":
            await remove_copy(pub, ptr)
        else:
            for mid in list(ptr.get("messages") or ()) + ([ptr["message"]] if ptr.get("message") else []):
                await pub.msgs.delete(ptr["channel"], mid)
            if suffix == "notes" and ptr.get("thread"):  # the thread that held its tasks
                await remove_copy(pub, {"forum": ptr["channel"], "channel": ptr["thread"]})
        await ptrs.drop(suffix)
    for uid, ptr in (await ptrs.with_prefix("dm:")).items():
        await pub.msgs.delete(ptr.get("channel"), ptr.get("message"))
        await ptrs.drop(f"dm:{uid}")
    for suffix, ptr in (await ptrs.with_prefix("thread:")).items():
        if ptr.get("forum"):  # a project forum: the meeting's post, whole
            await remove_copy(pub, {**ptr, "channel": ptr.get("thread") or ptr.get("channel")})
        else:  # the anchor message and the thread named after the meeting
            await pub.msgs.delete(ptr.get("channel"), ptr.get("message"))
            if ptr.get("thread"):
                await remove_copy(pub, {"forum": ptr["channel"], "channel": ptr["thread"]})
        await ptrs.drop(f"thread:{suffix}")
