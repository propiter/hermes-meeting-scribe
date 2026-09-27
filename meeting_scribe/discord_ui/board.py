"""The task board of one meeting (DESIGN §16): items + where each goes + what happened to it.

Built from SQLite (items, Kanban/Linear deliveries, learned map) and the guild snapshot; pure
enough to run in a worker thread once the snapshot has been taken on the loop.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from ..config import Settings
from ..domain.models import ActionItem, Meeting, Notes
from ..domain.names import clean_channel_name, similarity
from .render_tasks import TaskView
from .routing import ChannelInfo, RouteContext, route_item

_LINEAR_ID_RE = re.compile(r"/issue/([A-Za-z0-9]+-\d+)")
MOVE_OPTIONS = 25


@dataclass(frozen=True)
class Board:
    meeting: Meeting
    views: tuple[TaskView, ...]
    channels: tuple[ChannelInfo, ...]

    def view(self, item_id: str) -> Optional[TaskView]:
        return next((v for v in self.views if v.item.id == item_id), None)


def _refs(repo: Any, meeting_id: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    prefix = f"mtg:{meeting_id}:"
    for sink in ("kanban", "linear"):
        for row in repo.list_deliveries(meeting_id, sink=sink, prefix=prefix):
            item_id = row["key"][len(prefix):]
            ref = str(row.get("external_id") or "")
            if sink == "linear":
                m = _LINEAR_ID_RE.search(str(row.get("url") or ""))
                ref = m.group(1) if m else ref
            if ref:
                out.setdefault(item_id, {})[sink] = ref
    return out


def items_of(repo: Any, meeting: Meeting, notes: Notes) -> list[ActionItem]:
    """The notes' items with the stored human state (status, 📁 moves) applied."""
    stored = {a.id: a for a in repo.list_action_items(meeting.id)}
    return [stored.get(a.id, a) for a in notes.action_items]


def build_board(repo: Any, settings: Settings, meeting: Meeting, notes: Notes,
                channels: Sequence[ChannelInfo]) -> Board:
    ctx = RouteContext(channel_map=settings.project_channel_map(), min_score=settings.project_match_min_score,
                       ignore_prefixes=settings.channel_name_ignore_prefixes, learned=repo.project_channel)
    refs = _refs(repo, meeting.id)
    views = tuple(TaskView(item, route_item(item, meeting, channels, ctx), refs.get(item.id, {}))
                  for item in items_of(repo, meeting, notes))
    return Board(meeting, views, tuple(channels))


def move_options(board: Board, item_id: str, ignore_prefixes: Sequence[str]) -> list[tuple[str, str]]:
    """Postable text channels as ``(id, "#name")``, most likely first (for the 📁 picker)."""
    view = board.view(item_id)
    names: Mapping[str, str] = {c.id: clean_channel_name(c.name, ignore_prefixes) or c.name
                                for c in board.channels if c.kind != "category" and c.can_post}
    wanted = [n for n in ((view.item.project, view.item.project_hint, view.route.project) if view else ()) if n]

    def score(cid: str) -> float:
        return max((similarity(w, names[cid], ignore_prefixes=ignore_prefixes) for w in wanted), default=0.0)
    order = {c.id: c.position for c in board.channels}
    ranked = sorted(names, key=lambda cid: (-score(cid), order.get(cid, 0)))
    return [(cid, f"#{names[cid]}") for cid in ranked[:MOVE_OPTIONS]]
