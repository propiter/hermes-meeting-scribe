"""Agent tools (DESIGN §11), toolset ``meeting_scribe``. Handlers return JSON strings and never
raise: the agent reads ``{"error": ...}`` and can recover (e.g. by searching first)."""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from .pipeline.service import MeetingService
from .storage.artifacts import fmt_ts, read_notes, read_transcript

TOOLSET = "meeting_scribe"
PARTS = ("notes", "transcript", "tasks", "meta")

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
            "required": ["meeting_id"]},
    },
}


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


class MeetingTools:
    def __init__(self, service: Callable[[], MeetingService], max_utterances: int = 400) -> None:
        self._service = service
        self._max = max_utterances

    def search(self, args: Mapping[str, Any], **_: Any) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return _json({"error": "query is required"})
        try:
            limit = max(1, min(int(args.get("limit") or 10), 50))
            hits = self._service().search(query, limit)
        except Exception as exc:  # tool contract: JSON error, never an exception
            return _json({"error": f"{type(exc).__name__}: {exc}"})
        return _json({"results": [{**h, "ts": fmt_ts(h["t0"])} for h in hits]})

    def get(self, args: Mapping[str, Any], **_: Any) -> str:
        part = str(args.get("part") or "notes")
        if part not in PARTS:
            return _json({"error": f"part must be one of {', '.join(PARTS)}"})
        try:
            service = self._service()
            meeting = service.find(str(args.get("meeting_id") or ""))
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
