"""Deliveries and per-(item, sink) decisions (DESIGN §8, §15 — review findings 1 and 6).

``deliveries`` rows are the idempotency ledger. A delivery is CLAIMED before the external call:
``INSERT … state='pending' ON CONFLICT DO NOTHING`` — only the caller whose insert landed (or who
takes over an abandoned pending row) talks to the external system; everyone else sees ``busy`` or
the finished row. A takeover means an earlier attempt may have created the object without
recording it, so the sink must reconcile (look the object up by its idempotency marker) first.

``item_sinks`` records human decisions PER SINK: approving an item for Kanban says nothing about
Linear. The global ``action_items.status`` stays a display summary (✅ once delivered anywhere).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .result import Result

PENDING_STALE_SECONDS = 600.0  # a claim older than this is abandoned (crash mid-create)


@dataclass(frozen=True)
class Claim:
    """Outcome of :meth:`DeliveriesMixin.claim_delivery`.

    ``kind``: ``new`` (we own a fresh pending row), ``takeover`` (we own an abandoned one —
    reconcile first), ``done`` (already delivered; ``row`` has it), ``busy`` (someone else is
    creating it right now).
    """

    kind: str
    token: Optional[str] = None
    row: Optional[dict[str, Any]] = None


class DeliveriesMixin:
    """Mixed into :class:`~meeting_scribe.storage.repo.Repository` (needs ``_x``)."""

    def _x(self, sql: str, params: Sequence[Any] = ()) -> Result:  # pragma: no cover - provided
        raise NotImplementedError

    # -- ledger ---------------------------------------------------------------------------------
    def get_delivery(self, sink: str, key: str) -> Optional[dict[str, Any]]:
        """The FINISHED delivery for ``(sink, key)``; pending claims are not deliveries."""
        row = self._x("SELECT * FROM deliveries WHERE sink=? AND key=? AND state='done'", (sink, key)).fetchone()
        return dict(row) if row else None

    def record_delivery(self, meeting_id: str, sink: str, key: str, *, external_id: Optional[str],
                        url: Optional[str]) -> None:
        """First write wins: a finished delivery is never overwritten (a pending claim is completed)."""
        self._x("INSERT INTO deliveries (meeting_id, sink, key, external_id, url, created_at, state)"
                " VALUES (?,?,?,?,?,?, 'done') ON CONFLICT(sink, key) DO UPDATE SET external_id=excluded.external_id,"
                " url=excluded.url, state='done', claim=NULL WHERE deliveries.state='pending'",
                (meeting_id, sink, key, external_id, url, time.time()))

    def upsert_delivery(self, meeting_id: str, sink: str, key: str, *, external_id: Optional[str],
                        url: Optional[str]) -> None:
        """Mutable pointer (e.g. the Discord notes message that later reprocesses edit in place)."""
        self._x("INSERT INTO deliveries (meeting_id, sink, key, external_id, url, created_at, state)"
                " VALUES (?,?,?,?,?,?, 'done') ON CONFLICT(sink, key) DO UPDATE SET external_id=excluded.external_id,"
                " url=excluded.url, state='done', claim=NULL", (meeting_id, sink, key, external_id, url, time.time()))

    def list_deliveries(self, meeting_id: str, *, sink: Optional[str] = None,
                        prefix: Optional[str] = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM deliveries WHERE meeting_id=? AND state='done'", [meeting_id]
        if sink is not None:
            sql, params = sql + " AND sink=?", params + [sink]
        if prefix is not None:
            sql, params = sql + " AND substr(key, 1, ?)=?", params + [len(prefix), prefix]
        return [dict(r) for r in self._x(sql + " ORDER BY id", params).fetchall()]

    def find_task_cards(self, message_id: str) -> list[dict[str, Any]]:
        """The Discord task messages (``mtg:<meeting>:task:<item>`` and a participant's direct-messages
        copy ``mtg:<meeting>:pdm:<user>:task:<item>``) whose message id is ``message_id``."""
        rows = self._x("SELECT meeting_id, key, external_id FROM deliveries WHERE sink='discord' AND state='done'"
                       " AND key LIKE 'mtg:%task:%' AND json_valid(external_id)"
                       " AND CAST(json_extract(external_id, '$.message') AS TEXT)=?", (str(message_id),))
        return [dict(r) for r in rows.fetchall()]

    def delete_delivery(self, sink: str, key: str) -> None:
        """Drop a mutable pointer (a Discord message that no longer exists)."""
        self._x("DELETE FROM deliveries WHERE sink=? AND key=?", (sink, key))

    # -- claims ---------------------------------------------------------------------------------
    def claim_delivery(self, meeting_id: str, sink: str, key: str, *,
                       stale_after: float = PENDING_STALE_SECONDS) -> Claim:
        token = uuid.uuid4().hex
        now = time.time()
        cur = self._x("INSERT INTO deliveries (meeting_id, sink, key, created_at, state, claim, claimed_at)"
                      " VALUES (?,?,?,?, 'pending', ?, ?) ON CONFLICT(sink, key) DO NOTHING",
                      (meeting_id, sink, key, now, token, now))
        if cur.rowcount == 1:
            return Claim("new", token)
        cur = self._x("UPDATE deliveries SET claim=?, claimed_at=? WHERE sink=? AND key=? AND state='pending'"
                      " AND (claim IS NULL OR claimed_at IS NULL OR claimed_at < ?)",
                      (token, now, sink, key, now - stale_after))
        if cur.rowcount == 1:
            return Claim("takeover", token)
        done = self.get_delivery(sink, key)
        return Claim("done", row=done) if done else Claim("busy")

    def complete_delivery(self, sink: str, key: str, token: str, *, external_id: str, url: Optional[str]) -> None:
        self._x("UPDATE deliveries SET state='done', external_id=?, url=?, claim=NULL, created_at=?"
                " WHERE sink=? AND key=? AND claim=?", (external_id, url, time.time(), sink, key, token))

    def release_delivery(self, sink: str, key: str, token: str) -> None:
        """Give a failed claim back WITHOUT forgetting it: the external call may have succeeded
        (e.g. a timeout after the server created the issue), so the next claimer takes it over
        and reconciles before creating."""
        self._x("UPDATE deliveries SET claim=NULL WHERE sink=? AND key=? AND claim=? AND state='pending'",
                (sink, key, token))

    # -- per-sink decisions ---------------------------------------------------------------------
    def set_item_sink_status(self, meeting_id: str, item_id: str, sink: str, status: str) -> None:
        """``approved`` or ``delivered``; ``delivered`` is never downgraded to ``approved``."""
        self._x("INSERT INTO item_sinks (meeting_id, item_id, sink, status, updated_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(meeting_id, item_id, sink) DO UPDATE SET status=excluded.status,"
                " updated_at=excluded.updated_at WHERE item_sinks.status != 'delivered'",
                (meeting_id, item_id, sink, status, time.time()))

    def item_sink_status(self, meeting_id: str, item_id: str, sink: str) -> Optional[str]:
        row = self._x("SELECT status FROM item_sinks WHERE meeting_id=? AND item_id=? AND sink=?",
                      (meeting_id, item_id, sink)).fetchone()
        return str(row["status"]) if row else None

    def item_sink_statuses(self, meeting_id: str, sink: str) -> dict[str, str]:
        rows = self._x("SELECT item_id, status FROM item_sinks WHERE meeting_id=? AND sink=?", (meeting_id, sink))
        return {r["item_id"]: r["status"] for r in rows.fetchall()}
