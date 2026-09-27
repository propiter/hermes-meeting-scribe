"""Durable operator commands from the Desktop page, executed only by the gateway's pipeline worker.

The dashboard process never builds a pipeline, never starts a worker and never runs a stage: it
writes one row into ``desktop_commands`` and the worker that already runs in the gateway picks it up
on its next tick (``PipelineRunner.control``); the page polls ``GET /v1/commands/<id>``. Events do
not cross processes, so polling is the contract.

Only ``reprocess`` is accepted. A claimed command is never retried after a crash (its outcome is
unknown and reported as such); request ids make an HTTP retry of the same submission inert.
"""
from __future__ import annotations

import json
import os
import socket
import re
import time
from typing import Any, Mapping

from ..domain.errors import NothingToReprocess
from ..domain.models import MeetingState, Stage
from ..llm_config import redact
from ..storage.owner import process_owner_id

HEARTBEAT_KV = "desktop.worker_heartbeat"
HEARTBEAT_EVERY = 20.0  # seconds between liveness writes (the worker ticks every ~5 s when idle)
REPROCESS_STAGES = ("transcribe", "analyze", "deliver")
STALE_RUNNING_SECONDS = 600
_RID_RE = re.compile(r"[A-Za-z0-9_-]{1,100}")
_last_pulse: dict[int, float] = {}


def pulse(repo: Any, *, force: bool = False) -> None:
    """Record that the gateway worker is alive (read by ``GET /v1/status``); throttled per repo."""
    now = time.time()
    if not force and now - _last_pulse.get(id(repo), 0.0) < HEARTBEAT_EVERY:
        return
    _last_pulse[id(repo)] = now
    repo.kv_set(HEARTBEAT_KV, str(now))


def _refuse_busy(repo: Any, meeting: Any) -> None:
    """Same rule as ``hermes meeting-scribe reprocess``: not while recording or while a stage runs."""
    job = repo.get_job(meeting.id)
    if meeting.state == MeetingState.RECORDING:
        raise ValueError("meeting is still recording")
    if meeting.state == MeetingState.EMPTY:
        raise NothingToReprocess("no audio was captured; there is nothing to reprocess")
    if job is not None and job.state == "running":
        raise ValueError("meeting is being processed right now; try again when the stage finishes")


def executor_dead(owner: str | None) -> bool:
    """Only a missing local PID proves death; age, remote and legacy owners do not."""
    if not owner:
        return False
    parts = owner.split(":")
    if len(parts) != 3 or parts[0] != socket.gethostname() or not parts[1].isascii() or not parts[1].isdigit():
        return False
    pid = int(parts[1])
    if not 0 < pid < 2**31 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        pass  # denied/unknown is not proof of death
    return False


class Commands:
    """Submit/read side, used by the dashboard process (SQLite only)."""

    def __init__(self, repo: Any) -> None:
        self.repo = repo

    def _reconcile(self, where: str, params: tuple) -> None:
        """Mark ``running`` rows whose executor is provably gone as ``unknown`` (never re-run them)."""
        for row in self.repo._x(f"SELECT id, owner FROM desktop_commands WHERE state='running' AND {where}",
                                params).fetchall():
            if executor_dead(row["owner"]):
                self.repo._x("UPDATE desktop_commands SET state='unknown',error=?,updated_at=? "
                             "WHERE id=? AND state='running' AND owner=?",
                             ("Executor exited; inspect results before acknowledging. Nothing will be retried automatically.",
                              time.time(), row["id"], row["owner"]))

    def get(self, rid: str) -> dict[str, Any]:
        self._reconcile("id=?", (rid,))
        row = self.repo._x("SELECT id,meeting_id,body,state,error,created_at,updated_at FROM desktop_commands "
                           "WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise KeyError(rid)
        out = dict(row)
        body = json.loads(out.pop("body"))
        out["action"], out["stage"] = body.get("action"), body.get("stage")
        out["stalled"] = out["state"] == "running" and time.time() - out["updated_at"] > STALE_RUNNING_SECONDS
        return out

    def acknowledge(self, rid: str) -> dict[str, Any]:
        """Release an uncertain result, never execute it. Active work remains protected."""
        with self.repo.transaction():
            command = self.get(rid)
            if command["state"] == "acknowledged":
                return command
            if command["state"] != "unknown":
                raise ValueError("executor death is not proven; cannot acknowledge this command")
            job = self.repo.get_job(command["meeting_id"])
            if job is not None and job.state in ("queued", "running"):
                raise ValueError("processing is still pending; inspect the result after it finishes")
            self.repo._x("UPDATE desktop_commands SET state='acknowledged',updated_at=? WHERE id=? AND state='unknown'",
                         (time.time(), rid))
            return self.get(rid)

    def submit(self, rid: str, mid: str, body: Mapping[str, Any]) -> dict[str, Any]:
        # Outside the transaction: a refusal below rolls back, and the orphan must stay reconciled.
        if isinstance(mid, str):
            self._reconcile("meeting_id=?", (mid,))
        with self.repo.transaction():
            return self._submit_locked(rid, mid, body)

    def _submit_locked(self, rid: str, mid: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(rid, str) or not _RID_RE.fullmatch(rid):
            raise ValueError("invalid request id")
        encoded = _encode(body)
        old = self.repo._x("SELECT meeting_id,body FROM desktop_commands WHERE id=?", (rid,)).fetchone()
        if old is not None:
            if old["meeting_id"] != mid or old["body"] != encoded:
                raise ValueError("request id already used")
            return self.get(rid)
        meeting = self.repo.get_meeting(mid)
        if meeting is None:
            raise KeyError(mid)
        if body["action"] == "reprocess":
            _refuse_busy(self.repo, meeting)
        pending = self.repo._x("SELECT id FROM desktop_commands WHERE meeting_id=? AND state IN ('queued','running','unknown')",
                               (mid,)).fetchone()
        if pending is not None:
            raise ValueError("a command for this meeting is already pending")
        now = time.time()
        self.repo._x("INSERT INTO desktop_commands(id,meeting_id,body,created_at,updated_at) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(id) DO NOTHING", (rid, mid, encoded, now, now))
        row = self.repo._x("SELECT body,meeting_id FROM desktop_commands WHERE id=?", (rid,)).fetchone()
        if row["body"] != encoded or row["meeting_id"] != mid:  # a concurrent submit won the INSERT
            raise ValueError("request id already used")
        return self.get(rid)


def _encode(body: Mapping[str, Any]) -> str:
    """The two commands the page may queue: ``reprocess`` from a stage, and ``prepare_audio``
    (write the listening copy of an older multitrack archive)."""
    if not isinstance(body, Mapping):
        raise ValueError("invalid command")
    if body.get("action") == "prepare_audio" and set(body) == {"action"}:
        return json.dumps({"action": "prepare_audio"}, sort_keys=True)
    if set(body) != {"action", "stage"} or body.get("action") != "reprocess":
        raise ValueError("invalid command")
    if body.get("stage") not in REPROCESS_STAGES:
        raise ValueError("invalid stage")
    return json.dumps({"action": "reprocess", "stage": body["stage"]}, sort_keys=True)


def execute_one(service: Any) -> bool:
    """Gateway side: run the oldest queued command; False when there is none. Never raises."""
    repo = service.repo
    pulse(repo)
    row = repo._x("SELECT * FROM desktop_commands WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
    if row is None:
        return False
    if repo._x("UPDATE desktop_commands SET state='running',updated_at=?,owner=? WHERE id=? AND state='queued'",
               (time.time(), process_owner_id(), row["id"])).rowcount != 1:
        return True
    error = ""
    try:
        body = json.loads(row["body"])
        _encode(body)
        meeting = service.require(row["meeting_id"])
        if body["action"] == "prepare_audio":
            service.prepare_audio(meeting.id)
        else:
            _refuse_busy(repo, meeting)
            service.reprocess(meeting.id, Stage(body["stage"]))
    except Exception as exc:  # reported to the page; the worker keeps running
        error = redact(f"{type(exc).__name__}: {exc}")[:500]
    repo._x("UPDATE desktop_commands SET state=?,error=?,updated_at=? WHERE id=?",
            ("failed" if error else "done", error, time.time(), row["id"]))
    return True
