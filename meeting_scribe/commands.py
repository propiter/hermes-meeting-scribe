"""``/meeting`` router (DESIGN §3), platform-agnostic.

Hermes calls ``handler(raw_args) -> str``; the caller identity comes from
``gateway.session_context.get_session_env`` (see :func:`caller_from_session`). ``start``/``stop``
are delegated to the Phase B capture controller; without one they answer honestly that live
recording is unavailable instead of pretending.
"""
from __future__ import annotations

import contextvars
import logging
import re
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, Sequence, Union

from .config import PRIMARY_COMMAND, Settings
from .domain.models import MeetingState, Stage
from .domain.text import is_ascii_digits
from .i18n import t
from .pipeline.service import MeetingService
from .privacy import Reader
from .spaces import SpaceAmbiguous, SpaceError
from .storage.artifacts import fmt_ts, read_notes, render_notes_md

log = logging.getLogger(__name__)
# Commands that do not read or write any space's data (capture resolves the space of the voice
# channel's server itself).
_NO_SPACE = frozenset({"start", "stop", "help"})
_SPACE: contextvars.ContextVar[str] = contextvars.ContextVar("meeting_scribe_command_space")
_MENTION_RE = re.compile(r"^<@!?([0-9]+)>$")


def state_label(state: Union[MeetingState, str, None], lang: str) -> str:
    """A meeting state as chat users read it (``done`` → "✅ Ready"); unknown values pass through."""
    value = getattr(state, "value", state) or ""
    label = t(f"state.{value}", lang)
    return value if label == f"state.{value}" else label


def stage_label(stage: Union[Stage, str, None], lang: str, *, redo: bool = False) -> str:
    """A pipeline stage in plain words: what failed (``label``) or what a reprocess will do (``redo``)."""
    value = getattr(stage, "value", stage) or ""
    key = f"stage.{value}.{'redo' if redo else 'label'}"
    label = t(key, lang)
    return value if label == key else label


@dataclass(frozen=True)
class Caller:
    platform: str
    chat_id: str
    user_id: str
    thread_id: str = ""
    # The chat's server (Discord guild id) — Hermes' platform-neutral ``scope_id``; "" in a DM. It
    # decides the space the command acts in (DESIGN §23).
    scope_id: str = ""
    parent_chat_id: str = ""  # a thread's / forum post's channel (private meetings, DESIGN §19.2)
    source: str = ""  # Hermes' session source (cli, tui, desktop, ...): who the operator is without a chat
    cron: bool = False  # a scheduled job: its output goes wherever the job delivers (never private meetings)

    @property
    def guild_id(self) -> str:
        return self.scope_id if (self.platform or "").lower() == "discord" and is_ascii_digits(self.scope_id) else ""

    @property
    def reader(self) -> Reader:
        """Who reads, for private meetings: a chat sees one only from its private channel."""
        places = frozenset(str(x) for x in (self.chat_id, self.thread_id, self.parent_chat_id) if x)
        return Reader(self.platform or "", places, source=self.source, cron=self.cron)


def _labels(spaces: Sequence[Any]) -> str:
    return ", ".join(f"`{s.slug}` ({s.name})" for s in spaces)


def caller_from_session() -> Caller:
    from gateway.session_context import get_session_env

    return Caller(platform=get_session_env("HERMES_SESSION_PLATFORM"), chat_id=get_session_env("HERMES_SESSION_CHAT_ID"),
                  user_id=get_session_env("HERMES_SESSION_USER_ID"), thread_id=get_session_env("HERMES_SESSION_THREAD_ID"),
                  scope_id=get_session_env("HERMES_SESSION_SCOPE_ID", "") or "",
                  parent_chat_id=get_session_env("HERMES_SESSION_PARENT_CHAT_ID", "") or "",
                  source=get_session_env("HERMES_SESSION_SOURCE", "") or "",
                  cron=bool(get_session_env("HERMES_CRON_SESSION", "")))


class CaptureController(Protocol):
    """Implemented by Phase B (``meeting_scribe.capture``). Both return the chat reply."""

    def start(self, caller: Caller, target: Optional[str]) -> str: ...

    def stop(self, caller: Caller) -> str: ...

    def live_meeting_ids(self) -> set[str]: ...


#: ``membership(user_id, guild_ids)``: the ids among ``guild_ids`` of the Discord servers the user is
#: a member of; ``None`` when that cannot be checked (Discord not connected).
Membership = Callable[[str, Sequence[str]], Optional[set[str]]]


class MeetingCommands:
    """Every read/write command acts in ONE space: the one owning the caller's server, else (DM) the
    only space. With several spaces a Discord DM uses the space(s) of the servers the caller is a
    member of: one is used directly, several need ``space=<id>``. Nothing of a space the caller is
    not part of is ever shown."""

    def __init__(self, service: Union[MeetingService, Callable[[], MeetingService]], settings: Callable[..., Settings],
                 capture: Callable[[], Optional[CaptureController]],
                 membership: Callable[[], Optional[Membership]] = lambda: None) -> None:
        # A callable is resolved per command (review finding 7): the runtime may switch to another
        # profile's database, and a captured instance would keep answering from the old one.
        self._service = service if callable(service) else (lambda: service)
        self.settings = settings
        self.capture = capture
        self._membership = membership

    @property
    def service(self) -> MeetingService:
        return self._service()

    def handle(self, raw_args: str, caller: Caller, invoked_as: str = PRIMARY_COMMAND) -> str:
        lang = self.settings().ui_language
        try:
            parts = shlex.split(raw_args or "")
        except ValueError:
            parts = (raw_args or "").split()
        wanted = next((p.split("=", 1)[1].strip().lower() for p in parts if p.lower().startswith("space=")), None)
        parts = [p for p in parts if not p.lower().startswith("space=")]
        sub, args = (parts[0].lower(), parts[1:]) if parts else ("start", [])
        handler = getattr(self, f"_cmd_{sub}", None)
        if handler is None:
            return t("cmd.unknown", lang, sub=sub)
        token = None
        try:
            if sub not in _NO_SPACE:
                space, refusal = self._resolve_space(caller, wanted, lang, invoked_as, sub)
                if space is None:
                    return refusal
                token = _SPACE.set(space)
                lang = self.settings(space).ui_language
            return handler(args, caller, lang, invoked_as)
        except Exception:  # a slash command must always answer
            # The details are for the administrator (log / ``hermes meeting-scribe status``), not the chat.
            log.exception("meeting-scribe /%s %s failed", invoked_as, sub)
            return t("cmd.error", lang)
        finally:
            if token is not None:
                _SPACE.reset(token)

    def _resolve_space(self, caller: Caller, wanted: Optional[str], lang: str, cmd: str,
                       sub: str) -> tuple[Optional[str], str]:
        """``(space, "")``, or ``(None, reply)`` explaining what is missing."""
        service = self.service
        if caller.guild_id:
            try:
                space = service.space_for(caller.guild_id)
            except SpaceError:  # several spaces and this server is in none
                return None, t("space.unassigned", lang, guild=caller.guild_id)
            if wanted and wanted != space:
                return None, t("space.here_only", lang)
            return space, ""
        try:
            return service.space_for(None), ""  # one space: DMs and other platforms use it
        except SpaceAmbiguous:
            pass
        mine = self._spaces_of(caller)
        if mine is None:
            return None, t("space.choose", lang)
        if wanted:
            if any(s.slug == wanted for s in mine):
                return wanted, ""
            return None, t("space.not_yours", lang, slug=wanted, spaces=_labels(mine) or "-")
        if len(mine) == 1:
            return mine[0].slug, ""
        if not mine:
            return None, t("space.choose_none", lang)
        return None, t("space.choose_yours", lang, spaces=_labels(mine), cmd=cmd, sub=sub, example=mine[0].slug)

    def _spaces_of(self, caller: Caller) -> Optional[list]:
        """The spaces owning a Discord server the caller is a member of; ``None`` when it cannot be
        checked (another platform, or Discord not connected)."""
        from .spaces import Spaces

        check = self._membership() if (caller.platform or "").lower() == "discord" else None
        if check is None or not is_ascii_digits(str(caller.user_id)):
            return None
        spaces = Spaces(lambda: self.service.repo, lambda key, default=None: default).all()
        found = check(str(caller.user_id), [g for s in spaces for g in s.guild_ids])
        if found is None:
            return None
        return [s for s in spaces if found.intersection(s.guild_ids)]

    @property
    def _space(self) -> str:
        """The space of the command being handled (set per call: handlers may run concurrently)."""
        return _SPACE.get()

    # -- capture ------------------------------------------------------------------------------
    def _cmd_start(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        cap = self.capture()
        return cap.start(caller, args[0] if args else None) if cap else t("cmd.capture_unavailable", lang)

    def _cmd_stop(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        cap = self.capture()
        return cap.stop(caller) if cap else t("cmd.capture_unavailable", lang)

    # -- read ---------------------------------------------------------------------------------
    def _cmd_help(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        return t("cmd.help", lang, cmd=cmd)

    def _visible(self, meeting: Any, caller: Caller) -> bool:
        """A private meeting exists in chat only inside its private channel (DESIGN §19.2)."""
        return self.service.readable(meeting, caller.reader)

    def _find(self, ref: str, caller: Caller) -> Optional[Any]:
        meeting = self.service.find(ref, self._space)
        return meeting if meeting is not None and self._visible(meeting, caller) else None

    def _cmd_status(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        st = self.service.status(self._space)
        lines = [t("cmd.status_idle", lang, queued=st["queued"])]
        for row in st["recent"]:
            if not self._visible(row["id"], caller):
                continue
            job = row["job"] or {}
            extra = ""
            if row["state"] == MeetingState.FAILED.value:
                extra = t("cmd.status_failed_extra", lang,
                          stage=stage_label(job.get("failed_stage") or job.get("stage"), lang))
            elif (row.get("delivery") or {}).get("state") == "waiting_destination":
                extra = t("cmd.status_waiting_channel", lang)
            lines.append(t("cmd.status_line", lang, id=row["id"], title=row["title"],
                           state=state_label(row["state"], lang), extra=extra))
        return "\n".join(lines)

    def _cmd_list(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        n = int(args[0]) if args and is_ascii_digits(args[0]) else 10
        meetings = [m for m in self.service.repo.list_meetings(limit=max(1, min(n, 50)), space=self._space)
                    if self._visible(m, caller)]
        if not meetings:
            return t("cmd.list_empty", lang)
        return "\n".join(t("cmd.list_line", lang, id=m.id, date=f"{m.started_at:%Y-%m-%d %H:%M}",
                            title=m.title or m.channel_name, state=state_label(m.state, lang)) for m in meetings)

    def _cmd_show(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        if not args:
            return t("cmd.usage", lang, usage=f"/{cmd} show <id>")
        meeting = self._find(args[0], caller)
        if meeting is None:
            return t("cmd.not_found", lang, id=args[0])
        notes = read_notes(self.service.folder(meeting))
        if notes is None:
            return t("cmd.status_line", lang, id=meeting.id, title=meeting.title,
                     state=state_label(meeting.state, lang), extra="")
        body = render_notes_md(meeting, notes, notes.language or lang)
        return body.split("---\n", 2)[-1].strip()[:1900]

    def _cmd_search(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        query = " ".join(args)
        if not query:
            return t("cmd.usage", lang, usage=f"/{cmd} search <text>")
        hits = self.service.search(query, self._space, limit=8, reader=caller.reader)
        if not hits:
            return t("cmd.search_empty", lang, query=query)
        return "\n".join(t("cmd.search_line", lang, id=h["meeting_id"], ts=fmt_ts(h["t0"]), speaker=h["speaker"],
                            text=h["text"][:200]) for h in hits)

    def _cmd_config(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        from .config import SPEC

        def shown(v: object) -> str:
            if isinstance(v, bool):
                return t("cmd.config_yes" if v else "cmd.config_no", lang)
            if isinstance(v, tuple):
                return ", ".join(map(str, v)) or "-"
            return str(v) if v not in (None, "") else "-"
        return "\n".join(t("cmd.config_line", lang, label=SPEC[k].label(k, lang) if k in SPEC else k, value=shown(v))
                         for k, v in self.settings(self._space).as_dict().items())

    # -- write --------------------------------------------------------------------------------
    def _cmd_reprocess(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        if not args:
            return t("cmd.usage", lang, usage=f"/{cmd} reprocess <id> [from=transcribe|analyze|deliver]")
        raw = next((a.split("=", 1)[1] for a in args[1:] if a.startswith("from=")), "transcribe")
        try:
            stage = Stage.parse(raw)
        except ValueError:
            return t("cmd.reprocess_bad_stage", lang, stage=raw)
        if stage is Stage.ARCHIVE:
            return t("cmd.reprocess_bad_stage", lang, stage=raw)
        meeting = self._find(args[0], caller)
        if meeting is None:
            return t("cmd.not_found", lang, id=args[0])
        if meeting.state is MeetingState.EMPTY:
            return t("cmd.reprocess_empty", lang, id=meeting.id)
        used = self.service.effective_stage(meeting, stage)
        self.service.reprocess(meeting.id, stage)
        reply = t("cmd.reprocess_queued", lang, id=meeting.id, stage=stage_label(used, lang, redo=True))
        reply += "\n" + t("cmd.reprocess_no_audio", lang) if used is not stage else ""
        # The stored hint holds admin commands (shown by the CLI/doctor); the chat gets plain words.
        in_dm = bool(self.service.dm_notes().get(meeting.id)) if hasattr(self.service, "dm_notes") else False
        if in_dm:
            reply += "\n⚠️ " + t("cmd.reprocess_dm_moving" if used is Stage.DELIVER else "cmd.reprocess_dm_how", lang)
        return reply

    def _cmd_project(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        if len(args) < 2:
            return t("cmd.usage", lang, usage=f"/{cmd} project <id> <project>")
        meeting = self._find(args[0], caller)
        if meeting is None:
            return t("cmd.not_found", lang, id=args[0])
        name = " ".join(args[1:])
        try:
            chosen = self.service.set_project(meeting.id, name)
        except LookupError:
            names = ", ".join(c.name for c in self.service.candidates(meeting)) or "-"
            return t("cmd.project_unknown", lang, project=name, candidates=names)
        return t("cmd.project_saved", lang, id=meeting.id, project=chosen.name)

    def _cmd_link(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        m = _MENTION_RE.match(args[0]) if args else None
        user_id = m.group(1) if m else (args[0] if args and is_ascii_digits(args[0]) else None)
        if user_id is None or len(args) < 2:
            return t("cmd.usage", lang, usage=f"/{cmd} link @user <linear-email-or-name>")
        target = " ".join(args[1:])
        self.service.link(self._space, user_id, target)
        return t("cmd.link_saved", lang, discord_id=user_id, linear=target)
