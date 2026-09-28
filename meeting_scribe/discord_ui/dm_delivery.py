"""Direct-messages-only meetings (a ``:dm`` rule, DESIGN §19.3).

Nothing is posted in any channel. Every participant (:func:`privacy.participants`) gets the whole
meeting in a DM: the summary, decisions and open questions, the transcript file, an index of every
task and — one message each, with their buttons — the tasks assigned to them. The first delivery
anchors the meeting to its recipients (:func:`privacy.anchor_dm`): from then on only they receive it
or can read it from chat, whatever the rules say later.

Pointers (``deliveries``, sink ``discord``) live under ``pdm:<user>:`` — ``notes`` ``{"channel",
"messages", "url"}``, ``transcript`` (as :mod:`transcript_file`), ``index`` and ``task:<item>``
``{"channel", "message"}``. Each is saved as soon as its message exists, so a retry or a reprocess
edits in place instead of posting twice. A participant whose DMs are closed is skipped and reported
(``privacy.DM_UNREACHABLE_KV``); the others still get their copy, and nothing is ever posted in a
channel instead. Whatever was published in public places before the rule existed is withdrawn first
(:func:`private_share.withdraw_public`, whose allowed place is the recipients' DMs).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import privacy
from ..domain.models import Meeting, Notes
from ..i18n import t
from .board import Board
from .destination import DestinationPending
from .private_share import sync_copies, withdraw_public
from .publisher import Pointers, is_missing
from .render import MessageSpec, render_header
from .render_tasks import render_dm_index, render_task
from .transcript_file import publish_transcript

if TYPE_CHECKING:
    from .task_publisher import TaskPublisher

log = logging.getLogger(__name__)
CLOSED_CODE = 50007  # Discord: "Cannot send messages to this user"
UNKNOWN_USER_CODE = 10013  # Discord: "Unknown User" (a deleted account; discord.py raises NotFound)


class Prefixed(Pointers):
    """The pointers of one recipient's copy: ``mtg:<id>:pdm:<user>:<suffix>``."""

    def __init__(self, repo: Any, meeting_id: str, uid: str) -> None:
        super().__init__(repo, meeting_id)
        self.prefix = f"{privacy.DM_COPY_PREFIX}{uid}:"

    def key(self, suffix: str) -> str:
        return super().key(self.prefix + suffix)


def unreachable(exc: BaseException) -> bool:
    """The person cannot get a DM from the bot: DMs closed (403 / 50007) or an unknown user (a deleted
    account: 10013, or a lookup miss). Such a recipient is skipped and the others still get theirs."""
    return (isinstance(exc, LookupError) or getattr(exc, "code", None) in (CLOSED_CODE, UNKNOWN_USER_CODE)
            or getattr(exc, "status", None) == 403)


@dataclass
class DmReport:
    sent: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    url: str = ""


class DmDelivery:
    def __init__(self, pub: "TaskPublisher") -> None:
        self.pub = pub
        self.repo = pub.repo
        self.lang = pub.o.lang

    async def recipients(self, meeting: Meeting) -> tuple[list[str], list[str], bool]:
        """``(recipients, participant names that match no Discord user, anchored)``: the anchored list
        once someone got a copy; before that, the participants as they resolve NOW (a person linked
        after a failed attempt is included in the next one)."""
        found, unmapped = await asyncio.to_thread(privacy.participants, self.repo, meeting)
        rec = await asyncio.to_thread(privacy.record, self.repo, meeting.id) or {}
        anchored = [str(u) for u in rec.get("recipients") or ()]
        return (anchored or found), unmapped, bool(anchored)

    async def _anchor(self, meeting: Meeting, people: list[str]) -> None:
        """The first delivery that reached someone fixes the recipients (DESIGN §19.3)."""
        rec = await asyncio.to_thread(privacy.record, self.repo, meeting.id) or {}
        rule = str(rec.get("rule") or self.pub._destination(meeting).rule or "")
        await asyncio.to_thread(privacy.anchor_dm, self.repo, meeting.id, rule, people)

    async def publish(self, meeting: Meeting, notes: Notes, *, deliver: bool) -> DmReport:
        """Post what is missing and edit the rest, for every recipient. ``deliver`` (the pipeline's
        DELIVER) records who could not be reached and waits (``DestinationPending``) when nobody was;
        the recipients are anchored only after at least one of them got the copy."""
        ptrs = Pointers(self.repo, meeting.id)
        people, unmapped, anchored = await self.recipients(meeting)
        place = set((await asyncio.to_thread(privacy.dm_channels, self.repo, meeting.id)).values())
        await withdraw_public(self.pub, ptrs, place)  # a rule added later: the public copies leave first
        report = DmReport(unmapped=unmapped)
        if not people:
            await self._note(meeting, report)
            raise DestinationPending(self._nobody(meeting, unmapped))
        board = await self.pub.board(meeting, notes)
        for uid in people:
            try:
                url = await self._copy(uid, meeting, notes, board, deliver=deliver)
            except Exception as exc:
                if not unreachable(exc):
                    raise  # transient: the job retries, the saved pointers keep it from posting twice
                log.info("meeting-scribe: meeting %s: no direct message to %s: %s", meeting.id, uid, exc)
                report.closed.append(uid)
                continue
            report.sent.append(uid)
            report.url = report.url or url
        if deliver and report.sent and not anchored:
            await self._anchor(meeting, people)
        await sync_copies(self.pub, board, ptrs)  # the tasks their assignees shared in project channels
        if deliver:
            await self._note(meeting, report)
            if not report.sent:
                raise DestinationPending(self._closed_reason(meeting, report))
        return report

    # -- one recipient ------------------------------------------------------------------------------
    async def _copy(self, uid: str, meeting: Meeting, notes: Notes, board: Board, *, deliver: bool) -> str:
        p = Prefixed(self.repo, meeting.id, uid)
        if not deliver and await p.load("notes") is None:
            return ""  # a button refresh only edits: a copy is started only by a delivery
        user = await self.pub._user(uid)
        channel = await user.create_dm()
        url = await self._header(meeting, notes, p, user, channel)
        s = self.pub.settings
        if deliver and s.delivery_discord_transcript and self.pub._transcript_text is not None:
            await publish_transcript(self.pub.msgs, p, channel, meeting, self.pub._transcript_text,
                                     getattr(self.pub.views, "file", None), self.lang,
                                     max_bytes=int(s.delivery_transcript_max_mb) * 1024 * 1024)
        mine = [v for v in board.views if str(v.item.owner_speaker_id or "") == uid]
        await self._put(p, "index", user, channel, render_dm_index(board.meeting, board.views, uid, self.lang))
        for view in mine:
            await self._put(p, f"task:{view.item.id}", user, channel, render_task(board.meeting, view, self.pub.o))
        alive = {v.item.id for v in mine}
        for item_id, old in (await p.with_prefix("task:")).items():  # gone, or now someone else's
            if item_id not in alive:
                await self.pub.msgs.delete(old.get("channel"), old.get("message"))
                await p.drop(f"task:{item_id}")
        return url

    async def _header(self, meeting: Meeting, notes: Notes, p: Prefixed, user: Any, channel: Any) -> str:
        ptr = await p.load("notes") or {}
        ids: list[Any] = list(ptr.get("messages") or ())
        url = str(ptr.get("url") or "")
        specs = render_header(meeting, notes, self.lang)
        for i, spec in enumerate(specs):
            if i < len(ids) and await self._edited(channel, ids[i], spec):
                continue
            msg = await self.pub.msgs.send(None, spec=spec, dm_user=user)
            ids[i:i + 1] = [msg.id]
            url = url if i else str(getattr(msg, "jump_url", "") or "")
            await p.save("notes", {"channel": channel.id, "messages": ids, "url": url}, url)
        for surplus in ids[len(specs):]:  # a shorter summary after a reprocess
            await self.pub.msgs.delete(channel.id, surplus)
        await p.save("notes", {"channel": channel.id, "messages": ids[:len(specs)], "url": url}, url)
        return url

    async def _edited(self, channel: Any, message_id: Any, spec: MessageSpec) -> bool:
        """Edit in place; ``False`` when the person deleted it (it is posted again)."""
        try:
            await self.pub.msgs.edit(channel, message_id, spec=spec)
            return True
        except Exception as exc:
            if not is_missing(exc):
                raise
            return False

    async def _put(self, p: Prefixed, suffix: str, user: Any, channel: Any, spec: MessageSpec) -> None:
        ptr = await p.load(suffix)
        if ptr and ptr.get("message") and await self._edited(channel, ptr["message"], spec):
            return
        msg = await self.pub.msgs.send(None, spec=spec, dm_user=user)
        await p.save(suffix, {"channel": channel.id, "message": msg.id})

    # -- what status / doctor say -------------------------------------------------------------------
    @staticmethod
    def _nobody(meeting: Meeting, unmapped: list[str]) -> str:
        extra = (f"; {len(unmapped)} participant(s) match no Discord user — link them with `/meeting link` in "
                 "the space, then reprocess") if unmapped else ""
        return (f"waiting: meeting {meeting.id} goes only by direct message (meeting_routes ':dm') and none of "
                f"its participants is a Discord user{extra}. It is never posted in a channel")

    @staticmethod
    def _closed_reason(meeting: Meeting, report: DmReport) -> str:
        who = ", ".join(report.closed)
        return (f"waiting: meeting {meeting.id} goes only by direct message and none of its participants accepts "
                f"direct messages from the bot (user ids: {who}). They can open their DMs for this server; the "
                "delivery is retried. It is never posted in a channel")

    async def _note(self, meeting: Meeting, report: DmReport) -> None:
        parts = []
        if report.closed:
            parts.append(t("dm.unreachable", self.lang, users=", ".join(report.closed)))
        if report.unmapped:
            parts.append(t("dm.unmapped", self.lang, count=len(report.unmapped)))
        await asyncio.to_thread(self.repo.kv_set, privacy.DM_UNREACHABLE_KV + meeting.id, " ".join(parts) or None)
