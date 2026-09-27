"""Shared sink machinery.

``ItemSink`` implements the per-action-item flow used by Kanban and Linear:
  * ``auto``: every non-dismissed item is delivered by the pipeline's deliver stage;
  * ``approve``: the pipeline only delivers items a human approved FOR THIS SINK (per-(item, sink)
    status, review finding 1 — approving for Kanban never sends to Linear); single approvals go
    through ``deliver_item`` directly (Phase B buttons call :meth:`MeetingService.approve_item`);
  * ``off``: disabled.
Idempotency (review finding 6): a delivery row is CLAIMED before the external call
(``INSERT … ON CONFLICT DO NOTHING``), so concurrent approvals create one object. A claim taken
over from a crashed/failed attempt first asks the backend whether the object already exists
(:meth:`ItemSink._reconcile`) — Kanban via its server-side idempotency key, Linear by searching
the ``mtg:…`` marker we put in the description.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional, Protocol

from ..config import Settings
from ..domain.ids import idempotency_key
from ..domain.models import ActionItem, ActionStatus, Candidate, Meeting, Notes, SinkResult
from ..storage.deliveries import Claim

class DeliveryInProgress(RuntimeError):
    """Another caller holds the claim for this ``(sink, key)`` right now (double click, overlap)."""


ProjectFor = Callable[[Meeting, Notes, ActionItem], Optional[Candidate]]


class DeliveryStore(Protocol):
    def get_delivery(self, sink: str, key: str) -> Optional[dict]: ...

    def claim_delivery(self, meeting_id: str, sink: str, key: str) -> Claim: ...

    def complete_delivery(self, sink: str, key: str, token: str, *, external_id: str, url: Optional[str]) -> None: ...

    def release_delivery(self, sink: str, key: str, token: str) -> None: ...

    def set_item_sink_status(self, meeting_id: str, item_id: str, sink: str, status: str) -> None: ...

    def item_sink_statuses(self, meeting_id: str, sink: str) -> dict[str, str]: ...

    def list_action_items(self, meeting_id: str) -> list[ActionItem]: ...

    def get_action_item(self, meeting_id: str, item_id: str) -> Optional[ActionItem]: ...

    def set_action_status(self, meeting_id: str, item_id: str, status: ActionStatus) -> None: ...


class DiscordNotesSink(Protocol):
    """Interface the Discord notes sink satisfies (``discord_ui/sink.py``).

    ``deliver`` posts TL;DR/decisions/questions/action items grouped by person (with mentions)
    in a thread under ``delivery_discord_channel`` (fallback: voice text chat → automatic channel, DESIGN §19) and
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

    def _reconcile(self, meeting: Meeting, key: str) -> Optional[tuple[str, Optional[str]]]:
        """An object an earlier (crashed/failed) attempt may have created for ``key``, if any.

        Sinks with a server-side idempotency key (Kanban) can rely on ``_create`` being idempotent
        and keep this default."""
        return None

    def deliver_item(self, meeting: Meeting, notes: Notes, item: ActionItem, folder: Path) -> str:
        key = idempotency_key(meeting.id, item.id)
        claim = self._store.claim_delivery(meeting.id, self.name, key)
        if claim.kind == "done":
            assert claim.row is not None
            self._mark_delivered(meeting.id, item.id)
            return str(claim.row["external_id"])
        if claim.kind == "busy":
            raise DeliveryInProgress(f"{self.name} delivery of {item.id} is already in progress")
        assert claim.token is not None
        try:
            found = self._reconcile(meeting, key) if claim.kind == "takeover" else None
            external_id, url = found or self._create(meeting, notes, item, folder, key)
        except BaseException:
            self._store.release_delivery(self.name, key, claim.token)  # kept pending: next try reconciles
            raise
        self._store.complete_delivery(self.name, key, claim.token, external_id=external_id, url=url)
        self._mark_delivered(meeting.id, item.id)
        return external_id

    def _mark_delivered(self, meeting_id: str, item_id: str) -> None:
        self._store.set_item_sink_status(meeting_id, item_id, self.name, "delivered")
        if self._store.get_action_item(meeting_id, item_id):
            self._store.set_action_status(meeting_id, item_id, ActionStatus.DELIVERED)  # display summary

    def approved_for_me(self, meeting_id: str) -> set[str]:
        return {i for i, st in self._store.item_sink_statuses(meeting_id, self.name).items()
                if st in ("approved", "delivered")}

    def deliver(self, meeting: Meeting, notes: Notes, folder: Path) -> SinkResult:
        mode = self.mode()
        stored = {a.id: a for a in self._store.list_action_items(meeting.id)}
        mine = self.approved_for_me(meeting.id) if mode == "approve" else set()
        delivered: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        for item in notes.action_items:
            current = stored.get(item.id, item)
            wanted = current.status is not ActionStatus.DISMISSED and (mode != "approve" or current.id in mine)
            if not wanted or not self.eligible(current):
                skipped.append(item.id)
                continue
            try:
                delivered.append(self.deliver_item(meeting, notes, current, folder))
            except Exception as exc:  # one bad item must not block the others; reported upstream
                errors.append(f"{item.id}: {type(exc).__name__}: {exc}")
        return SinkResult(self.name, not errors, tuple(delivered), tuple(skipped), tuple(errors))
