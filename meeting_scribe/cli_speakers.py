"""``hermes meeting-scribe speaker list|assign`` (DESIGN §4.1): the "unidentified participant" tracks of
a meeting, and giving one to its owner after the meeting."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .cli_spaces import add_space_arg, selected
from .i18n import t
from .pipeline.speakers import AssignError
from .storage.artifacts import fmt_ts


def _print(text: str) -> None:
    sys.stdout.write(text.rstrip("\n") + "\n")


def add_parser(sub: Any) -> None:
    sp = sub.add_parser("speaker", help="Unidentified participant tracks: list them, assign one to its owner")
    ss = sp.add_subparsers(dest="speaker_command")
    ls = ss.add_parser("list", help="A meeting's unidentified tracks: interval, lines, owner")
    ls.add_argument("meeting_id")
    ls.add_argument("--json", action="store_true")
    add_space_arg(ls)
    asg = ss.add_parser("assign", help="Give an unidentified track to a participant (re-publishes in place)")
    asg.add_argument("meeting_id")
    asg.add_argument("label", metavar="unidentified-N")
    asg.add_argument("who", metavar="PARTICIPANT", help="A participant's name, user id or @id")
    add_space_arg(asg)


def error_text(exc: AssignError, lang: str) -> str:
    return t(f"speakers.error_{exc.code}", lang, detail=exc.detail)


def track_line(track: Any, lang: str) -> str:
    span = (f"{fmt_ts(track.first)}–{fmt_ts(track.last)}" if track.first is not None
            else t("speakers.no_lines", lang))
    owner = f" → {track.name} ({track.owner})" if track.owner else ""
    return f"{track.label}: {t('speakers.lines', lang, count=track.lines)}, {span}{owner}"


def dispatch(args: argparse.Namespace, rt: Any) -> int:
    lang = rt.settings().ui_language
    command = getattr(args, "speaker_command", None)
    if command not in ("list", "assign"):
        _print("usage: hermes meeting-scribe speaker list|assign")
        return 2
    service = rt.service()
    meeting = service.find(args.meeting_id, selected(args, rt, action=command == "assign"))
    if meeting is None:
        _print(t("cmd.not_found", lang, id=args.meeting_id))
        return 1
    if command == "list":
        tracks = service.speaker_tracks(meeting)
        if args.json:
            _print(json.dumps([tr.to_dict() for tr in tracks], ensure_ascii=False))
        elif not tracks:
            _print(t("speakers.none", lang))
        else:
            _print(t("speakers.one_person_each", lang))
            for tr in tracks:
                _print("  " + track_line(tr, lang))
        return 0
    try:
        done = service.assign_speaker(meeting.id, args.label, args.who)
    except AssignError as exc:
        _print(error_text(exc, lang))
        return 2 if exc.code in ("unknown_person", "not_unidentified", "unknown_track") else 1
    if not done.changed:
        _print(t("speakers.already", lang, label=done.label, name=done.name))
        return 0
    _print(t("speakers.assigned", lang, label=done.label, name=done.name, lines=done.lines, tasks=done.tasks))
    if done.redeliver:
        _print(t("speakers.redelivering", lang))
    return 0
