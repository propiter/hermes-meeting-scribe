"""Shared sink machinery.

``ItemSink`` implements the per-action-item flow used by Kanban and Linear:
  * ``auto``: every non-dismissed item is delivered by the pipeline's deliver stage;
  * ``approve``: the pipeline only delivers items a human already approved (e.g. "Approve all",
    or approvals made before a reprocess); single approvals go through ``deliver_item`` directly
    (Phase B buttons call :meth:`MeetingService.approve_item`);
  * ``off``: disabled.
Idempotency: a ``(sink, mtg:<meeting>:<item>)`` row in ``deliveries`` means "already created";
the external API also receives the key where it supports one (Kanban ``idempotency_key``).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional, Protocol

from ..config import Settings
from ..domain.ids import idempotency_key
from ..domain.models import ActionItem, ActionStatus, Candidate, Meeting, Notes, SinkResult

ProjectFor = Callable[[Meeting, Notes, ActionItem], Optional[Candidate]]


class DeliveryStore(Protocol):
    def get_delivery(self, sink: str, key: str) -> Optional[dict]: ...

    def record_delivery(self, meeting_id: str, sink: str, key: str, *, external_id: Optional[str],
                        url: Optional[str]) -> None: ...

    def list_action_items(self, meeting_id: str) -> list[ActionItem]: ...

    def get_action_item(self, meeting_id: str, item_id: str) -> Optional[ActionItem]: ...

    def set_action_status(self, meeting_id: str, item_id: str, status: ActionStatus) -> None: ...


class DiscordNotesSink(Protocol):
    """Interface the Phase B Discord sink satisfies (``sinks/discord_notes.py``).

    ``deliver`` posts TL;DR/decisions/questions/action items grouped by person (with mentions)
    in a thread under ``delivery.discord.channel`` (fallback: voice text chat → home channel) and
    records ``(sink="discord", key="mtg:<id>:notes")`` with the message/thread URL so reprocess
    edits instead of re-posting. It must run the coroutine on the gateway loop and return a
    ``SinkResult`` synchronously (the pipeline thread is not an event loop).
    """

    name: str

    def enabled(self) -> bool: ...

    def deliver(self, meeting: Meeting, notes: Notes, folder: Path) -> SinkResult: ...


class ItemSink(ABC):
    name: str = ""

    def __init__(self, settings: Callable[[], Settings], store: DeliveryStore, project_for: ProjectFor) -> None:
        self._settings = settings
        self._store = store
        self._project_for = project_for

    @abstractmethod
    def mode(self) -> str: ...

    def active(self) -> bool:
        """Backend connected (independent of mode)."""
        return True

    def enabled(self) -> bool:
        return self.mode() != "off" and self.active()

    def eligible(self, item: ActionItem) -> bool:
        return True

    @abstractmethod
    def _create(self, meeting: Meeting, notes: Notes, item: ActionItem, folder: Path,
                key: str) -> tuple[str, Optional[str]]:
        """Create the external object; return ``(external_id, url)``."""

    def deliver_item(self, meeting: Meeting, notes: Notes, item: ActionItem, folder: Path) -> str:
        key = idempotency_key(meeting.id, item.id)
        existing = self._store.get_delivery(self.name, key)
        if existing:
            return str(existing["external_id"])
        external_id, url = self._create(meeting, notes, item, folder, key)
        self._store.record_delivery(meeting.id, self.name, key, external_id=external_id, url=url)
        if self._store.get_action_item(meeting.id, item.id):
            self._store.set_action_status(meeting.id, item.id, ActionStatus.DELIVERED)
        return external_id

    def deliver(self, meeting: Meeting, notes: Notes, folder: Path) -> SinkResult:
        mode = self.mode()
        wanted = {ActionStatus.APPROVED, ActionStatus.DELIVERED} if mode == "approve" else {
            ActionStatus.PENDING, ActionStatus.APPROVED, ActionStatus.DELIVERED}
        stored = {a.id: a for a in self._store.list_action_items(meeting.id)}
        delivered: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        for item in notes.action_items:
            current = stored.get(item.id, item)
            if current.status not in wanted or not self.eligible(current):
                skipped.append(item.id)
                continue
            try:
                delivered.append(self.deliver_item(meeting, notes, current, folder))
            except Exception as exc:  # one bad item must not block the others; reported upstream
                errors.append(f"{item.id}: {type(exc).__name__}: {exc}")
        return SinkResult(self.name, not errors, tuple(delivered), tuple(skipped), tuple(errors))
