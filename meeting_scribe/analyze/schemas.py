"""JSON Schemas for structured extraction (DESIGN §7). Kept permissive on nullability because
providers differ in how strictly they honour ``response_format``; ``extract`` re-validates."""
from __future__ import annotations

from typing import Any

_NULLABLE_STR: dict[str, Any] = {"type": ["string", "null"]}
_ACTION_ITEM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "owner_speaker_id": _NULLABLE_STR,
        "owner_name": _NULLABLE_STR,
        "due": _NULLABLE_STR,
        "project": _NULLABLE_STR,
        "project_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "quote": {"type": "string"},
        "t0": {"type": ["number", "null"]},
    },
    "required": ["title", "description", "owner_speaker_id", "owner_name", "due", "project",
                 "project_confidence", "quote", "t0"],
    "additionalProperties": False,
}
_TOPIC: dict[str, Any] = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "points": {"type": "array", "items": {"type": "string"}}},
    "required": ["title", "points"],
    "additionalProperties": False,
}
_STR_LIST: dict[str, Any] = {"type": "array", "items": {"type": "string"}}

CHUNK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "topics": {"type": "array", "items": _TOPIC},
                   "decisions": _STR_LIST, "open_questions": _STR_LIST,
                   "action_items": {"type": "array", "items": _ACTION_ITEM}},
    "required": ["summary", "topics", "decisions", "open_questions", "action_items"],
    "additionalProperties": False,
}

NOTES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "meeting_title": {"type": "string"}, "tldr": {"type": "string"}, "summary": {"type": "string"},
        "topics": {"type": "array", "items": _TOPIC}, "decisions": _STR_LIST, "open_questions": _STR_LIST,
        "action_items": {"type": "array", "items": _ACTION_ITEM}, "language": {"type": "string"},
        "project": _NULLABLE_STR, "project_confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["meeting_title", "tldr", "summary", "topics", "decisions", "open_questions",
                 "action_items", "language", "project", "project_confidence"],
    "additionalProperties": False,
}
