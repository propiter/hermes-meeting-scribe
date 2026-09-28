"""Hermes Kanban sink (DESIGN §8): OWNER action items become triage tasks.

``KanbanGateway`` is the thin seam around ``hermes_cli.kanban_db`` so unit tests fake it; the
real implementation (:class:`HermesKanban`) mirrors the verified signatures at hermes-agent
436b904e8: ``kanban_db.create_task(conn, *, title, body, created_by, triage, idempotency_key,
project_id, board)`` inside ``kanban_db_connect.connect_closing(board=...)``, and
``kanban_db.list_boards(include_archived=...)``.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from ..config import Settings
from ..domain.models import ActionItem, Meeting, Notes
from ..i18n import t
from ..storage.artifacts import fmt_ts
from .base import DeliveryStore, ItemSink, ProjectFor


class KanbanGateway(Protocol):
    def create_task(self, *, title: str, body: str, idempotency_key: str, project_id: Optional[str],
                    board: Optional[str]) -> str: ...

    def list_boards(self) -> list[dict[str, Any]]: ...


class HermesKanban:
    """Production gateway. Imports are lazy so the plugin loads where kanban is unavailable."""

    def create_task(self, *, title: str, body: str, idempotency_key: str, project_id: Optional[str],
                    board: Optional[str]) -> str:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing(board=board) as conn:
            return kb.create_task(conn, title=title, body=body, assignee=None, created_by="meeting-scribe",
                                  triage=True, idempotency_key=idempotency_key, project_id=project_id,
                                  board=board)

    def list_boards(self) -> list[dict[str, Any]]:
        from hermes_cli import kanban_db as kb

        return kb.list_boards(include_archived=False)


class KanbanSink(ItemSink):
    name = "kanban"

    def __init__(self, settings: Callable[[str], Settings], store: DeliveryStore, gateway: KanbanGateway, *,
                 owners: Callable[[str], tuple[str, ...]], project_for: ProjectFor) -> None:
        super().__init__(settings, store, project_for)
        self._gw = gateway
        self._owners = owners

    def mode(self, meeting: Meeting) -> str:
        return self._settings(meeting.space).kanban_mode

    def eligible(self, meeting: Meeting, item: ActionItem) -> bool:
        return bool(item.owner_speaker_id) and item.owner_speaker_id in self._owners(meeting.space)

    def _target(self, meeting: Meeting, notes: Notes, item: ActionItem) -> tuple[Optional[str], Optional[str]]:
        """``(board, project_id)``: a Hermes project brings its own board; a kanban candidate is a board."""
        cand = self._project_for(meeting, notes, item)
        board = self._settings(meeting.space).kanban_board or None
        if cand is not None and cand.source == "hermes":
            return (cand.ref.get("board_slug") or board), cand.ref.get("project_id")
        if cand is not None and cand.source == "kanban":
            return cand.ref.get("board") or board, None
        return board, None

    def _create(self, meeting: Meeting, notes: Notes, item: ActionItem, folder: Path,
                key: str) -> tuple[str, Optional[str]]:
        lang = notes.language or self._settings(meeting.space).ui_language
        when = meeting.started_at + timedelta(seconds=item.t0 or 0)
        if self.private(meeting):
            body = t("sink.private_item_body", lang, description=item.description or item.title,
                     date=when.date().isoformat())
        else:
            body = t("sink.kanban_task_body", lang, description=item.description or item.title,
                     quote=item.quote or "-", title=notes.meeting_title or meeting.title, date=when.date().isoformat(),
                     ts=fmt_ts(item.t0 or 0), folder=str(folder))
        board, project_id = self._target(meeting, notes, item)
        task_id = self._gw.create_task(title=item.title, body=body, idempotency_key=key, project_id=project_id,
                                       board=board)
        return str(task_id), None
