"""Moving a task to another project channel (the 📁 button, DESIGN §16).

The move is a human correction, so it is authoritative for THAT task: it is pinned with an item
override that survives re-analysis. Only an owner's move (``learn=True``) also TEACHES routing for
everyone (the name the task was filed under and the channel's own name map to the chosen channel):
an assignee may move their own task but must not re-route other people's tasks sharing that name.
The item keeps its id (idempotency keys and per-sink decisions survive).
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..domain.models import ActionItem, Meeting
from ..domain.text import fold
from ..storage.artifacts import read_notes, write_notes


def apply_move(repo: Any, folder: Path, meeting: Meeting, item_id: str, channel_id: str, name: str, *,
               learn: bool = True) -> ActionItem:
    item = repo.get_action_item(meeting.id, item_id)
    if item is None:
        raise KeyError(item_id)
    if learn:
        for learned in {item.project, item.project_hint, name}:
            if learned and fold(learned):
                repo.learn_project_channel(meeting.space, learned, channel_id)
    moved = replace(item, project=name, project_key=f"discord:{channel_id}", project_confidence=1.0)
    repo.set_item_override(meeting.id, item_id, project=name, project_key=moved.project_key)
    repo.update_action_item(meeting.id, moved)
    notes = read_notes(folder)
    if notes is not None:
        items = tuple(replace(moved, status=a.status) if a.id == item_id else a for a in notes.action_items)
        write_notes(folder, meeting, replace(notes, action_items=items), notes.language or "en")
    return moved
