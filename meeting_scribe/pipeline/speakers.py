"""Give an "unidentified participant" track to its owner after the meeting (DESIGN §4.1).

Each ``unidentified-N`` is ONE SSRC, i.e. one Discord voice connection, i.e. one person whose voice
the bot could not prove (no SPEAKING, no DAVE key). Capture already gives it to its owner at the
close when exactly one person in the call had no voice of their own while it talked; otherwise an
administrator or someone who was in the meeting assigns it here (CLI, Desktop, Discord button).

Assigning rewrites what the meeting says, never the audio: transcript lines (``transcript.jsonl``,
``transcript.md``, the index), the tasks owned by the track, the speaker list and ``missing_audio``.
The mapping is kept (``speakers.assigned.<meeting>``) so a later ``reprocess from=transcribe``, which
reads the track files again, names the lines the same way. A published meeting is then re-delivered:
every message is edited in place (nothing duplicated; an edit notifies nobody). Assigning the same
track to the same person again changes nothing.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional, Sequence

from ..domain.models import (UNIDENTIFIED_PREFIX, ActionItem, Meeting, MeetingState, Speaker, Stage, Utterance,
                             is_unidentified)
from ..domain.names import match_person
from ..storage.artifacts import read_notes, read_transcript, write_notes, write_transcript, write_transcript_md

log = logging.getLogger(__name__)
ASSIGNED_KV = "speakers.assigned."  # + meeting id -> {"unidentified-N": {"user", "lines", "first", "last"}}


class AssignError(ValueError):
    """Why a track cannot be assigned, in plain words (``code`` for the surfaces that translate)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Track:
    label: str
    name: str
    lines: int
    first: Optional[float]  # seconds since the meeting started, from the transcript
    last: Optional[float]
    owner: Optional[str] = None  # the user id it was assigned to
    suggested_user: Optional[str] = None
    suggestion_name: str = ""
    suggestion_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "name": self.name, "lines": self.lines, "first": self.first,
                "last": self.last, "owner": self.owner, "suggested_user": self.suggested_user,
                "suggestion_name": self.suggestion_name, "suggestion_reason": self.suggestion_reason}


@dataclass(frozen=True)
class Assigned:
    label: str
    user_id: str
    name: str
    lines: int
    tasks: int
    changed: bool  # False: it was already theirs (idempotent)
    redeliver: bool


def _records(repo: Any, meeting_id: str) -> dict[str, dict[str, Any]]:
    raw = repo.kv_get(ASSIGNED_KV + meeting_id)
    return dict(json.loads(raw)) if raw else {}


def assignments(repo: Any, meeting_id: str) -> dict[str, str]:
    """``{"unidentified-N": user id}`` of the tracks assigned after the meeting."""
    return {label: str(rec["user"]) for label, rec in _records(repo, meeting_id).items()}


def relabel(utterances: Sequence[Utterance], mapping: Mapping[str, str],
            names: Mapping[str, str]) -> list[Utterance]:
    """The lines of each assigned track under its owner (the transcriber names them by track file)."""
    return [replace(u, speaker_id=mapping[u.speaker_id], speaker=names.get(mapping[u.speaker_id], u.speaker))
            if u.speaker_id in mapping else u for u in utterances]


def candidates(meeting: Meeting) -> list[Speaker]:
    """Who a track may be given to: the humans of the meeting that are people, not tracks."""
    return [s for s in meeting.human_speakers if not is_unidentified(s.user_id)]


def tracks(repo: Any, folder: Any, meeting: Meeting) -> list[Track]:
    """Every unidentified track of the meeting — still open or already assigned — with its interval
    and how many transcript lines it has."""
    done = _records(repo, meeting.id)
    utts = read_transcript(folder)
    names = {s.user_id: s.name for s in meeting.speakers}
    out = [Track(label, names.get(label, label), *_span([u for u in utts if u.speaker_id == label]))
           for label in names if is_unidentified(label) and label not in done]
    out += [Track(label, names.get(rec["user"], rec["user"]), int(rec["lines"]), rec["first"], rec["last"],
                  str(rec["user"])) for label, rec in done.items()]
    speakers = {s.user_id: s for s in meeting.speakers}
    out = [replace(tr, suggested_user=speakers[tr.label].suggested_user,
                   suggestion_name=names.get(speakers[tr.label].suggested_user, ""),
                   suggestion_reason=speakers[tr.label].suggestion_reason)
           if tr.label in speakers and not tr.owner else tr for tr in out]
    return sorted(out, key=lambda tr: int(tr.label[len(UNIDENTIFIED_PREFIX):]))


def _span(lines: Sequence[Utterance]) -> tuple[int, Optional[float], Optional[float]]:
    return (len(lines), min((u.t0 for u in lines), default=None), max((u.t1 for u in lines), default=None))


def resolve(meeting: Meeting, who: str) -> Speaker:
    """``who``: a user id, ``@id`` / ``<@id>``, or a name of one of :func:`candidates`."""
    people = candidates(meeting)
    raw = str(who or "").strip()
    uid = raw.strip("<@!>") if raw.startswith(("@", "<@")) else raw
    by_id = {s.user_id: s for s in people}
    if uid in by_id:
        return by_id[uid]
    key = match_person(raw.lstrip("@"), [(s.user_id, (s.name, *s.aliases)) for s in people])
    if key is None:
        raise AssignError("unknown_person", raw)
    return by_id[key]


def assign(service: Any, meeting_id: str, label: str, who: str) -> Assigned:
    """Give ``label``'s voice to ``who``; see the module docstring."""
    repo = service.repo
    meeting = service.require(meeting_id)
    if not is_unidentified(label):
        raise AssignError("not_unidentified", label)
    if meeting.state is MeetingState.RECORDING:
        raise AssignError("recording")
    job = repo.get_job(meeting.id)
    if job is not None and job.state == "running":
        raise AssignError("busy")
    person = resolve(meeting, who)
    done = assignments(repo, meeting.id)
    if label in done:
        if done[label] != person.user_id:
            raise AssignError("already_assigned", done[label])
        return Assigned(label, person.user_id, person.name, 0, 0, False, False)
    if label not in {s.user_id for s in meeting.speakers}:
        raise AssignError("unknown_track", label)
    folder = service.folder(meeting)
    utts = read_transcript(folder)
    moved, first, last = _span([u for u in utts if u.speaker_id == label])
    record = {"user": person.user_id, "lines": moved, "first": first, "last": last}
    repo.kv_set(ASSIGNED_KV + meeting.id, json.dumps({**_records(repo, meeting.id), label: record}, sort_keys=True))
    lang = meeting.language or service.settings(meeting.space).ui_language
    meeting = replace(meeting, speakers=tuple(s for s in meeting.speakers if s.user_id != label),
                      missing_audio=tuple(u for u in meeting.missing_audio if u != person.user_id))
    if moved:
        utts = relabel(utts, {label: person.user_id}, {person.user_id: person.name})
        write_transcript(folder, utts)
        write_transcript_md(folder, meeting, utts, lang)
        repo.replace_utterances(meeting.id, utts)
    tasks = _reassign_tasks(repo, folder, meeting, label, person, lang)
    repo.delete_speaker(meeting.id, label)
    service.runner.stages.persist(meeting)
    redeliver = meeting.state in (MeetingState.DONE, MeetingState.FAILED) and read_notes(folder) is not None
    if redeliver:
        service.reprocess(meeting.id, Stage.DELIVER)
    log.info("meeting-scribe %s: %s assigned to %s (%s): %d line(s), %d task(s)%s", meeting.id, label,
             person.name, person.user_id, moved, tasks, "; re-delivering" if redeliver else "")
    return Assigned(label, person.user_id, person.name, moved, tasks, True, redeliver)


def _reassign_tasks(repo: Any, folder: Any, meeting: Meeting, label: str, person: Speaker, lang: str) -> int:
    notes = read_notes(folder)
    if notes is None:
        return 0

    def mine(item: ActionItem) -> ActionItem:
        return (replace(item, owner_speaker_id=person.user_id, owner_name=person.name)
                if item.owner_speaker_id == label else item)

    count = sum(1 for a in notes.action_items if a.owner_speaker_id == label)
    if not count:
        return 0
    write_notes(folder, meeting, replace(notes, action_items=tuple(mine(a) for a in notes.action_items)), lang)
    for item in repo.list_action_items(meeting.id):
        if item.owner_speaker_id == label:
            repo.update_action_item(meeting.id, mine(item))
    return count
