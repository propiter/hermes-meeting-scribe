"""What a click on a task button does (DESIGN §8, §16), independent of discord.py.

Authorization is per task (:mod:`auth`). Service calls hit SQLite/Kanban/Linear, so they run in a
worker thread; the interaction is deferred first (Discord's 3 s deadline) and answered with an
ephemeral follow-up. After an action only THAT task's message (plus its assignee's DM and the index
counts) is re-rendered; a click that came from an ephemeral panel re-renders the panel too.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import re
from typing import Any, Callable, Optional, Sequence

from ..config import Settings
from ..domain.errors import (ChannelUnavailable, DirectMessageUnavailable, ForumTagRequired, ItemDismissed, NotesNotReady,
                             NotPrivate, SinkUnavailable)
from ..domain.models import Candidate
from ..i18n import t
from ..pipeline.speakers import AssignError
from ..privacy import people as _people
from ..pipeline.task_assign import Actor, TaskAssignError
from .auth import (ASSIGN_ACTIONS, MEETING_ACTIONS, MEETING_OWNER_ONLY, OPEN_ACTIONS, SHARE_ACTIONS, SPEAKER_ACTIONS,
                   TASK_ACTIONS, can_view, check_dm, check_private, check_speaker, check_task)
from .render import ButtonSpec, custom_id

log = logging.getLogger(__name__)
SELECT_LIMIT = 25
REPLY_LIMIT = 1900  # Discord rejects messages over 2000 characters (review S2)
_PAGE_RE = re.compile(r"^(?P<scope>[am])(?P<page>\d{1,3})$")
_SPACE: contextvars.ContextVar[str] = contextvars.ContextVar("meeting_scribe_click_space", default="")


def clip_reply(text: str, limit: int = REPLY_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


_SINK_NAMES = {"kanban": "Kanban", "linear": "Linear"}


class UserMessage(ValueError):
    """A reply already written for the user (e.g. "no project was selected")."""


def friendly_error(exc: BaseException, lang: str) -> str:
    """What the clicker reads when an action fails: plain words, never the exception text (that goes
    to the log, where the administrator finds it)."""
    if isinstance(exc, SinkUnavailable):
        return t("ui.error_sink_unavailable", lang, sink=_SINK_NAMES.get(exc.sink, exc.sink.title()))
    if isinstance(exc, ItemDismissed):
        return t("ui.error_dismissed", lang)
    if isinstance(exc, DirectMessageUnavailable):
        return t("share.error_dm", lang)
    if isinstance(exc, NotPrivate):
        return t("share.not_private", lang)
    if isinstance(exc, NotesNotReady):
        return t("ui.error_notes_not_ready", lang)
    if isinstance(exc, ForumTagRequired):
        return t("ui.error_forum_needs_tag", lang)
    if isinstance(exc, ChannelUnavailable):
        return t("ui.error_channel_unavailable", lang)
    if isinstance(exc, UserMessage):
        return str(exc)
    if isinstance(exc, AssignError):
        return t(f"speakers.error_{exc.code}", lang, detail=exc.detail)
    if isinstance(exc, TaskAssignError):
        return assign_error(exc, lang)
    if isinstance(exc, (KeyError, LookupError)):
        return t("ui.error_not_found", lang)
    return t("ui.action_failed", lang)


def assign_error(exc: TaskAssignError, lang: str) -> str:
    """Why a task could not be assigned, in the reader's words (mentions shown, never pinged)."""
    detail = f"<@{exc.detail}>" if exc.detail.isdigit() else exc.detail
    return t(f"assign.error_{exc.code}", lang, detail=detail)


def assign_reply(done: Any, lang: str, actor: str) -> str:
    """What the person who assigned reads: the change, and what happened in Linear/Kanban."""
    if not done.changed:
        text = t("assign.unchanged", lang, title=done.title)
    elif done.user is None:
        text = t("assign.released", lang, title=done.title)
    elif done.user == actor:
        text = t("assign.taken", lang, title=done.title)
    else:
        who = f"<@{done.user}>" if str(done.user).isdigit() else done.name
        text = t("assign.given", lang, title=done.title, user=who)
    for sink, status in sorted((done.sinks or {}).items()):
        text += "\n" + t(f"assign.sink_{status}", lang, sink=_SINK_NAMES.get(sink, sink))
    return text


def _from_panel(interaction: Any) -> bool:
    """The click came from an ephemeral panel or a DM (not from a public task message)."""
    flags = getattr(getattr(interaction, "message", None), "flags", None)
    return bool(getattr(flags, "ephemeral", False)) or getattr(interaction, "guild", 1) is None


class ButtonActions:
    def __init__(self, *, service: Callable[[], Any], settings: Callable[..., Settings],
                 owners: Callable[..., Sequence[str]], check_auth: Callable[[Any], bool], sink: Callable[[], Any],
                 project_view: Callable[[str, Sequence[Candidate]], Any],
                 move_view: Callable[[str, str, Sequence[tuple[str, str]]], Any],
                 buttons_view: Callable[[Sequence[ButtonSpec]], Any] = lambda _specs: None,
                 speaker_view: Callable[[str, str, Sequence[tuple[str, str]]], Any] = lambda *_a: None,
                 assign_view: Callable[[str, str, Sequence[tuple[str, str]], Sequence[ButtonSpec]], Any] =
                 lambda *_a: None) -> None:
        self._buttons_view = buttons_view
        self._assign_view = assign_view
        self._speaker_view = speaker_view
        self._service = service
        self._settings = settings
        self._owners = owners
        self._check_auth = check_auth
        self._sink = sink
        self._project_view = project_view
        self._move_view = move_view

    # Every click acts on ONE meeting: its space's language and owners apply (DESIGN §23). The space
    # is looked up once per click and kept in a context variable (clicks run concurrently).
    @property
    def space(self) -> str:
        return _SPACE.get()

    @property
    def lang(self) -> str:
        return (self._settings(self.space) if self.space else self._settings()).ui_language

    def _uid(self, interaction: Any) -> str:
        return str(getattr(interaction.user, "id", ""))

    def _owner_ids(self) -> frozenset[str]:
        return frozenset(str(o) for o in (self._owners(self.space) if self.space else self._owners()))

    def _meeting_space(self, meeting_id: str) -> str:
        try:
            meeting = self._service().repo.get_meeting(meeting_id)
        except Exception:  # storage trouble: the action itself reports it
            return ""
        return getattr(meeting, "space", "") or "" if meeting is not None else ""

    def is_owner(self, interaction: Any) -> bool:
        return self._uid(interaction) in self._owner_ids()

    def _hermes_allows(self, interaction: Any) -> bool:
        try:
            return bool(self._check_auth(interaction))
        except Exception:  # fail closed
            log.exception("meeting-scribe: component auth check failed")
            return False

    async def _deny(self, interaction: Any, message: str) -> None:
        await interaction.response.send_message(clip_reply(message), ephemeral=True)

    async def _private_gate(self, interaction: Any, action: str, meeting_id: str, item_id: str) -> Optional[bool]:
        """A private meeting's buttons work only inside its channel, for who can see it (DESIGN §19.2); a
        direct-messages meeting's only in the clicker's own copy, on their own tasks (§19.3).
        ``False``: refused (answered); ``True``: a share action, allowed; ``None``: go on with the task rules."""
        try:
            recipients = await self._sink().dm_recipients(meeting_id)
            if recipients is not None:
                return await self._dm_gate(interaction, action, meeting_id, item_id, recipients)
            place = await self._sink().private_place(meeting_id)
        except Exception:  # cannot tell whether it is private: fail closed
            log.exception("meeting-scribe: privacy check of %s failed", meeting_id)
            await self._deny(interaction, t("ui.action_failed", self.lang))
            return False
        if place is None:
            if action in SHARE_ACTIONS:
                await self._deny(interaction, t("share.not_private", self.lang))
                return False
            return None
        verdict = check_private(interaction, place, self.lang)
        if not verdict.allowed:
            await self._deny(interaction, verdict.message)
            return False
        return True if action in SHARE_ACTIONS else None

    async def _dm_gate(self, interaction: Any, action: str, meeting_id: str, item_id: str,
                       recipients: dict[str, str]) -> Optional[bool]:
        item = (None if item_id == "all" or action in OPEN_ACTIONS
                else await asyncio.to_thread(self._service().repo.get_action_item, meeting_id, item_id))
        verdict = check_dm(interaction, recipients, action, item, self.lang)
        if not verdict.allowed:
            await self._deny(interaction, verdict.message)
            return False
        return True if action in SHARE_ACTIONS else None

    async def _authorize(self, interaction: Any, action: str, meeting_id: str, item_id: str) -> bool:
        gate = await self._private_gate(interaction, action, meeting_id, item_id)
        if gate is not None:
            return gate
        if action in OPEN_ACTIONS or action in ASSIGN_ACTIONS:  # the assignment rules are the service's
            return True
        if action in SPEAKER_ACTIONS:
            if not await self._speaker_gate(interaction, meeting_id):
                return False
            if self.is_owner(interaction) or self._hermes_allows(interaction):
                return True
            await self._deny(interaction, t("ui.not_allowed", self.lang))
            return False
        if item_id == "all" or action in MEETING_ACTIONS:  # 0.1 meeting-wide buttons keep their rule
            if self.is_owner(interaction):
                return True
            if action in MEETING_OWNER_ONLY:
                await self._deny(interaction, t("ui.owner_only", self.lang))
                return False
            if self._hermes_allows(interaction):
                return True
            await self._deny(interaction, t("ui.not_allowed", self.lang))
            return False
        item = await asyncio.to_thread(self._service().repo.get_action_item, meeting_id, item_id)
        verdict = check_task(action, item, self._uid(interaction), self._owner_ids(), self.lang, item_id)
        if not verdict.allowed:
            await self._deny(interaction, verdict.message)
        return verdict.allowed

    async def _speaker_gate(self, interaction: Any, meeting_id: str) -> bool:
        from ..pipeline.speakers import candidates

        meeting = await asyncio.to_thread(self._service().require, meeting_id)
        verdict = check_speaker(self._uid(interaction), frozenset(s.user_id for s in candidates(meeting)),
                                self._owner_ids(), self.lang)
        if not verdict.allowed:
            await self._deny(interaction, verdict.message)
        return verdict.allowed

    async def handle(self, interaction: Any, action: str, meeting_id: str, item_id: str,
                     values: Optional[Sequence[str]] = None) -> None:
        if action not in TASK_ACTIONS | OPEN_ACTIONS | MEETING_ACTIONS | SHARE_ACTIONS | SPEAKER_ACTIONS | ASSIGN_ACTIONS:
            return
        token = _SPACE.set(await asyncio.to_thread(self._meeting_space, meeting_id))
        try:
            await self._handle(interaction, action, meeting_id, item_id, values)
        finally:
            _SPACE.reset(token)

    async def _handle(self, interaction: Any, action: str, meeting_id: str, item_id: str,
                      values: Optional[Sequence[str]]) -> None:
        if not await self._authorize(interaction, action, meeting_id, item_id):
            return
        if values is None:
            values = list((getattr(interaction, "data", None) or {}).get("values") or ())
        if action == "prj":
            await (self._offer_projects(interaction, meeting_id) if item_id == "all"
                   else self._offer_channels(interaction, meeting_id, item_id))
            return
        if action in OPEN_ACTIONS:
            await self._panel(interaction, action, meeting_id, item_id)
            return
        if action in SHARE_ACTIONS:
            await self._share(interaction, action, meeting_id, item_id)
            return
        if action in ASSIGN_ACTIONS:
            await (self._offer_assignee(interaction, meeting_id, item_id) if action == "tas"
                   else self._assign_task(interaction, action, meeting_id, item_id, list(values or ())))
            return
        if action in SPEAKER_ACTIONS:
            if action == "scfm":
                meeting = await asyncio.to_thread(self._service().require, meeting_id)
                suggestion = next((s.suggested_user for s in meeting.speakers if s.user_id == item_id), None)
                values = [suggestion] if suggestion else []
                if not self.is_owner(interaction) and suggestion != self._uid(interaction):
                    action = "spk"  # someone else's suggested voice: a participant can only claim their own
            elif action == "sme":  # "that voice is me": always the clicker, whatever the message says
                values = [self._uid(interaction)]
            await (self._offer_speakers(interaction, meeting_id, item_id) if action == "spk"
                   else self._assign_speaker(interaction, meeting_id, item_id, list(values or ())))
            return
        from_panel = _from_panel(interaction) and action in TASK_ACTIONS
        if from_panel:  # update-type defer: edit_original_response then targets the clicked panel
            await interaction.response.defer()
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if action == "tsel":
                reply = await self._move(meeting_id, item_id, list(values or ()), viewer=self._uid(interaction),
                                         learn=self.is_owner(interaction))
            else:
                reply = await asyncio.to_thread(self._run, action, meeting_id, item_id, list(values or ()))
        except (KeyError, LookupError, ValueError) as exc:  # expected: the clicker can do something about it
            log.warning("meeting-scribe button %s on %s/%s refused: %s: %s", action, meeting_id, item_id,
                        type(exc).__name__, exc)
            reply = friendly_error(exc, self.lang)
        except Exception as exc:  # Kanban/Linear outages: tell the clicker, keep the gateway healthy
            log.exception("meeting-scribe button %s on %s/%s failed", action, meeting_id, item_id)
            reply = friendly_error(exc, self.lang)
        else:
            await self._after(interaction, action, meeting_id, item_id, from_panel)
        await interaction.followup.send(clip_reply(reply), ephemeral=True)

    async def _after(self, interaction: Any, action: str, meeting_id: str, item_id: str, from_panel: bool) -> None:
        sink = self._sink()
        try:
            if item_id == "all" or action in MEETING_ACTIONS:
                await sink.refresh(meeting_id)
            elif action != "tsel":  # a move already re-published everything
                await sink.refresh_item(meeting_id, item_id)
            if from_panel:
                view = await sink.task_panel(meeting_id, self._uid(interaction), "m", 0,
                                             is_owner=self.is_owner(interaction))
                await interaction.edit_original_response(view=view)
        except Exception:  # stale buttons are cosmetic; the action itself succeeded
            log.exception("meeting-scribe: refreshing tasks of %s failed", meeting_id)

    def _run(self, action: str, meeting_id: str, item_id: str, values: list[str]) -> str:
        svc = self._service()
        lang = self.lang
        if action in ("ok", "lin"):
            sink = "kanban" if action == "ok" else "linear"
            ref = svc.approve_item(meeting_id, item_id, sink)
            return t("ui.approved", lang, sink=_SINK_NAMES[sink], ref=ref)
        if action in ("allk", "alll"):
            sink = "kanban" if action == "allk" else "linear"
            res = svc.approve_all(meeting_id, sink)
            text = t("ui.approved_all", lang, sink=_SINK_NAMES[sink], count=len(res.delivered))
            if res.errors:
                log.warning("meeting-scribe: approving all of %s to %s: %s", meeting_id, sink, "; ".join(res.errors))
                text += "\n" + t("ui.approved_partial", lang, failed=len(res.errors))
            return text
        if action == "no":
            svc.dismiss_item(meeting_id, item_id)
            return t("ui.dismissed", lang)
        if action == "psel":
            if not values:
                raise UserMessage(t("ui.no_selection", lang))
            chosen = svc.set_project(meeting_id, values[0])
            return t("ui.project_saved", lang, project=chosen.name)
        raise ValueError(f"unknown action {action}")

    # -- private meetings (DESIGN §19.2) ----------------------------------------------------------------
    async def _share(self, interaction: Any, action: str, meeting_id: str, item_id: str) -> None:
        lang = self.lang
        if action == "sha":  # nothing happens before an explicit confirmation
            confirm = ButtonSpec(t("ui.btn_share_confirm", lang), custom_id("shc", meeting_id, "all"), "success", 0, "📤")
            await interaction.response.send_message(t("share.confirm_all", lang), view=self._buttons_view([confirm]),
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        sink = self._sink()
        try:
            if action == "shc":
                report = await sink.share_all(meeting_id)
                reply = t("share.done_all", lang, dms=report.dms, channels=report.channels)
                if report.failed:
                    reply += "\n" + t("share.failed_some", lang, tasks=", ".join(report.failed))
            else:
                done = await sink.share(meeting_id, item_id, "dm" if action == "shd" else "project")
                reply = (t("share.already", lang) if not done else t("share.done_dm", lang) if done == "dm"
                         else t("share.done_project", lang, channel=f"<#{done}>"))
        except Exception as exc:
            log.warning("meeting-scribe: share %s on %s/%s refused: %s: %s", action, meeting_id, item_id,
                        type(exc).__name__, exc)
            reply = friendly_error(exc, lang)
        await interaction.followup.send(clip_reply(reply), ephemeral=True)

    async def _move(self, meeting_id: str, item_id: str, values: list[str], *, viewer: str, learn: bool) -> str:
        if not values:
            raise UserMessage(t("ui.no_selection", self.lang))
        mention = await self._sink().move_item(meeting_id, item_id, values[0], viewer=viewer, learn=learn)
        item = await asyncio.to_thread(self._service().repo.get_action_item, meeting_id, item_id)
        if not learn:
            return t("tasks.moved_one", self.lang, channel=mention)
        project = (getattr(item, "project_hint", None) or getattr(item, "project", None) or mention)
        return t("tasks.moved", self.lang, channel=mention, project=project)

    async def _panel(self, interaction: Any, action: str, meeting_id: str, item_id: str) -> None:
        scope, page = "m", 0
        m = _PAGE_RE.match(item_id) if action == "pg" else None
        if m:
            scope, page = m["scope"], int(m["page"])
        in_place = action == "pg"
        if in_place:
            await interaction.response.defer()  # updates the panel message itself
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            view = await self._sink().task_panel(meeting_id, self._uid(interaction), scope, page,
                                                 is_owner=self.is_owner(interaction))
        except Exception as exc:
            log.exception("meeting-scribe: task panel of %s failed", meeting_id)
            await interaction.followup.send(clip_reply(friendly_error(exc, self.lang)), ephemeral=True)
            return
        if in_place:
            await interaction.edit_original_response(view=view)
        else:
            await interaction.followup.send(view=view, ephemeral=True)

    # -- who a task belongs to (DESIGN §16.2) ---------------------------------------------------------
    def actor(self, interaction: Any) -> Actor:
        """What this click proves about the clicker; nothing typed by anyone widens it."""
        return Actor(self._uid(interaction), admin=self.is_owner(interaction),
                     authorized=self._hermes_allows(interaction), sees=can_view(interaction),
                     name=str(getattr(interaction.user, "display_name", "") or ""))

    async def _offer_assignee(self, interaction: Any, meeting_id: str, item_id: str) -> None:
        """👤: owners pick who gets the task; its assignee may release it; anyone else is told whose it is."""
        from ..pipeline.task_assign import participants

        await interaction.response.defer(ephemeral=True, thinking=True)
        svc, lang, uid = self._service(), self.lang, self._uid(interaction)
        item = await asyncio.to_thread(svc.repo.get_action_item, meeting_id, item_id)
        if item is None:
            await interaction.followup.send(t("tasks.unknown", lang, item=item_id), ephemeral=True)
            return
        owner = item.owner_speaker_id or ""
        if not self.is_owner(interaction):
            if owner and owner == uid:
                release = ButtonSpec(t("ui.btn_release", lang), custom_id("trl", meeting_id, item_id), "secondary",
                                     0, "↩️")
                await interaction.followup.send(t("assign.yours", lang, title=item.title),
                                                view=self._buttons_view([release]), ephemeral=True)
            elif owner:
                await interaction.followup.send(t("assign.error_taken", lang, detail=f"<@{owner}>"
                                                  if owner.isdigit() else item.owner_name or owner), ephemeral=True)
            else:
                await interaction.followup.send(t("assign.take_hint", lang), ephemeral=True)
            return
        meeting = await asyncio.to_thread(svc.require, meeting_id)
        people = dict(await asyncio.to_thread(lambda: [(p, n) for p, n in _people(svc.repo, meeting) if p]))
        known = await asyncio.to_thread(participants, svc.repo, meeting)
        options = [("none", t("notes.unassigned", lang))] + [(p, people.get(p, p)) for p in known]
        history = await asyncio.to_thread(svc.repo.task_history, meeting_id, item_id)
        extra = ([ButtonSpec(t("ui.btn_undo", lang), custom_id("tun", meeting_id, item_id), "secondary", 2, "↩️")]
                 if any(not h["undone"] for h in history) else [])
        await interaction.followup.send(t("assign.pick", lang, title=item.title),
                                        view=self._assign_view(meeting_id, item_id, options[:SELECT_LIMIT], extra),
                                        ephemeral=True)

    async def _assign_task(self, interaction: Any, action: str, meeting_id: str, item_id: str,
                           values: list[str]) -> None:
        """🙋 take (always the clicker, whatever the message says), release, undo, or an owner's pick."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        actor, svc = self.actor(interaction), self._service()
        try:
            if action == "tun":
                done = await asyncio.to_thread(svc.undo_task_assignment, meeting_id, item_id, actor)
            else:
                who = {"tak": "me", "trl": "none"}.get(action) or (values[0] if values else "")
                if not who:
                    raise UserMessage(t("ui.no_selection", self.lang))
                done = await asyncio.to_thread(svc.assign_task, meeting_id, item_id, who, actor)
            reply = assign_reply(done, self.lang, actor.user_id)
        except (KeyError, LookupError, ValueError) as exc:
            log.warning("meeting-scribe: assigning task %s/%s refused: %s", meeting_id, item_id, exc)
            await interaction.followup.send(clip_reply(friendly_error(exc, self.lang)), ephemeral=True)
            return
        if done.changed:
            try:
                await self._sink().announce(meeting_id)
            except Exception:  # the change is saved and queued: the worker shows it on its next tick
                log.exception("meeting-scribe: showing the assignment of %s/%s failed", meeting_id, item_id)
        await interaction.followup.send(clip_reply(reply), ephemeral=True)

    # -- unidentified participants (DESIGN §4.1) -----------------------------------------------------
    async def _offer_speakers(self, interaction: Any, meeting_id: str, label: str) -> None:
        from ..pipeline.speakers import candidates

        from ..storage.artifacts import fmt_ts

        await interaction.response.defer(ephemeral=True, thinking=True)
        svc = self._service()
        meeting = await asyncio.to_thread(svc.require, meeting_id)
        track = next((tr for tr in await asyncio.to_thread(svc.speaker_tracks, meeting) if tr.label == label), None)
        if track is None or (track.owner and not self.is_owner(interaction)):
            await interaction.followup.send(t("speakers.already", self.lang, label=label,
                                              name=track.name if track else label), ephemeral=True)
            return
        span = f"{fmt_ts(track.first)}–{fmt_ts(track.last)}" if track.first is not None else t("speakers.no_lines", self.lang)
        if not self.is_owner(interaction):  # a participant can only say "that voice is me" (DESIGN §4.1)
            me = ButtonSpec(t("speakers.its_me", self.lang), custom_id("sme", meeting_id, label), "primary", 0, "🙋")
            await interaction.followup.send(t("speakers.is_it_you", self.lang, name=track.name, lines=track.lines,
                                              span=span), view=self._buttons_view([me]), ephemeral=True)
            return
        options = [("unassigned", t("capture.unidentified", self.lang)),
                   *((s.user_id, s.name) for s in candidates(meeting))][:SELECT_LIMIT]
        await interaction.followup.send(t("speakers.pick", self.lang, name=track.name, lines=track.lines, span=span),
                                        view=self._speaker_view(meeting_id, label, options), ephemeral=True)

    async def _assign_speaker(self, interaction: Any, meeting_id: str, label: str, values: list[str]) -> None:
        """The same operation as ``speaker assign``: the pipeline then edits every published message."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not values:
                raise UserMessage(t("ui.no_selection", self.lang))
            done = await asyncio.to_thread(self._service().assign_speaker, meeting_id, label, values[0],
                                           actor=self._uid(interaction), admin=self.is_owner(interaction))
            reply = (t("speakers.assigned", self.lang, label=label, name=done.name, lines=done.lines, tasks=done.tasks)
                     if done.changed else t("speakers.already", self.lang, label=label, name=done.name))
            if done.redeliver:
                reply += "\n" + t("speakers.redelivering", self.lang)
            elif done.deliver:
                reply += "\n" + t("speakers.delivering", self.lang)
        except (KeyError, LookupError, ValueError) as exc:
            log.warning("meeting-scribe: assigning %s of %s refused: %s", label, meeting_id, exc)
            reply = friendly_error(exc, self.lang)
        await interaction.followup.send(clip_reply(reply), ephemeral=True)

    async def _offer_channels(self, interaction: Any, meeting_id: str, item_id: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            options = list(await self._sink().move_options(meeting_id, item_id,
                                                          viewer=self._uid(interaction)))[:SELECT_LIMIT]
        except Exception as exc:
            log.exception("meeting-scribe: move options of %s/%s failed", meeting_id, item_id)
            await interaction.followup.send(clip_reply(friendly_error(exc, self.lang)), ephemeral=True)
            return
        if not options:
            await interaction.followup.send(t("ui.no_projects", self.lang), ephemeral=True)
            return
        await interaction.followup.send(t("tasks.pick_channel", self.lang),
                                        view=self._move_view(meeting_id, item_id, options), ephemeral=True)

    async def _offer_projects(self, interaction: Any, meeting_id: str) -> None:
        # Candidate lookup can hit Linear over HTTP: defer first, Discord's deadline is 3 s (S1).
        await interaction.response.defer(ephemeral=True, thinking=True)
        svc = self._service()
        try:
            meeting = await asyncio.to_thread(svc.require, meeting_id)
            cands = (await asyncio.to_thread(svc.candidates, meeting))[:SELECT_LIMIT]
        except Exception as exc:  # unknown meeting / catalog outage
            log.exception("meeting-scribe: project candidates of %s failed", meeting_id)
            await interaction.followup.send(clip_reply(friendly_error(exc, self.lang)), ephemeral=True)
            return
        if not cands:
            await interaction.followup.send(t("ui.no_projects", self.lang), ephemeral=True)
            return
        await interaction.followup.send(t("ui.pick_project", self.lang), view=self._project_view(meeting_id, cands),
                                        ephemeral=True)
