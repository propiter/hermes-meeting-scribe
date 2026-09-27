"""``/meeting`` router (DESIGN §3), platform-agnostic.

Hermes calls ``handler(raw_args) -> str``; the caller identity comes from
``gateway.session_context.get_session_env`` (see :func:`caller_from_session`). ``start``/``stop``
are delegated to the Phase B capture controller; without one they answer honestly that live
recording is unavailable instead of pretending.
"""
from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Union

from .config import PRIMARY_COMMAND, Settings
from .domain.models import Stage
from .domain.text import is_ascii_digits
from .i18n import t
from .pipeline.service import MeetingService
from .storage.artifacts import fmt_ts, read_notes, render_notes_md

log = logging.getLogger(__name__)
_MENTION_RE = re.compile(r"^<@!?([0-9]+)>$")


@dataclass(frozen=True)
class Caller:
    platform: str
    chat_id: str
    user_id: str
    thread_id: str = ""


def caller_from_session() -> Caller:
    from gateway.session_context import get_session_env

    return Caller(platform=get_session_env("HERMES_SESSION_PLATFORM"), chat_id=get_session_env("HERMES_SESSION_CHAT_ID"),
                  user_id=get_session_env("HERMES_SESSION_USER_ID"), thread_id=get_session_env("HERMES_SESSION_THREAD_ID"))


class CaptureController(Protocol):
    """Implemented by Phase B (``meeting_scribe.capture``). Both return the chat reply."""

    def start(self, caller: Caller, target: Optional[str]) -> str: ...

    def stop(self, caller: Caller) -> str: ...

    def live_meeting_ids(self) -> set[str]: ...


class MeetingCommands:
    def __init__(self, service: Union[MeetingService, Callable[[], MeetingService]], settings: Callable[[], Settings],
                 capture: Callable[[], Optional[CaptureController]]) -> None:
        # A callable is resolved per command (review finding 7): the runtime may switch to another
        # profile's database, and a captured instance would keep answering from the old one.
        self._service = service if callable(service) else (lambda: service)
        self.settings = settings
        self.capture = capture

    @property
    def service(self) -> MeetingService:
        return self._service()

    def handle(self, raw_args: str, caller: Caller, invoked_as: str = PRIMARY_COMMAND) -> str:
        lang = self.settings().ui_language
        try:
            parts = shlex.split(raw_args or "")
        except ValueError:
            parts = (raw_args or "").split()
        sub, args = (parts[0].lower(), parts[1:]) if parts else ("start", [])
        handler = getattr(self, f"_cmd_{sub}", None)
        if handler is None:
            return t("cmd.unknown", lang, sub=sub)
        try:
            return handler(args, caller, lang, invoked_as)
        except Exception as exc:  # a slash command must always answer
            log.exception("meeting-scribe /%s %s failed", invoked_as, sub)
            return t("cmd.error", lang, error=f"{type(exc).__name__}: {exc}")

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

    def _cmd_status(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        st = self.service.status()
        lines = [t("cmd.status_idle", lang, queued=st["queued"])]
        for row in st["recent"]:
            job = row["job"] or {}
            extra = t("cmd.status_failed_extra", lang, stage=job.get("failed_stage"), attempts=job.get("attempts"),
                      error=(job.get("error") or "")[:160]) if job.get("error") else ""
            lines.append(t("cmd.status_line", lang, id=row["id"], title=row["title"], state=row["state"], extra=extra))
        return "\n".join(lines)

    def _cmd_list(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        n = int(args[0]) if args and is_ascii_digits(args[0]) else 10
        meetings = self.service.repo.list_meetings(limit=max(1, min(n, 50)))
        if not meetings:
            return t("cmd.list_empty", lang)
        return "\n".join(t("cmd.list_line", lang, id=m.id, date=f"{m.started_at:%Y-%m-%d %H:%M}",
                            title=m.title or m.channel_name, state=m.state.value) for m in meetings)

    def _cmd_show(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        if not args:
            return t("cmd.usage", lang, usage=f"/{cmd} show <id>")
        meeting = self.service.find(args[0])
        if meeting is None:
            return t("cmd.not_found", lang, id=args[0])
        notes = read_notes(self.service.folder(meeting))
        if notes is None:
            return t("cmd.status_line", lang, id=meeting.id, title=meeting.title, state=meeting.state.value, extra="")
        body = render_notes_md(meeting, notes, notes.language or lang)
        return body.split("---\n", 2)[-1].strip()[:1900]

    def _cmd_search(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        query = " ".join(args)
        if not query:
            return t("cmd.usage", lang, usage=f"/{cmd} search <text>")
        hits = self.service.search(query, limit=8)
        if not hits:
            return t("cmd.search_empty", lang, query=query)
        return "\n".join(t("cmd.search_line", lang, id=h["meeting_id"], ts=fmt_ts(h["t0"]), speaker=h["speaker"],
                            text=h["text"][:200]) for h in hits)

    def _cmd_config(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        return "\n".join(t("cmd.config_line", lang, key=k, value=", ".join(v) if isinstance(v, tuple) else v)
                         for k, v in self.settings().as_dict().items())

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
        meeting = self.service.find(args[0])
        if meeting is None:
            return t("cmd.not_found", lang, id=args[0])
        used = self.service.effective_stage(meeting, stage)
        self.service.reprocess(meeting.id, stage)
        reply = t("cmd.reprocess_queued", lang, id=meeting.id, stage=used.value)
        reply += "\n" + t("cmd.reprocess_no_audio", lang) if used is not stage else ""
        hint = self.service.dm_notes().get(meeting.id) if hasattr(self.service, "dm_notes") else None
        return reply + (f"\n⚠️ {hint}" if hint else "")

    def _cmd_project(self, args: list[str], caller: Caller, lang: str, cmd: str) -> str:
        if len(args) < 2:
            return t("cmd.usage", lang, usage=f"/{cmd} project <id> <project>")
        meeting = self.service.find(args[0])
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
        self.service.link(user_id, target)
        return t("cmd.link_saved", lang, discord_id=user_id, linear=target)
