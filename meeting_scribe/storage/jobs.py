"""Job queue rows with leases (DESIGN §9, §15 — review finding 2).

A worker that claims a job stamps ``owner`` + ``heartbeat`` and refreshes the heartbeat while the
job runs (transcription can take hours inside one stage). ``requeue_stale`` only takes back jobs
whose lease expired — a second process opening the database can no longer steal the gateway's
running job.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

from .result import Result
from ..domain.models import Stage


@dataclass(frozen=True)
class Job:
    id: int
    meeting_id: str
    stage: Stage
    state: str  # queued | running | failed | done
    attempts: int
    failed_stage: Optional[Stage]
    error: Optional[str]
    next_retry_at: Optional[float]
    owner: Optional[str] = None
    heartbeat: Optional[float] = None


def _ts(value: Optional[datetime]) -> Optional[float]:
    return value.timestamp() if value is not None else None


class JobsMixin:
    """Mixed into :class:`~meeting_scribe.storage.repo.Repository` (needs ``_x``)."""

    def _x(self, sql: str, params: Sequence[Any] = ()) -> Result:  # pragma: no cover - provided
        raise NotImplementedError

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        keys = row.keys()
        return Job(id=row["id"], meeting_id=row["meeting_id"], stage=Stage(row["stage"]), state=row["state"],
                   attempts=row["attempts"],
                   failed_stage=Stage(row["failed_stage"]) if row["failed_stage"] else None,
                   error=row["error"], next_retry_at=row["next_retry_at"],
                   owner=row["owner"] if "owner" in keys else None,
                   heartbeat=row["heartbeat"] if "heartbeat" in keys else None)

    def enqueue_job(self, meeting_id: str, stage: Stage, *, now: datetime, reset_attempts: bool = False) -> None:
        """One job row per meeting (the pipeline is sequential per meeting); re-enqueue updates it."""
        ts = _ts(now)
        reset = ", attempts=0, error=NULL, failed_stage=NULL" if reset_attempts else ""
        self._x("INSERT INTO jobs (meeting_id, stage, state, next_retry_at, created_at, updated_at)"
                " VALUES (?,?, 'queued', ?, ?, ?) ON CONFLICT(meeting_id) DO UPDATE SET stage=excluded.stage,"
                " state='queued', owner=NULL, heartbeat=NULL, next_retry_at=excluded.next_retry_at,"
                f" updated_at=excluded.updated_at{reset}",
                (meeting_id, stage.value, ts, ts, ts))

    def next_job(self, *, now: datetime) -> Optional[Job]:
        row = self._x("SELECT * FROM jobs WHERE state='queued' AND (next_retry_at IS NULL OR next_retry_at <= ?)"
                      " ORDER BY next_retry_at, id LIMIT 1", (_ts(now),)).fetchone()
        return self._job(row) if row else None

    def claim_next_job(self, *, now: datetime, owner: Optional[str],
                       skip_stages: Sequence[Stage] = ()) -> Optional[Job]:
        """Pick AND lease the next ready job in ONE statement, so parallel workers (threads of this
        process or other processes on the same database) can never take the same job. ``skip_stages``
        leaves jobs at those stages for later (the transcription cap)."""
        ts = _ts(now)
        skip = tuple(s.value for s in skip_stages)
        not_in = f" AND stage NOT IN ({','.join('?' * len(skip))})" if skip else ""
        row = self._x("UPDATE jobs SET state='running', owner=?, heartbeat=?, updated_at=?"
                      " WHERE state='queued' AND id=(SELECT id FROM jobs WHERE state='queued'"
                      f" AND (next_retry_at IS NULL OR next_retry_at <= ?){not_in}"
                      " ORDER BY next_retry_at, id LIMIT 1) RETURNING *",
                      (owner, ts, ts, ts, *skip)).fetchone()
        return self._job(row) if row else None

    def hold_job(self, meeting_id: str) -> Optional[str]:
        """Park the meeting's job so no worker claims it while an operator rewinds the meeting; the
        caller re-enqueues it right after, or restores it with :meth:`unhold_job`. Returns the state
        it had (``"none"`` without a job row), or ``None`` when a worker is running it now."""
        with self.transaction():
            row = self._x("SELECT state FROM jobs WHERE meeting_id=?", (meeting_id,)).fetchone()
            if row is None:
                return "none"
            if row["state"] == "running":
                return None
            self._x("UPDATE jobs SET state='held', updated_at=? WHERE meeting_id=?", (time.time(), meeting_id))
            return str(row["state"])

    def unhold_job(self, meeting_id: str, state: str) -> None:
        self._x("UPDATE jobs SET state=?, updated_at=? WHERE meeting_id=? AND state='held'",
                (state, time.time(), meeting_id))

    def get_job(self, meeting_id: str) -> Optional[Job]:
        row = self._x("SELECT * FROM jobs WHERE meeting_id=?", (meeting_id,)).fetchone()
        return self._job(row) if row else None

    def list_jobs(self, states: Sequence[str] = ("queued", "running", "failed"), *,
                  space: Optional[str] = None) -> list[Job]:
        """Jobs in ``states``; with ``space``, only that space's meetings (the queue itself is shared)."""
        where, params = f"state IN ({','.join('?' * len(states))})", list(states)
        if space is not None:
            where += " AND meeting_id IN (SELECT id FROM meetings WHERE space=?)"
            params.append(space)
        rows = self._x(f"SELECT * FROM jobs WHERE {where} ORDER BY id", tuple(params)).fetchall()
        return [self._job(r) for r in rows]

    def claim_job(self, job_id: int, *, now: datetime, owner: Optional[str] = None) -> bool:
        """Atomically move ``queued -> running`` and take the lease; False when another worker won."""
        ts = _ts(now)
        cur = self._x("UPDATE jobs SET state='running', owner=?, heartbeat=?, updated_at=?"
                      " WHERE id=? AND state='queued'", (owner, ts, ts, job_id))
        return cur.rowcount == 1

    def heartbeat_job(self, job_id: int, owner: Optional[str], *, now: float) -> bool:
        """Refresh our lease; False when the row is no longer ours (requeued as stale, reprocessed)."""
        cur = self._x("UPDATE jobs SET heartbeat=? WHERE id=? AND state='running' AND owner IS ?",
                      (now, job_id, owner))
        return cur.rowcount == 1

    def mark_job_running(self, job_id: int, *, now: datetime, owner: Optional[str] = None) -> None:
        ts = _ts(now)
        self._x("UPDATE jobs SET state='running', owner=?, heartbeat=?, updated_at=? WHERE id=?",
                (owner, ts, ts, job_id))

    def advance_job(self, job_id: int, stage: Stage) -> None:
        self._x("UPDATE jobs SET stage=?, updated_at=? WHERE id=?", (stage.value, time.time(), job_id))

    def complete_job(self, job_id: int) -> None:
        self._x("UPDATE jobs SET state='done', owner=NULL, error=NULL, next_retry_at=NULL, updated_at=? WHERE id=?",
                (time.time(), job_id))

    def defer_job(self, job_id: int, stage: Stage, reason: str, *, retry_at: datetime) -> None:
        """Re-queue without counting an attempt (the stage only waits for something to be ready)."""
        self._x("UPDATE jobs SET state='queued', owner=NULL, heartbeat=NULL, error=?, next_retry_at=?, stage=?,"
                " updated_at=? WHERE id=?", (reason[:2000], _ts(retry_at), stage.value, time.time(), job_id))

    def fail_job(self, job_id: int, stage: Stage, error: str, *, retry_at: Optional[datetime]) -> None:
        """Record a failure; ``retry_at`` re-queues (backoff), ``None`` parks it as failed."""
        state = "queued" if retry_at is not None else "failed"
        self._x("UPDATE jobs SET state=?, owner=NULL, heartbeat=NULL, attempts=attempts+1, failed_stage=?,"
                " error=?, next_retry_at=?, stage=?, updated_at=? WHERE id=?",
                (state, stage.value, error[:2000], _ts(retry_at), stage.value, time.time(), job_id))

    def requeue_stale(self, *, stale_before: float,
                      owner_dead: Optional[Callable[[Optional[str]], bool]] = None) -> list[str]:
        """Running jobs whose lease expired (crashed/stopped worker) go back to the queue.

        Rows without a heartbeat predate leases (schema v1) and are treated as stale. With
        ``owner_dead``, a job whose lease is still fresh but whose owner is provably gone (same host,
        pid no longer exists — e.g. the gateway was just restarted) is taken back as well, instead of
        waiting for the lease to run out.
        """
        rows = self._x("UPDATE jobs SET state='queued', owner=NULL, heartbeat=NULL, next_retry_at=NULL"
                       " WHERE state='running' AND (heartbeat IS NULL OR heartbeat < ?) RETURNING meeting_id",
                       (stale_before,)).fetchall()
        taken = [r["meeting_id"] for r in rows]
        if owner_dead is not None:
            for row in self._x("SELECT id, owner FROM jobs WHERE state='running'").fetchall():
                if owner_dead(row["owner"]):
                    back = self._x("UPDATE jobs SET state='queued', owner=NULL, heartbeat=NULL, next_retry_at=NULL"
                                   " WHERE id=? AND state='running' AND owner IS ? RETURNING meeting_id",
                                   (row["id"], row["owner"])).fetchone()
                    if back is not None:
                        taken.append(back["meeting_id"])
        return taken

    def requeue_running(self) -> int:
        """Unconditional requeue — only for single-process tools/tests; production uses leases."""
        return self._x("UPDATE jobs SET state='queued', owner=NULL, heartbeat=NULL, next_retry_at=NULL"
                       " WHERE state='running'").rowcount

    def pending_job_count(self, space: Optional[str] = None) -> int:
        if space is None:
            return int(self._x("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0])
        return int(self._x("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running') AND meeting_id IN"
                           " (SELECT id FROM meetings WHERE space=?)", (space,)).fetchone()[0])
