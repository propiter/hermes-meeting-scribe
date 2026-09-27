"""Moving a task to another project channel (the 📁 button, DESIGN §16).

The move is a human correction, so it is authoritative and it TEACHES routing: the name the task
was filed under (the LLM's project or the spoken hint) now maps to the chosen channel, and so does
the channel's own name. The item keeps its id (idempotency keys and per-sink decisions survive).
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..domain.models import ActionItem, Meeting
from ..domain.text import fold
from ..storage.artifacts import read_notes, write_notes


def apply_move(repo: Any, folder: Path, meeting: Meeting, item_id: str, channel_id: str, name: str) -> ActionItem:
    item = repo.get_action_item(meeting.id, item_id)
    if item is None:
        raise KeyError(item_id)
    for learned in {item.project, item.project_hint, name}:
        if learned and fold(learned):
            repo.learn_project_channel(learned, channel_id)
    moved = replace(item, project=name, project_key=f"discord:{channel_id}", project_confidence=1.0)
    repo.update_action_item(meeting.id, moved)
    notes = read_notes(folder)
    if notes is not None:
        items = tuple(replace(moved, status=a.status) if a.id == item_id else a for a in notes.action_items)
        write_notes(folder, meeting, replace(notes, action_items=items), notes.language or "en")
    return moved
