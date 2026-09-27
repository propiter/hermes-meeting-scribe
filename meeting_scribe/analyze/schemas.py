"""JSON Schemas for structured extraction (DESIGN §7, §15).

Hermes VALIDATES ``complete_structured`` output against the schema we send (jsonschema) and raises
on any violation, so a strict schema turns one extra ``priority`` field or a ``"t0": "12:30"`` into
a failed analyze stage (review finding 8). The schemas therefore describe the shape we *want* but
only require what we cannot do without (item ``title``); everything else is optional, nullable and
open to extra properties — :mod:`extract` normalises every variant. ``HermesStructuredLLM`` also
retries once with plain JSON mode and no schema if validation still fails.
"""
from __future__ import annotations

from typing import Any

_NULLABLE_STR: dict[str, Any] = {"type": ["string", "null"]}
_NULLABLE_NUM: dict[str, Any] = {"type": ["number", "string", "null"]}
_ACTION_ITEM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": _NULLABLE_STR,
        "owner_speaker_id": {"type": ["string", "integer", "null"]},
        "owner_name": _NULLABLE_STR,
        "due": _NULLABLE_STR,
        "project": _NULLABLE_STR,
        "project_confidence": _NULLABLE_NUM,
        "quote": _NULLABLE_STR,
        "t0": _NULLABLE_NUM,
    },
    "required": ["title"],
}
_TOPIC: dict[str, Any] = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "points": {"type": ["array", "null"], "items": {"type": "string"}}},
    "required": ["title"],
}
_STR_LIST: dict[str, Any] = {"type": ["array", "null"], "items": {"type": "string"}}
_ITEMS: dict[str, Any] = {"type": ["array", "null"], "items": _ACTION_ITEM}

CHUNK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "topics": {"type": ["array", "null"], "items": _TOPIC},
                   "decisions": _STR_LIST, "open_questions": _STR_LIST, "action_items": _ITEMS},
    "required": ["summary", "action_items"],
}

NOTES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "meeting_title": {"type": "string"}, "tldr": {"type": "string"}, "summary": {"type": "string"},
        "topics": {"type": ["array", "null"], "items": _TOPIC}, "decisions": _STR_LIST, "open_questions": _STR_LIST,
        "action_items": _ITEMS, "language": _NULLABLE_STR,
        "project": _NULLABLE_STR, "project_confidence": _NULLABLE_NUM,
    },
    "required": ["meeting_title", "tldr", "summary", "action_items"],
}
