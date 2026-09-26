"""SQLite index (DESIGN §9): meetings, speakers, utterances + FTS5, jobs, deliveries, action items,
person links and the learned channel→project map.

WAL mode so the CLI can read while the gateway's worker writes. One connection per repository
guarded by an RLock: the pipeline thread and command handlers share it, and SQLite serialises
writers anyway. Schema changes go through ``_MIGRATIONS`` keyed by ``PRAGMA user_version``.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from ..domain.models import ActionItem, ActionStatus, Meeting, MeetingState, Speaker, Stage, Utterance

_V1 = """
CREATE TABLE meetings (
  id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, state TEXT NOT NULL,
  started_at TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', folder TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX meetings_started ON meetings(started_at DESC);
CREATE TABLE speakers (
  meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE, user_id TEXT NOT NULL,
  name TEXT NOT NULL, is_bot INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (meeting_id, user_id));
CREATE TABLE utterances (
  id INTEGER PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  t0 REAL NOT NULL, t1 REAL NOT NULL, speaker_id TEXT NOT NULL, speaker TEXT NOT NULL, text TEXT NOT NULL);
CREATE INDEX utterances_meeting ON utterances(meeting_id, t0);
CREATE VIRTUAL TABLE utterances_fts USING fts5(
  text, meeting_id UNINDEXED, tokenize = 'unicode61 remove_diacritics 2');
CREATE TABLE jobs (
  id INTEGER PRIMARY KEY, meeting_id TEXT NOT NULL UNIQUE REFERENCES meetings(id) ON DELETE CASCADE,
  stage TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  failed_stage TEXT, error TEXT, next_retry_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE deliveries (
  id INTEGER PRIMARY KEY, meeting_id TEXT NOT NULL, sink TEXT NOT NULL, key TEXT NOT NULL,
  external_id TEXT, url TEXT, created_at REAL NOT NULL, UNIQUE (sink, key));
CREATE TABLE action_items (
  meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE, id TEXT NOT NULL,
  position INTEGER NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (meeting_id, id));
CREATE TABLE links (
  discord_user_id TEXT PRIMARY KEY, linear_user_id TEXT, email TEXT, name TEXT, updated_at REAL NOT NULL);
CREATE TABLE channel_projects (
  channel_id TEXT PRIMARY KEY, project_key TEXT NOT NULL, project_name TEXT NOT NULL, updated_at REAL NOT NULL);
"""
_MIGRATIONS: tuple[str, ...] = (_V1,)
SCHEMA_VERSION = len(_MIGRATIONS)
_WORD_RE = re.compile(r"\w+", re.UNICODE)


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


def _ts(value: Optional[datetime]) -> Optional[float]:
    return value.timestamp() if value is not None else None


class Repository:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    # -- plumbing -------------------------------------------------------------------------------
    def _migrate(self) -> None:
        with self._lock:
            current = self.user_version()
            for version, script in enumerate(_MIGRATIONS[current:], start=current + 1):
                self._conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={version};\nCOMMIT;")

    def _x(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _tx(self, statements: Iterable[tuple[str, Sequence[Any]]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for sql, params in statements:
                    self._conn.execute(sql, params)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def user_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def journal_mode(self) -> str:
        return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- meetings -------------------------------------------------------------------------------
    def save_meeting(self, meeting: Meeting) -> None:
        self._tx([(
            "INSERT INTO meetings (id, guild_id, channel_id, state, started_at, title, folder, data, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,"
            " title=excluded.title, folder=excluded.folder, data=excluded.data, updated_at=excluded.updated_at",
            (meeting.id, meeting.guild_id, meeting.channel_id, meeting.state.value,
             meeting.started_at.isoformat(), meeting.title, meeting.folder,
             json.dumps(meeting.to_dict(), ensure_ascii=False), time.time()),
        )] + [(
            "INSERT INTO speakers (meeting_id, user_id, name, is_bot) VALUES (?,?,?,?)"
            " ON CONFLICT(meeting_id, user_id) DO UPDATE SET name=excluded.name",
            (meeting.id, s.user_id, s.name, int(s.is_bot)),
        ) for s in meeting.speakers])

    def get_meeting(self, meeting_id: str) -> Optional[Meeting]:
        row = self._x("SELECT data FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        return Meeting.from_dict(json.loads(row["data"])) if row else None

    def find_meeting(self, id_or_prefix: str) -> Optional[Meeting]:
        """Exact id, else a *unique* prefix match (users type the first few chars)."""
        needle = (id_or_prefix or "").strip().lower()
        if not needle:
            return None
        exact = self.get_meeting(needle)
        if exact:
            return exact
        rows = self._x("SELECT data FROM meetings WHERE id LIKE ? ESCAPE '\\' LIMIT 2",
                       (needle.replace("%", "\\%").replace("_", "\\_") + "%",)).fetchall()
        return Meeting.from_dict(json.loads(rows[0]["data"])) if len(rows) == 1 else None

    def list_meetings(self, limit: int = 20, states: Optional[Iterable[MeetingState]] = None) -> list[Meeting]:
        wanted = [s.value for s in states] if states is not None else None
        if wanted is not None and not wanted:
            return []
        where = f"WHERE state IN ({','.join('?' * len(wanted))})" if wanted else ""
        rows = self._x(f"SELECT data FROM meetings {where} ORDER BY started_at DESC LIMIT ?",
                       (*(wanted or ()), int(limit))).fetchall()
        return [Meeting.from_dict(json.loads(r["data"])) for r in rows]

    def upsert_speakers(self, meeting_id: str, speakers: Sequence[Speaker]) -> None:
        self._tx([("INSERT INTO speakers (meeting_id, user_id, name, is_bot) VALUES (?,?,?,?)"
                   " ON CONFLICT(meeting_id, user_id) DO UPDATE SET name=excluded.name",
                   (meeting_id, s.user_id, s.name, int(s.is_bot))) for s in speakers])

    # -- utterances + FTS -----------------------------------------------------------------------
    def replace_utterances(self, meeting_id: str, utterances: Sequence[Utterance]) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM utterances_fts WHERE meeting_id=?", (meeting_id,))
                self._conn.execute("DELETE FROM utterances WHERE meeting_id=?", (meeting_id,))
                for u in utterances:
                    cur = self._conn.execute(
                        "INSERT INTO utterances (meeting_id, t0, t1, speaker_id, speaker, text)"
                        " VALUES (?,?,?,?,?,?)", (meeting_id, u.t0, u.t1, u.speaker_id, u.speaker, u.text))
                    self._conn.execute("INSERT INTO utterances_fts (rowid, text, meeting_id) VALUES (?,?,?)",
                                       (cur.lastrowid, u.text, meeting_id))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """AND of the query's words; each word is quoted so FTS syntax in user input is inert."""
        words = _WORD_RE.findall(query or "")
        if not words:
            return []
        match = " ".join('"' + w.replace('"', "") + '"' for w in words)
        rows = self._x(
            "SELECT u.meeting_id, u.t0, u.t1, u.speaker_id, u.speaker, u.text, m.title, m.started_at"
            " FROM utterances_fts f JOIN utterances u ON u.id = f.rowid JOIN meetings m ON m.id = u.meeting_id"
            " WHERE utterances_fts MATCH ? ORDER BY bm25(utterances_fts), m.started_at DESC LIMIT ?",
            (match, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def utterance_count(self, meeting_id: str) -> int:
        return int(self._x("SELECT COUNT(*) FROM utterances WHERE meeting_id=?", (meeting_id,)).fetchone()[0])

    # -- jobs -----------------------------------------------------------------------------------
    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(id=row["id"], meeting_id=row["meeting_id"], stage=Stage(row["stage"]), state=row["state"],
                   attempts=row["attempts"],
                   failed_stage=Stage(row["failed_stage"]) if row["failed_stage"] else None,
                   error=row["error"], next_retry_at=row["next_retry_at"])

    def enqueue_job(self, meeting_id: str, stage: Stage, *, now: datetime, reset_attempts: bool = False) -> None:
        """One job row per meeting (the pipeline is sequential per meeting); re-enqueue updates it."""
        ts = _ts(now)
        reset = ", attempts=0, error=NULL, failed_stage=NULL" if reset_attempts else ""
        self._x("INSERT INTO jobs (meeting_id, stage, state, next_retry_at, created_at, updated_at)"
                " VALUES (?,?, 'queued', ?, ?, ?) ON CONFLICT(meeting_id) DO UPDATE SET stage=excluded.stage,"
                f" state='queued', next_retry_at=excluded.next_retry_at, updated_at=excluded.updated_at{reset}",
                (meeting_id, stage.value, ts, ts, ts))

    def next_job(self, *, now: datetime) -> Optional[Job]:
        row = self._x("SELECT * FROM jobs WHERE state='queued' AND (next_retry_at IS NULL OR next_retry_at <= ?)"
                      " ORDER BY next_retry_at, id LIMIT 1", (_ts(now),)).fetchone()
        return self._job(row) if row else None

    def get_job(self, meeting_id: str) -> Optional[Job]:
        row = self._x("SELECT * FROM jobs WHERE meeting_id=?", (meeting_id,)).fetchone()
        return self._job(row) if row else None

    def list_jobs(self, states: Sequence[str] = ("queued", "running", "failed")) -> list[Job]:
        rows = self._x(f"SELECT * FROM jobs WHERE state IN ({','.join('?' * len(states))}) ORDER BY id",
                       tuple(states)).fetchall()
        return [self._job(r) for r in rows]

    def claim_job(self, job_id: int, *, now: datetime) -> bool:
        """Atomically move ``queued -> running``; False when another worker got it first."""
        cur = self._x("UPDATE jobs SET state='running', updated_at=? WHERE id=? AND state='queued'",
                      (_ts(now), job_id))
        return cur.rowcount == 1

    def mark_job_running(self, job_id: int, *, now: datetime) -> None:
        self._x("UPDATE jobs SET state='running', updated_at=? WHERE id=?", (_ts(now), job_id))

    def advance_job(self, job_id: int, stage: Stage) -> None:
        self._x("UPDATE jobs SET stage=?, updated_at=? WHERE id=?", (stage.value, time.time(), job_id))

    def complete_job(self, job_id: int) -> None:
        self._x("UPDATE jobs SET state='done', error=NULL, next_retry_at=NULL, updated_at=? WHERE id=?",
                (time.time(), job_id))

    def fail_job(self, job_id: int, stage: Stage, error: str, *, retry_at: Optional[datetime]) -> None:
        """Record a failure; ``retry_at`` re-queues (backoff), ``None`` parks it as failed."""
        state = "queued" if retry_at is not None else "failed"
        self._x("UPDATE jobs SET state=?, attempts=attempts+1, failed_stage=?, error=?, next_retry_at=?,"
                " stage=?, updated_at=? WHERE id=?",
                (state, stage.value, error[:2000], _ts(retry_at), stage.value, time.time(), job_id))

    def requeue_running(self) -> int:
        """Jobs left ``running`` by a crash/restart go back to the queue (resume on start)."""
        return self._x("UPDATE jobs SET state='queued', next_retry_at=NULL WHERE state='running'").rowcount

    def pending_job_count(self) -> int:
        return int(self._x("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0])

    # -- deliveries -----------------------------------------------------------------------------
    def get_delivery(self, sink: str, key: str) -> Optional[dict[str, Any]]:
        row = self._x("SELECT * FROM deliveries WHERE sink=? AND key=?", (sink, key)).fetchone()
        return dict(row) if row else None

    def record_delivery(self, meeting_id: str, sink: str, key: str, *, external_id: Optional[str],
                        url: Optional[str]) -> None:
        """First write wins: a delivery that already happened is never overwritten."""
        self._x("INSERT OR IGNORE INTO deliveries (meeting_id, sink, key, external_id, url, created_at)"
                " VALUES (?,?,?,?,?,?)", (meeting_id, sink, key, external_id, url, time.time()))

    def list_deliveries(self, meeting_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._x("SELECT * FROM deliveries WHERE meeting_id=? ORDER BY id",
                                         (meeting_id,)).fetchall()]

    # -- action items ---------------------------------------------------------------------------
    def sync_action_items(self, meeting_id: str, items: Sequence[ActionItem]) -> None:
        """Mirror the latest analysis while keeping human decisions (status) on surviving ids."""
        keep = [a.id for a in items]
        stmts: list[tuple[str, Sequence[Any]]] = [(
            f"DELETE FROM action_items WHERE meeting_id=? AND id NOT IN ({','.join('?' * len(keep))})"
            if keep else "DELETE FROM action_items WHERE meeting_id=?", (meeting_id, *keep))]
        for pos, a in enumerate(items):
            stmts.append(("INSERT INTO action_items (meeting_id, id, position, status, data) VALUES (?,?,?,?,?)"
                          " ON CONFLICT(meeting_id, id) DO UPDATE SET position=excluded.position,"
                          " data=excluded.data",
                          (meeting_id, a.id, pos, a.status.value, json.dumps(a.to_dict(), ensure_ascii=False))))
        self._tx(stmts)

    def _item(self, row: sqlite3.Row) -> ActionItem:
        return ActionItem.from_dict({**json.loads(row["data"]), "status": row["status"]})

    def list_action_items(self, meeting_id: str) -> list[ActionItem]:
        rows = self._x("SELECT * FROM action_items WHERE meeting_id=? ORDER BY position", (meeting_id,))
        return [self._item(r) for r in rows.fetchall()]

    def get_action_item(self, meeting_id: str, item_id: str) -> Optional[ActionItem]:
        row = self._x("SELECT * FROM action_items WHERE meeting_id=? AND id=?", (meeting_id, item_id)).fetchone()
        return self._item(row) if row else None

    def set_action_status(self, meeting_id: str, item_id: str, status: ActionStatus) -> None:
        self._x("UPDATE action_items SET status=? WHERE meeting_id=? AND id=?", (status.value, meeting_id, item_id))

    # -- people links & learned projects --------------------------------------------------------
    def set_link(self, discord_user_id: str, *, linear_user_id: Optional[str] = None,
                 email: Optional[str] = None, name: Optional[str] = None) -> None:
        self._x("INSERT INTO links (discord_user_id, linear_user_id, email, name, updated_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(discord_user_id) DO UPDATE SET"
                " linear_user_id=COALESCE(excluded.linear_user_id, linear_user_id),"
                " email=COALESCE(excluded.email, email), name=COALESCE(excluded.name, name),"
                " updated_at=excluded.updated_at", (discord_user_id, linear_user_id, email, name, time.time()))

    def get_link(self, discord_user_id: str) -> Optional[dict[str, Any]]:
        row = self._x("SELECT * FROM links WHERE discord_user_id=?", (discord_user_id,)).fetchone()
        return dict(row) if row else None

    def learn_channel_project(self, channel_id: str, project_key: str, project_name: str) -> None:
        self._x("INSERT INTO channel_projects (channel_id, project_key, project_name, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(channel_id) DO UPDATE SET project_key=excluded.project_key,"
                " project_name=excluded.project_name, updated_at=excluded.updated_at",
                (channel_id, project_key, project_name, time.time()))

    def channel_project(self, channel_id: str) -> Optional[dict[str, str]]:
        row = self._x("SELECT project_key, project_name FROM channel_projects WHERE channel_id=?",
                      (channel_id,)).fetchone()
        return dict(row) if row else None

    def all_channel_projects(self) -> list[dict[str, str]]:
        rows = self._x("SELECT channel_id, project_key, project_name FROM channel_projects ORDER BY channel_id")
        return [dict(r) for r in rows.fetchall()]
