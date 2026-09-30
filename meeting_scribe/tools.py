"""Agent tools (DESIGN §11, §16.3), toolset ``meeting_scribe``. Handlers return JSON strings and never
raise: the agent reads ``{"error": ...}`` and can recover (e.g. by searching first).

``meeting_search``/``meeting_get``/``meeting_task_list`` read; ``meeting_task_assign`` and
``meeting_task_send`` act AS the Discord user whose message started the turn (:class:`Caller`, read from
Hermes' session context), with the same rules as the task card's buttons — never as an administrator.
Without a Discord user in the session (the Hermes CLI/TUI, a cron job, another platform) they refuse:
the operator has ``hermes meeting-scribe task assign`` and the Desktop."""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Optional

from .pipeline.service import MeetingService
from .privacy import Reader
from .storage.artifacts import fmt_ts, read_notes, read_transcript

TOOLSET = "meeting_scribe"
PARTS = ("notes", "transcript", "tasks", "meta")
TARGETS = ("linear", "kanban")
_TASK_REF = {"type": "string", "maxLength": 80,
             "description": ("The task id (from meeting_task_list), or the id of the Discord message that shows the "
                             "task card when the user replied to it.")}

SCHEMAS: dict[str, dict[str, Any]] = {
    "meeting_search": {
        "name": "meeting_search",
        "description": ("Full-text search over recorded meeting transcripts. Use it to answer questions "
                        "like 'what did we decide about SMTP?'. Returns matching utterances with meeting id, "
                        "speaker and timestamp; follow up with meeting_get for notes or context."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Words to search for (all must match)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
            "required": ["query"]},
    },
    "meeting_get": {
        "name": "meeting_get",
        "description": ("Get a recorded meeting by id (or unique id prefix): structured notes (default), "
                        "the transcript, the action items with their status, or metadata."),
        "parameters": {"type": "object", "properties": {
            "meeting_id": {"type": "string", "description": "Meeting id or unique prefix."},
            "part": {"type": "string", "enum": list(PARTS), "default": "notes"}},
            "required": ["meeting_id"], "additionalProperties": False},
    },
    "meeting_task_list": {
        "name": "meeting_task_list",
        "description": ("List a meeting's tasks with their ids, assignee, status and whether they are in Linear/Kanban. "
                        "Use the ids with meeting_task_assign / meeting_task_send."),
        "parameters": {"type": "object", "properties": {
            "meeting_id": {"type": "string", "maxLength": 40, "description": "Meeting id or unique prefix."}},
            "required": ["meeting_id"], "additionalProperties": False},
    },
    "meeting_task_assign": {
        "name": "meeting_task_assign",
        "description": ("Assign a meeting task AS the Discord user who is talking to you: assignee 'me' takes an "
                        "unassigned task for them; 'none' releases their own task; another user (id or <@id>) "
                        "only works if that Discord user is an owner. The plugin checks who is asking — never "
                        "claim permissions on the user's behalf. If the user replied to a task card, pass that "
                        "message's id as message_id instead of task_id."),
        "parameters": {"type": "object", "properties": {
            "meeting_id": {"type": "string", "maxLength": 40,
                           "description": "Meeting id or unique prefix (optional with message_id)."},
            "task_id": _TASK_REF,
            "message_id": {"type": "string", "maxLength": 30,
                           "description": "Id of the Discord message showing the task card (a reply to it)."},
            "assignee": {"type": "string", "maxLength": 80,
                         "description": "'me', 'none', or a Discord user id / <@id> mention."}},
            "required": ["assignee"], "additionalProperties": False},
    },
    "meeting_task_send": {
        "name": "meeting_task_send",
        "description": ("Create a meeting task in Linear or in the Hermes Kanban board AS the Discord user who is "
                        "talking to you (the same as pressing the task card's button): its assignee or an owner "
                        "may send it; Kanban only takes the owners' own tasks. Only the destinations configured "
                        "for the meeting's space work. Pass message_id instead of task_id when the user replied "
                        "to a task card."),
        "parameters": {"type": "object", "properties": {
            "meeting_id": {"type": "string", "maxLength": 40,
                           "description": "Meeting id or unique prefix (optional with message_id)."},
            "task_id": _TASK_REF,
            "message_id": {"type": "string", "maxLength": 30,
                           "description": "Id of the Discord message showing the task card (a reply to it)."},
            "target": {"type": "string", "enum": list(TARGETS)}},
            "required": ["target"], "additionalProperties": False},
    },
}
SCHEMAS["meeting_search"]["parameters"]["additionalProperties"] = False


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _session_guild() -> str:
    """The Discord server of the agent's chat ("" in a DM, outside a gateway or on other platforms)."""
    try:
        from .commands import caller_from_session

        return caller_from_session().guild_id
    except Exception:  # no Hermes gateway session (CLI, tests)
        return ""


def _session_caller() -> Optional[Any]:
    """The Hermes session of this turn (``None`` outside Hermes: nobody is known)."""
    try:
        from .commands import caller_from_session

        return caller_from_session()
    except ImportError:  # outside Hermes
        return None


def _session_reader() -> Reader:
    """Who the agent answers (DESIGN §19.2): a chat sees a private meeting only from its private
    channel, the Hermes CLI/TUI and Desktop see everything, and a cron job or an unknown context sees
    none (the operator reads them with ``hermes meeting-scribe show``)."""
    try:
        from .commands import caller_from_session

        return caller_from_session().reader
    except ImportError:  # outside Hermes: nobody is known, so no private meeting
        return Reader()


class MeetingTools:
    """Both tools act in ONE space (DESIGN §23): the one owning the chat's server, else the only
    space; with several spaces and no server they answer an error instead of another team's data.
    A private meeting (DESIGN §19.2) does not exist for them outside its private channel."""

    def __init__(self, service: Callable[[], MeetingService], max_utterances: int = 400,
                 guild: Callable[[], str] = _session_guild, reader: Callable[[], Reader] = _session_reader,
                 caller: Callable[[], Optional[Any]] = _session_caller,
                 owners: Callable[[str], Any] = lambda space: ()) -> None:
        self._service = service
        self._max = max_utterances
        self._guild = guild
        self._reader = reader
        self._caller = caller
        self._owners = owners

    def _space(self, service: MeetingService) -> str:
        return service.space_for(self._guild() or None)

    def search(self, args: Mapping[str, Any], **_: Any) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return _json({"error": "query is required"})
        try:
            limit = max(1, min(int(args.get("limit") or 10), 50))
            service = self._service()
            hits = service.search(query, self._space(service), limit, reader=self._reader())
        except Exception as exc:  # tool contract: JSON error, never an exception
            return _json({"error": f"{type(exc).__name__}: {exc}"})
        return _json({"results": [{**h, "ts": fmt_ts(h["t0"])} for h in hits]})

    def get(self, args: Mapping[str, Any], **_: Any) -> str:
        part = str(args.get("part") or "notes")
        if part not in PARTS:
            return _json({"error": f"part must be one of {', '.join(PARTS)}"})
        try:
            service = self._service()
            meeting = service.find(str(args.get("meeting_id") or ""), self._space(service))
            if meeting is not None and not service.readable(meeting, self._reader()):
                meeting = None  # same answer as an unknown id: its existence is not revealed
            if meeting is None:
                return _json({"error": f"no meeting {args.get('meeting_id')!r}; use meeting_search"})
            folder = service.folder(meeting)
            out: dict[str, Any] = {"meeting": {"id": meeting.id, "title": meeting.title, "state": meeting.state.value,
                                               "started_at": meeting.started_at.isoformat(),
                                               "channel": meeting.channel_name, "project": meeting.project,
                                               "folder": str(folder)}}
            if part == "notes":
                notes = read_notes(folder)
                out["notes"] = notes.to_dict() if notes else None
            elif part == "transcript":
                utts = read_transcript(folder)
                out["transcript"] = [{"ts": fmt_ts(u.t0), "speaker": u.speaker, "text": u.text}
                                     for u in utts[: self._max]]
                out["truncated"] = len(utts) > self._max
            elif part == "tasks":
                out["tasks"] = [{**a.to_dict(), "status": a.status.value}
                                for a in service.repo.list_action_items(meeting.id)]
            else:
                out["meta"] = meeting.to_dict()
            return _json(out)
        except Exception as exc:  # tool contract
            return _json({"error": f"{type(exc).__name__}: {exc}"})

    # -- tasks (DESIGN §16.3) -----------------------------------------------------------------------
    def _lang(self, service: MeetingService) -> str:
        try:
            return service.settings(self._space(service)).ui_language
        except Exception:  # an unknown space: the error itself is reported by the caller
            return service.settings().ui_language

    def _meeting(self, service: MeetingService, args: Mapping[str, Any]) -> Any:
        """The meeting named by ``meeting_id``, or by the card ``message_id`` points to — in this chat's
        space, and only if this chat may read it (else the same answer as an unknown id)."""
        from .pipeline.task_assign import find_card

        space = self._space(service)
        ref = str(args.get("meeting_id") or "").strip()
        card = find_card(service.repo, str(args.get("message_id") or "")) if args.get("message_id") else None
        meeting = service.find(ref, space) if ref else None
        if meeting is None and card is not None:
            meeting = service.find(card.meeting_id, space)
        if meeting is not None and card is not None and card.meeting_id != meeting.id:
            meeting = None
        if meeting is not None and not service.readable(meeting, self._reader()):
            meeting = None
        return meeting, card

    def task_list(self, args: Mapping[str, Any], **_: Any) -> str:
        try:
            service = self._service()
            meeting, _card = self._meeting(service, args)
            if meeting is None:
                return _json({"error": f"no meeting {args.get('meeting_id')!r}; use meeting_search"})
            from .pipeline.task_assign import repo_delivered

            tasks = [{"id": a.id, "title": a.title, "assignee": a.owner_speaker_id, "assignee_name": a.owner_name,
                      "status": a.status.value, "due": a.due, "project": a.project,
                      "sent_to": [s for s in TARGETS if repo_delivered(service.repo, meeting.id, a.id, s)]}
                     for a in service.repo.list_action_items(meeting.id)]
            return _json({"meeting": {"id": meeting.id, "title": meeting.title}, "tasks": tasks})
        except Exception as exc:  # tool contract
            return _json({"error": f"{type(exc).__name__}: {exc}"})

    def _who(self, service: MeetingService, meeting: Any) -> Any:
        """What this turn proves about the person asking (DESIGN §16.3), or ``None``: nobody is known.
        Only a Discord turn names a person; Hermes answered it, so Hermes authorizes that person. They
        "see" the task where they are chatting: in one of the channels/threads holding the meeting's notes
        or cards (a private meeting: only its private channel — :meth:`Reader.may_read`)."""
        from . import privacy
        from .domain.models import is_discord_user_id
        from .pipeline.task_assign import Actor, meeting_places

        caller = self._caller()
        if caller is None or caller.cron or (caller.platform or "").lower() != "discord":
            return None
        uid = str(caller.user_id or "")
        if not is_discord_user_id(uid) or not caller.chat_id:
            return None
        places = {str(x) for x in (caller.chat_id, caller.thread_id, caller.parent_chat_id) if x}
        sees = bool(places & meeting_places(service.repo, meeting.id))
        if privacy.is_dm(service.repo, service.settings(meeting.space), meeting):
            sees = privacy.dm_copies(service.repo, meeting.id).get(uid) in places  # their own copy only
        elif service.is_private(meeting):
            sees = service.readable(meeting, caller.reader)
        owners = {str(o) for o in self._owners(meeting.space)}
        return Actor(uid, admin=uid in owners, authorized=True, sees=sees)

    def _task(self, service: MeetingService, args: Mapping[str, Any]) -> tuple[Any, Any, Any, str]:
        """``(meeting, item, actor, lang)`` of a write tool; raises what the agent should read."""
        from .pipeline.task_assign import TaskAssignError

        meeting, card = self._meeting(service, args)
        if meeting is None:
            raise LookupError(f"no meeting {args.get('meeting_id') or args.get('message_id')!r}; use meeting_search")
        actor = self._who(service, meeting)
        if actor is None:
            raise TaskAssignError("no_identity")
        ref = str(args.get("task_id") or "") or (card.item_id if card is not None else "")
        return meeting, service.resolve_task(meeting, ref), actor, service.settings(meeting.space).ui_language

    def task_assign(self, args: Mapping[str, Any], **_: Any) -> str:
        from .pipeline.task_assign import TaskAssignError, assign_reply

        service = self._service()
        lang = "en"
        try:
            lang = self._lang(service)
            meeting, item, actor, lang = self._task(service, args)
            done = service.assign_task(meeting.id, item.id, str(args.get("assignee") or ""), actor)
            return _json({"ok": True, "changed": done.changed, "meeting_id": meeting.id, "task_id": item.id,
                          "assignee": done.user, "sinks": done.sinks,
                          "message": assign_reply(done, lang, actor.user_id)})
        except TaskAssignError as exc:
            return _json({"error": _assign_error(exc, lang), "code": exc.code})
        except Exception as exc:  # tool contract
            return _json({"error": f"{type(exc).__name__}: {exc}"})

    def task_send(self, args: Mapping[str, Any], **_: Any) -> str:
        """The 🟣 Linear / ✅ Kanban button, pressed by the person asking (``discord_ui.auth`` rules)."""
        from .discord_ui.actions import friendly_error
        from .discord_ui.auth import check_task
        from .i18n import t
        from .pipeline.task_assign import TaskAssignError, queue_refresh

        service = self._service()
        lang = "en"
        target = str(args.get("target") or "")
        if target not in TARGETS:
            return _json({"error": f"target must be one of {', '.join(TARGETS)}"})
        try:
            lang = self._lang(service)
            meeting, item, actor, lang = self._task(service, args)
            if not actor.sees and service.is_private(meeting):
                raise TaskAssignError("private_only")
            refusal = self._dm_refusal(service, meeting, item, actor, lang)
            if refusal:
                return _json({"error": refusal})
            verdict = check_task("ok" if target == "kanban" else "lin", item, actor.user_id,
                                 frozenset(str(o) for o in self._owners(meeting.space)), lang, item.id)
            if not verdict.allowed:
                return _json({"error": verdict.message})
            ref = service.approve_item(meeting.id, item.id, target)
            queue_refresh(service.repo, meeting.id, item.id)  # the card shows the result (an edit, no ping)
            name = "Linear" if target == "linear" else "Kanban"
            return _json({"ok": True, "meeting_id": meeting.id, "task_id": item.id, "target": target, "ref": ref,
                          "message": t("ui.approved", lang, sink=name, ref=ref)})
        except TaskAssignError as exc:
            return _json({"error": _assign_error(exc, lang), "code": exc.code})
        except LookupError as exc:  # no such meeting here: say how to find it
            return _json({"error": str(exc).strip("'\"")})
        except Exception as exc:  # tool contract: the same plain words as the button
            return _json({"error": friendly_error(exc, lang)})

    @staticmethod
    def _dm_refusal(service: MeetingService, meeting: Any, item: Any, actor: Any, lang: str) -> str:
        """A direct-messages meeting (DESIGN §19.3): only from the asker's own copy, on their own task."""
        from . import privacy
        from .i18n import t

        if not privacy.is_dm(service.repo, service.settings(meeting.space), meeting):
            return ""
        if not actor.sees or str(item.owner_speaker_id or "") != actor.user_id:
            return t("dm.only_own_tasks", lang)
        return ""


def _assign_error(exc: Any, lang: str) -> str:
    from .pipeline.task_assign import assign_error

    return assign_error(exc, lang)
