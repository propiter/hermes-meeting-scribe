"""Agent tools (DESIGN §11, §16.3), toolset ``meeting_scribe``. Handlers return JSON strings and never
raise: the agent reads ``{"error": ...}`` and can recover (e.g. by searching first).

``meeting_search``/``meeting_get``/``meeting_task_list`` read; ``meeting_task_assign`` and
``meeting_task_send`` act AS the Discord user whose message started the turn (:class:`Caller`, read from
Hermes' session context), with the same rules as the task card's buttons — never as an administrator —
but only in a conversation that is that user's alone (``Caller.per_user_session``). In a conversation
several people share (a thread, by default) the turn's identity may be someone else's, so they post the
change for confirmation instead and whoever presses ✅ makes it as themselves (``pipeline.task_proposals``).
Without a Discord conversation (the Hermes CLI/TUI, a cron job, another platform) they refuse: the
operator has ``hermes meeting-scribe task assign`` and the Desktop. Results never carry a live mention."""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping, Optional

from .pipeline.service import MeetingService
from .privacy import Reader
from .storage.artifacts import fmt_ts, read_notes, read_transcript

TOOLSET = "meeting_scribe"
#: ``post(chat_id, text, meeting_id, proposal_id, lang)`` -> the id of the Discord message it posted with
#: the ✅ Confirm / ✖ Cancel buttons (``None``: not posted) — ``discord_ui.proposer_for``.
Poster = Callable[[str, str, str, str, str], Optional[str]]
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
                        "claim permissions on the user's behalf. When the user's message replies to a task card, "
                        "task_id and meeting_id can be omitted: the card is found from the reply."),
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
                        "for the meeting's space work. When the user's message replies to a task card, task_id "
                        "and meeting_id can be omitted."),
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


_MENTION = re.compile(r"<@[!&]?([0-9]+)>")


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def inert(value: Any, names: Mapping[str, str]) -> Any:
    """``value`` with every ``<@id>`` in its strings replaced by the person's visible name (or an inert
    ``@id``). A task tool's result is relayed by the model into the chat, where Hermes' Discord adapter
    lets user mentions notify: the card already carries the ONE mention of an assignment (DESIGN §16.2),
    a tool result never adds another."""
    from .discord_ui.render import safe_name

    if isinstance(value, str):
        return _MENTION.sub(lambda m: safe_name(names.get(m[1]) or "") or "@\u200b" + m[1], value)
    if isinstance(value, dict):
        return {k: inert(v, names) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [inert(v, names) for v in value]
    return value


def _names(service: MeetingService, meeting: Any) -> dict[str, str]:
    """Discord id -> visible name of the people a task tool may talk about: the space's person links, the
    meeting's people and whoever its tasks name."""
    from .privacy import people

    out = {str(link["discord_user_id"]): str(link.get("name") or "") for link in service.repo.list_links(meeting.space)
           if link.get("name")}
    out.update({a.owner_speaker_id: a.owner_name for a in service.repo.list_action_items(meeting.id)
                if a.owner_speaker_id and a.owner_name})
    out.update({uid: name for uid, name in people(service.repo, meeting) if uid and name})
    return out


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
                 owners: Callable[[str], Any] = lambda space: (),
                 replied_to: Callable[[], Optional[Callable[[str, str], Optional[str]]]] = lambda: None,
                 proposer: Callable[[], Optional[Poster]] = lambda: None) -> None:
        self._service = service
        self._max = max_utterances
        self._guild = guild
        self._reader = reader
        self._caller = caller
        self._owners = owners
        self._replied_to = replied_to
        self._proposer = proposer

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
        message = str(args.get("message_id") or "") or ("" if args.get("task_id") else self._reply_target())
        card = find_card(service.repo, message) if message else None
        meeting = service.find(ref, space) if ref else None
        if meeting is None and card is not None:
            meeting = service.find(card.meeting_id, space)
        if meeting is not None and card is not None and card.meeting_id != meeting.id:
            meeting = None
        if meeting is not None and not service.readable(meeting, self._reader()):
            meeting = None
        return meeting, card

    def _reply_target(self) -> str:
        """The message the user's Discord message replies to (a task card, in the case this is for)."""
        caller = self._caller()
        lookup = self._replied_to()
        if caller is None or lookup is None or (caller.platform or "").lower() != "discord" or not caller.message_id:
            return ""
        chat = caller.thread_id or caller.chat_id
        return lookup(str(chat), str(caller.message_id)) or ""

    def task_list(self, args: Mapping[str, Any], **_: Any) -> str:
        try:
            service = self._service()
            meeting, _card = self._meeting(service, args)
            if meeting is None:
                return _out({"error": f"no meeting {args.get('meeting_id')!r}; use meeting_search"})
            from .pipeline.task_assign import repo_delivered

            tasks = [{"id": a.id, "title": a.title, "assignee": a.owner_speaker_id, "assignee_name": a.owner_name,
                      "status": a.status.value, "due": a.due, "project": a.project,
                      "sent_to": [s for s in TARGETS if repo_delivered(service.repo, meeting.id, a.id, s)]}
                     for a in service.repo.list_action_items(meeting.id)]
            return _out({"meeting": {"id": meeting.id, "title": meeting.title}, "tasks": tasks},
                        _names(service, meeting))
        except Exception as exc:  # tool contract
            return _out({"error": f"{type(exc).__name__}: {exc}"})

    def _who(self, service: MeetingService, meeting: Any) -> Any:
        """What this turn proves about the person asking (DESIGN §16.3), or ``None``: nobody is known.
        Only a Discord turn in a conversation that is THIS person's alone names them (a shared one goes
        through a confirmation instead, :meth:`_shared`); Hermes answered it, so Hermes authorizes that
        person. They "see" the task where they are chatting: in one of the channels/threads holding the
        meeting's notes or cards (a private meeting: only its private channel — :meth:`Reader.may_read`)."""
        from . import privacy
        from .domain.models import is_discord_user_id
        from .pipeline.task_assign import Actor, meeting_places

        caller = self._caller()
        if not _discord_chat(caller) or caller.delegated or not caller.per_user_session:
            return None
        uid = str(caller.user_id or "")
        if not is_discord_user_id(uid):
            return None
        places = {str(x) for x in (caller.chat_id, caller.thread_id, caller.parent_chat_id) if x}
        sees = bool(places & meeting_places(service.repo, meeting.id))
        if privacy.is_dm(service.repo, service.settings(meeting.space), meeting):
            sees = privacy.dm_copies(service.repo, meeting.id).get(uid) in places  # their own copy only
        elif service.is_private(meeting):
            sees = service.readable(meeting, caller.reader)
        owners = {str(o) for o in self._owners(meeting.space)}
        return Actor(uid, admin=uid in owners, authorized=True, sees=sees)

    def _shared(self) -> bool:
        """A Discord conversation whose session identity may not be the asker's (DESIGN §16.3): a thread or
        channel several people share, or a subagent working for someone. Writes there need a confirmation."""
        caller = self._caller()
        return _discord_chat(caller) and (caller.delegated or not caller.per_user_session)

    def _task(self, service: MeetingService, args: Mapping[str, Any]) -> tuple[Any, Any, str]:
        """``(meeting, item, lang)`` of a write tool; raises what the agent should read."""
        from .pipeline.task_assign import TaskAssignError

        meeting, card = self._meeting(service, args)
        if meeting is None and not (args.get("meeting_id") or args.get("message_id")):
            raise TaskAssignError("which_task")
        if meeting is None:
            raise TaskAssignError("unknown_meeting", str(args.get("meeting_id") or args.get("message_id")))
        ref = str(args.get("task_id") or "") or (card.item_id if card is not None else "")
        return meeting, service.resolve_task(meeting, ref), service.settings(meeting.space).ui_language

    def _actor(self, service: MeetingService, meeting: Any) -> Any:
        from .pipeline.task_assign import TaskAssignError

        actor = self._who(service, meeting)
        if actor is None:
            raise TaskAssignError("no_identity")
        return actor

    def task_assign(self, args: Mapping[str, Any], **_: Any) -> str:
        from .pipeline.task_assign import ME, NOBODY, TaskAssignError, assign_reply, person

        service = self._service()
        lang, names = "en", {}
        try:
            lang = self._lang(service)
            meeting, item, lang = self._task(service, args)
            names = _names(service, meeting)
            who = str(args.get("assignee") or "").strip()
            if self._shared():
                shown = ("" if who.lower() in ME | NOBODY else person(service.repo, meeting, who)[1])
                return _out(self._propose(service, meeting, item, "assign", who, shown, lang), names)
            actor = self._actor(service, meeting)
            done = service.assign_task(meeting.id, item.id, who, actor)
            return _out({"ok": True, "changed": done.changed, "meeting_id": meeting.id, "task_id": item.id,
                         "assignee": done.user, "sinks": done.sinks,
                         "message": assign_reply(done, lang, actor.user_id)}, names)
        except TaskAssignError as exc:
            return _out({"error": _assign_error(exc, lang), "code": exc.code}, names)
        except Exception as exc:  # tool contract
            return _out({"error": f"{type(exc).__name__}: {exc}"}, names)

    def task_send(self, args: Mapping[str, Any], **_: Any) -> str:
        """The 🟣 Linear / ✅ Kanban button, pressed by the person asking (``discord_ui.auth`` rules)."""
        from .discord_ui.actions import friendly_error
        from .discord_ui.auth import check_task
        from .i18n import t
        from .pipeline.task_assign import TaskAssignError, queue_refresh

        service = self._service()
        lang, names = "en", {}
        target = str(args.get("target") or "")
        if target not in TARGETS:
            return _out({"error": f"target must be one of {', '.join(TARGETS)}"})
        try:
            lang = self._lang(service)
            meeting, item, lang = self._task(service, args)
            names = _names(service, meeting)
            if self._shared():
                return _out(self._propose(service, meeting, item, "send", target, "", lang), names)
            actor = self._actor(service, meeting)
            if not actor.sees and service.is_private(meeting):
                raise TaskAssignError("private_only")
            refusal = self._dm_refusal(service, meeting, item, actor, lang)
            if refusal:
                return _out({"error": refusal}, names)
            verdict = check_task("ok" if target == "kanban" else "lin", item, actor.user_id,
                                 frozenset(str(o) for o in self._owners(meeting.space)), lang, item.id)
            if not verdict.allowed:
                return _out({"error": verdict.message}, names)
            ref = service.approve_item(meeting.id, item.id, target)
            queue_refresh(service.repo, meeting.id, item.id)  # the card shows the result (an edit, no ping)
            name = "Linear" if target == "linear" else "Kanban"
            return _out({"ok": True, "meeting_id": meeting.id, "task_id": item.id, "target": target, "ref": ref,
                         "message": t("ui.approved", lang, sink=name, ref=ref)}, names)
        except TaskAssignError as exc:
            return _out({"error": _assign_error(exc, lang), "code": exc.code}, names)
        except Exception as exc:  # tool contract: the same plain words as the button
            return _out({"error": friendly_error(exc, lang)}, names)

    def _propose(self, service: MeetingService, meeting: Any, item: Any, kind: str, arg: str, shown: str,
                 lang: str) -> dict[str, Any]:
        """Post the change in the chat with ✅ Confirm / ✖ Cancel instead of making it (DESIGN §16.3): whoever
        presses ✅ makes it, as themselves. Only where this chat already sees the meeting (``_meeting`` read it
        through the same privacy gate), never for a direct-messages meeting (it has no shared chat)."""
        import time

        from . import privacy
        from .discord_ui.render import safe_name
        from .i18n import t
        from .pipeline import task_proposals
        from .pipeline.task_assign import NOBODY, SINK_NAMES, TaskAssignError

        if privacy.is_dm(service.repo, service.settings(meeting.space), meeting):
            raise TaskAssignError("dm_meeting")
        poster = self._proposer()
        caller = self._caller()
        if poster is None:
            raise TaskAssignError("confirm_unavailable")
        what = (SINK_NAMES[arg] if kind == "send" else safe_name(shown) if shown
                else t("propose.nobody" if arg.lower() in NOBODY else "propose.who_confirms", lang))
        chat = str(caller.thread_id or caller.chat_id)
        proposal = task_proposals.create(service.repo, time.time(), meeting.id, item.id, kind, arg.strip(), chat,
                                         str(caller.user_id or ""))
        text = task_proposals.describe(kind, safe_name(item.title), meeting.id, what, lang)
        message = poster(chat, text, meeting.id, proposal.id, lang)
        if not message:
            raise TaskAssignError("confirm_unavailable")
        service.repo.set_task_proposal_message(proposal.id, str(message))
        return {"status": "pending_confirmation", "code": "pending_confirmation", "meeting_id": meeting.id,
                "task_id": item.id, "proposal_id": proposal.id, "expires_in_minutes": task_proposals.TTL_SECONDS // 60,
                "message": t("propose.pending", lang, minutes=task_proposals.TTL_SECONDS // 60)}

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


def _discord_chat(caller: Any) -> bool:
    """A Discord conversation turn (not the CLI/TUI, a cron job, another platform or nothing bound)."""
    return (caller is not None and not caller.cron and (caller.platform or "").lower() == "discord"
            and bool(caller.chat_id))


def _out(obj: Any, names: Optional[Mapping[str, str]] = None) -> str:
    """A task tool's JSON result, with no live mention in it (:func:`inert`)."""
    return _json(inert(obj, names or {}))


def _assign_error(exc: Any, lang: str) -> str:
    from .pipeline.task_assign import assign_error

    return assign_error(exc, lang)
