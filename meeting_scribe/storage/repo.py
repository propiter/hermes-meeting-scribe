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
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from ..domain.text import fold
from ..domain.models import ActionItem, ActionStatus, Meeting, MeetingState, Speaker, Utterance
from .deliveries import Claim, DeliveriesMixin
from .jobs import Job, JobsMixin

__all__ = ["Claim", "Job", "Repository", "SCHEMA_VERSION"]

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
# v2 (review findings 1, 2, 6): job leases, recording ownership, delivery claims, per-sink decisions.
_V2 = """
ALTER TABLE jobs ADD COLUMN owner TEXT;
ALTER TABLE jobs ADD COLUMN heartbeat REAL;
ALTER TABLE meetings ADD COLUMN capture_owner TEXT;
ALTER TABLE deliveries ADD COLUMN state TEXT NOT NULL DEFAULT 'done';
ALTER TABLE deliveries ADD COLUMN claim TEXT;
ALTER TABLE deliveries ADD COLUMN claimed_at REAL;
CREATE TABLE item_sinks (
  meeting_id TEXT NOT NULL, item_id TEXT NOT NULL, sink TEXT NOT NULL, status TEXT NOT NULL,
  updated_at REAL NOT NULL, PRIMARY KEY (meeting_id, item_id, sink));
INSERT OR IGNORE INTO item_sinks (meeting_id, item_id, sink, status, updated_at)
  SELECT meeting_id, substr(key, length('mtg:' || meeting_id || ':') + 1), sink, 'delivered', created_at
  FROM deliveries WHERE key LIKE 'mtg:%' AND sink IN ('kanban', 'linear');
"""
# v3 (DESIGN §16): project name -> Discord channel learned from 📁 corrections.
_V3 = """
CREATE TABLE project_channels (
  project TEXT PRIMARY KEY, channel_id TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE item_overrides (
  meeting_id TEXT NOT NULL, item_id TEXT NOT NULL, project TEXT, project_key TEXT, updated_at REAL NOT NULL,
  PRIMARY KEY (meeting_id, item_id));
"""
_MIGRATIONS: tuple[str, ...] = (_V1, _V2, _V3)
SCHEMA_VERSION = len(_MIGRATIONS)
_WORD_RE = re.compile(r"\w+", re.UNICODE)


class Repository(JobsMixin, DeliveriesMixin):
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

    def set_capture_owner(self, meeting_id: str, owner: Optional[str]) -> None:
        """The process whose capture writes this recording (only it — or a successor after it died —
        may close the row as an orphan; review finding 2)."""
        self._x("UPDATE meetings SET capture_owner=? WHERE id=?", (owner, meeting_id))

    def capture_owner(self, meeting_id: str) -> Optional[str]:
        row = self._x("SELECT capture_owner FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        return row["capture_owner"] if row else None

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

    _ITEM_SELECT = ("SELECT a.*, o.project AS o_project, o.project_key AS o_key FROM action_items a"
                    " LEFT JOIN item_overrides o ON o.meeting_id=a.meeting_id AND o.item_id=a.id")

    def _item(self, row: sqlite3.Row) -> ActionItem:
        data = {**json.loads(row["data"]), "status": row["status"]}
        if row["o_key"]:  # a human 📁 move beats whatever the (re-)analysis said
            data.update(project=row["o_project"], project_key=row["o_key"], project_confidence=1.0)
        return ActionItem.from_dict(data)

    def list_action_items(self, meeting_id: str) -> list[ActionItem]:
        rows = self._x(f"{self._ITEM_SELECT} WHERE a.meeting_id=? ORDER BY a.position", (meeting_id,))
        return [self._item(r) for r in rows.fetchall()]

    def get_action_item(self, meeting_id: str, item_id: str) -> Optional[ActionItem]:
        row = self._x(f"{self._ITEM_SELECT} WHERE a.meeting_id=? AND a.id=?", (meeting_id, item_id)).fetchone()
        return self._item(row) if row else None

    def set_item_override(self, meeting_id: str, item_id: str, *, project: str, project_key: str) -> None:
        """Pin one task to a project (📁 move); survives re-analysis of the meeting."""
        self._x("INSERT INTO item_overrides (meeting_id, item_id, project, project_key, updated_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(meeting_id, item_id) DO UPDATE SET project=excluded.project,"
                " project_key=excluded.project_key, updated_at=excluded.updated_at",
                (meeting_id, item_id, project, project_key, time.time()))

    def update_action_item(self, meeting_id: str, item: ActionItem) -> None:
        """Replace an item's data (e.g. a 📁 move) without touching its human decision (status)."""
        self._x("UPDATE action_items SET data=? WHERE meeting_id=? AND id=?",
                (json.dumps(item.to_dict(), ensure_ascii=False), meeting_id, item.id))

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

    def learn_project_channel(self, project: str, channel_id: str) -> None:
        """Remember where tasks of ``project`` (folded name) belong (a 📁 move)."""
        self._x("INSERT INTO project_channels (project, channel_id, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(project) DO UPDATE SET channel_id=excluded.channel_id, updated_at=excluded.updated_at",
                (fold(project), str(channel_id), time.time()))

    def project_channel(self, project: str) -> Optional[str]:
        row = self._x("SELECT channel_id FROM project_channels WHERE project=?", (fold(project),)).fetchone()
        return str(row["channel_id"]) if row else None

    def all_channel_projects(self) -> list[dict[str, str]]:
        rows = self._x("SELECT channel_id, project_key, project_name FROM channel_projects ORDER BY channel_id")
        return [dict(r) for r in rows.fetchall()]
