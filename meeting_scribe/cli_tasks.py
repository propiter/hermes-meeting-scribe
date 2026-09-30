"""``hermes meeting-scribe task list|assign|undo|history`` (DESIGN §16.2): who a meeting's tasks belong
to, changed by the operator at this machine (an owner's rights; audited as ``cli``). Discord shows the
change — the card edited in place, the new assignee's DM panel, one mention — when the gateway's worker
next runs."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .cli_spaces import add_space_arg, selected
from .i18n import t
from .pipeline.task_assign import Actor, TaskAssignError, assign_error, assign_reply


def _print(text: str) -> None:
    sys.stdout.write(text.rstrip("\n") + "\n")


def add_parser(sub: Any) -> None:
    sp = sub.add_parser("task", help="A meeting's tasks: list them, assign one, undo an assignment")
    ss = sp.add_subparsers(dest="task_command")
    ls = ss.add_parser("list", help="The meeting's tasks with id and assignee")
    ls.add_argument("meeting_id")
    ls.add_argument("--json", action="store_true")
    add_space_arg(ls)
    asg = ss.add_parser("assign", help="Give a task to someone (the card in Discord is edited in place)")
    asg.add_argument("meeting_id")
    asg.add_argument("task_id")
    asg.add_argument("who", metavar="USER", help="A participant's name, a Discord user id or <@id>, me (the only "
                                                 "owner of the space), or none")
    add_space_arg(asg)
    und = ss.add_parser("undo", help="Put back who had the task before its last assignment")
    und.add_argument("meeting_id")
    und.add_argument("task_id")
    add_space_arg(und)
    his = ss.add_parser("history", help="Every assignment of the meeting's tasks: who, when, from whom to whom")
    his.add_argument("meeting_id")
    his.add_argument("--json", action="store_true")
    add_space_arg(his)


def _me(rt: Any, space: str, lang: str) -> str:
    """``me`` at the terminal: the space's owner, when there is exactly one (the operator)."""
    owners = [str(o) for o in rt.owners(space)]
    if len(owners) != 1:
        raise TaskAssignError("cli_me", ", ".join(owners))
    return owners[0]


def dispatch(args: argparse.Namespace, rt: Any) -> int:
    lang = rt.settings().ui_language
    command = getattr(args, "task_command", None)
    if command not in ("list", "assign", "undo", "history"):
        _print("usage: hermes meeting-scribe task list|assign|undo|history")
        return 2
    service = rt.service()
    meeting = service.find(args.meeting_id, selected(args, rt, action=command in ("assign", "undo")))
    if meeting is None:
        _print(t("cmd.not_found", lang, id=args.meeting_id))
        return 1
    lang = rt.settings(meeting.space).ui_language
    if command == "list":
        items = service.repo.list_action_items(meeting.id)
        if args.json:
            _print(json.dumps([{**a.to_dict(), "status": a.status.value} for a in items], ensure_ascii=False))
        for a in [] if args.json else items:
            who = f"{a.owner_name or a.owner_speaker_id} ({a.owner_speaker_id})" if a.owner_speaker_id else \
                t("notes.unassigned", lang)
            _print(f"{a.id}: {a.title} → {who} [{a.status.value}]")
        return 0
    if command == "history":
        rows = service.repo.task_history(meeting.id)
        if args.json:
            _print(json.dumps(rows, ensure_ascii=False))
        for h in [] if args.json else rows:
            undone = " (undone)" if h["undone"] else ""
            _print(f"{h['at']} {h['item_id']}: {h['previous_user'] or '-'} → {h['next_user'] or '-'} "
                   f"by {h['actor']}{undone}")
        return 0
    actor = Actor("cli", local=True)
    try:
        item = service.resolve_task(meeting, args.task_id)
        if command == "undo":
            done = service.undo_task_assignment(meeting.id, item.id, actor)
        else:
            who = _me(rt, meeting.space, lang) if args.who.strip().lower() in ("me", "yo") else args.who
            done = service.assign_task(meeting.id, item.id, who, actor)
    except TaskAssignError as exc:
        _print(assign_error(exc, lang))
        return 2 if exc.code in ("unknown_person", "unknown_task", "cli_me") else 1
    _print(assign_reply(done, lang, actor.user_id))
    return 0
