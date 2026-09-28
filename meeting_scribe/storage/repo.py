"""SQLite index (DESIGN §9, §23): meetings, speakers, utterances + FTS5, jobs, deliveries, action
items, person links and the learned channel↔project maps — every meeting-derived row belongs to ONE
space (``meetings.space``), and every query that lists, searches or learns takes the space.

WAL mode so the CLI can read while the gateway's workers write. One connection per repository
guarded by an RLock: pipeline workers and command handlers share it, and SQLite serialises writers
anyway. ``BASELINE`` is the schema of record; a database written by an older layout (before spaces)
is moved aside to a timestamped backup by :mod:`.baseline` and a fresh one is created. Later schema
changes append to ``_MIGRATIONS``.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from ..domain.text import fold
from ..domain.models import SOURCE_DISCORD, ActionItem, ActionStatus, Meeting, MeetingState, Speaker, Utterance
from . import baseline
from .deliveries import Claim, DeliveriesMixin
from .jobs import Job, JobsMixin
from .result import Result
from .spaces import SpacesMixin

__all__ = ["Claim", "Job", "Repository", "SCHEMA_VERSION"]

_BASELINE = """
CREATE TABLE meetings (
  id TEXT PRIMARY KEY, space TEXT NOT NULL, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
  state TEXT NOT NULL, started_at TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', folder TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL, updated_at REAL NOT NULL, capture_owner TEXT,
  source TEXT NOT NULL DEFAULT 'discord', external_id TEXT);
CREATE INDEX meetings_space_started ON meetings(space, started_at DESC);
CREATE UNIQUE INDEX meetings_source_external ON meetings(space, source, external_id) WHERE external_id IS NOT NULL;
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
  failed_stage TEXT, error TEXT, next_retry_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  owner TEXT, heartbeat REAL);
CREATE TABLE deliveries (
  id INTEGER PRIMARY KEY, meeting_id TEXT NOT NULL, sink TEXT NOT NULL, key TEXT NOT NULL,
  external_id TEXT, url TEXT, created_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'done', claim TEXT,
  claimed_at REAL, UNIQUE (sink, key));
CREATE TABLE action_items (
  meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE, id TEXT NOT NULL,
  position INTEGER NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (meeting_id, id));
CREATE TABLE item_sinks (
  meeting_id TEXT NOT NULL, item_id TEXT NOT NULL, sink TEXT NOT NULL, status TEXT NOT NULL,
  updated_at REAL NOT NULL, PRIMARY KEY (meeting_id, item_id, sink));
CREATE TABLE item_overrides (
  meeting_id TEXT NOT NULL, item_id TEXT NOT NULL, project TEXT, project_key TEXT, updated_at REAL NOT NULL,
  PRIMARY KEY (meeting_id, item_id));
CREATE TABLE links (
  space TEXT NOT NULL, discord_user_id TEXT NOT NULL, linear_user_id TEXT, email TEXT, name TEXT,
  updated_at REAL NOT NULL, PRIMARY KEY (space, discord_user_id));
CREATE TABLE channel_projects (
  space TEXT NOT NULL, channel_id TEXT NOT NULL, project_key TEXT NOT NULL, project_name TEXT NOT NULL,
  updated_at REAL NOT NULL, PRIMARY KEY (space, channel_id));
CREATE TABLE project_channels (
  space TEXT NOT NULL, project TEXT NOT NULL, channel_id TEXT NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY (space, project));
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT, updated_at REAL NOT NULL);
CREATE TABLE leases (name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE spaces (
  slug TEXT PRIMARY KEY, name TEXT NOT NULL, settings TEXT NOT NULL DEFAULT '{}',
  adopt_guilds INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE space_guilds (
  guild_id TEXT PRIMARY KEY, space TEXT NOT NULL REFERENCES spaces(slug) ON DELETE CASCADE,
  name TEXT NOT NULL DEFAULT '');
CREATE TABLE desktop_commands (
  id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL, updated_at REAL NOT NULL, owner TEXT);
CREATE INDEX desktop_commands_state ON desktop_commands(state, created_at);
"""
BASELINE = baseline.BASELINE_VERSION
_MIGRATIONS: tuple[str, ...] = ()  # schema changes after the baseline, in order (BASELINE + 1, ...)
SCHEMA_VERSION = BASELINE + len(_MIGRATIONS)
_WORD_RE = re.compile(r"\w+", re.UNICODE)


_MEETING_COLS = "data, source, external_id, space"


def meeting_from_row(row: sqlite3.Row) -> Meeting:
    """A meeting from its index row: ``source``/``external_id``/``space`` come from the COLUMNS (fixed
    at insert), never the JSON."""
    return Meeting.from_dict(meeting_dict_from_row(row))


def meeting_dict_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = json.loads(row["data"])
    data["source"] = row["source"] or SOURCE_DISCORD
    data["external_id"] = row["external_id"]
    data["space"] = row["space"]
    return data


def _statements(script: str) -> list[str]:
    """Split a migration script into complete SQL statements (``;`` inside literals is respected)."""
    out: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete SQL statement in migration: {buf.strip()[:80]}")
    return out


class Repository(JobsMixin, DeliveriesMixin, SpacesMixin):
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.retired = baseline.retire_legacy(path)  # a pre-spaces database is backed up, not migrated
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")  # first: the WAL switch and migrations may wait
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # -- plumbing -------------------------------------------------------------------------------
    def _migrate(self) -> None:
        """Create the baseline on an empty database, then apply later migrations — atomically and safe
        against processes opening the database at once: ``user_version`` is read INSIDE a
        ``BEGIN IMMEDIATE`` transaction (the write lock), so a second process waits for the first one's
        COMMIT and sees the new version. ``executescript`` is not used: it commits any open transaction.
        """
        with self._lock:
            if self.user_version() >= SCHEMA_VERSION:
                return  # fast path: no write lock taken on an up-to-date database
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                current = self.user_version()
                if current == 0:
                    for statement in _statements(_BASELINE):
                        self._conn.execute(statement)
                    current = BASELINE
                    self._conn.execute(f"PRAGMA user_version={BASELINE}")
                for version, script in enumerate(_MIGRATIONS[current - BASELINE:], start=current + 1):
                    for statement in _statements(script):
                        self._conn.execute(statement)
                    self._conn.execute(f"PRAGMA user_version={int(version)}")
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def _x(self, sql: str, params: Sequence[Any] = ()) -> Result:
        """Run one statement and read ALL of its rows while holding the lock. Pipeline workers,
        heartbeats and command handlers share this connection: handing out a live cursor would let
        another thread's statement interleave with the fetch."""
        with self._lock:
            cur = self._conn.execute(sql, params)
            return Result(cur.fetchall() if cur.description is not None else [], cur.rowcount)

    @contextmanager
    def transaction(self):
        """Serialize a read/check/write operation across threads and SQLite connections."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

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
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def journal_mode(self) -> str:
        with self._lock:
            return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- meetings -------------------------------------------------------------------------------
    def save_meeting(self, meeting: Meeting) -> None:
        if not meeting.space:
            raise ValueError(f"meeting {meeting.id} has no space")
        self._tx([(
            "INSERT INTO meetings (id, space, guild_id, channel_id, state, started_at, title, folder, data, updated_at,"
            " source, external_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,"
            " title=excluded.title, folder=excluded.folder, updated_at=excluded.updated_at,"
            # space/source/external_id are fixed at insert: a stale in-memory copy can never move a
            # meeting to another space or turn an import into a Discord meeting.
            " data=json_set(excluded.data, '$.source', meetings.source, '$.external_id', meetings.external_id,"
            " '$.space', meetings.space)",
            (meeting.id, meeting.space, meeting.guild_id, meeting.channel_id, meeting.state.value,
             meeting.started_at.isoformat(), meeting.title, meeting.folder,
             json.dumps(meeting.to_dict(), ensure_ascii=False), time.time(), meeting.source, meeting.external_id),
        )] + [(
            "INSERT INTO speakers (meeting_id, user_id, name, is_bot) VALUES (?,?,?,?)"
            " ON CONFLICT(meeting_id, user_id) DO UPDATE SET name=excluded.name",
            (meeting.id, s.user_id, s.name, int(s.is_bot)),
        ) for s in meeting.speakers])

    def create_imported_meeting(self, meeting: Meeting, utterances: Sequence[Utterance]) -> bool:
        """Insert an imported meeting (row + speakers + utterances) in ONE transaction.

        ``False`` when ``(space, source, external_id)`` already exists — another process or an earlier
        run imported it — and nothing is written. A crash can never leave a row without its transcript.
        """
        if not meeting.external_id or not meeting.space:
            raise ValueError("imported meetings need an external_id and a space")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "INSERT INTO meetings (id, space, guild_id, channel_id, state, started_at, title, folder, data,"
                    " updated_at, source, external_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    (meeting.id, meeting.space, meeting.guild_id, meeting.channel_id, meeting.state.value,
                     meeting.started_at.isoformat(), meeting.title, meeting.folder,
                     json.dumps(meeting.to_dict(), ensure_ascii=False), time.time(), meeting.source,
                     meeting.external_id))
                if cur.rowcount != 1:
                    self._conn.execute("ROLLBACK")
                    return False
                for s in meeting.speakers:
                    self._conn.execute("INSERT OR IGNORE INTO speakers (meeting_id, user_id, name, is_bot)"
                                       " VALUES (?,?,?,?)", (meeting.id, s.user_id, s.name, int(s.is_bot)))
                for u in utterances:
                    ucur = self._conn.execute(
                        "INSERT INTO utterances (meeting_id, t0, t1, speaker_id, speaker, text) VALUES (?,?,?,?,?,?)",
                        (meeting.id, u.t0, u.t1, u.speaker_id, u.speaker, u.text))
                    self._conn.execute("INSERT INTO utterances_fts (rowid, text, meeting_id) VALUES (?,?,?)",
                                       (ucur.lastrowid, u.text, meeting.id))
                self._conn.execute("COMMIT")
                return True
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def find_by_external(self, space: str, source: str, external_id: str) -> Optional[Meeting]:
        row = self._x(f"SELECT {_MEETING_COLS} FROM meetings WHERE space=? AND source=? AND external_id=?",
                      (space, source, external_id)).fetchone()
        return meeting_from_row(row) if row else None

    def known_external_ids(self, space: str, source: str) -> set[str]:
        rows = self._x("SELECT external_id FROM meetings WHERE space=? AND source=? AND external_id IS NOT NULL",
                       (space, source))
        return {r["external_id"] for r in rows.fetchall()}

    # -- key/value status & named leases (DESIGN §17) -------------------------------------------
    def kv_get(self, key: str) -> Optional[str]:
        row = self._x("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: Optional[str]) -> None:
        if value is None:
            self._x("DELETE FROM kv WHERE key=?", (key,))
            return
        self._x("INSERT INTO kv (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET"
                " value=excluded.value, updated_at=excluded.updated_at", (key, str(value), time.time()))

    def kv_prefix(self, prefix: str) -> dict[str, str]:
        rows = self._x("SELECT key, value FROM kv WHERE substr(key, 1, ?)=?", (len(prefix), prefix)).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def acquire_lease(self, name: str, owner: str, *, ttl: float, now: Optional[float] = None) -> bool:
        """Take or renew lease ``name``; ``False`` while another owner holds an unexpired one."""
        ts = time.time() if now is None else now
        cur = self._x("INSERT INTO leases (name, owner, expires_at) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET"
                      " owner=excluded.owner, expires_at=excluded.expires_at"
                      " WHERE leases.owner=excluded.owner OR leases.expires_at < ?", (name, owner, ts + ttl, ts))
        return cur.rowcount == 1

    def release_lease(self, name: str, owner: str) -> None:
        self._x("DELETE FROM leases WHERE name=? AND owner=?", (name, owner))

    def lease_owner(self, name: str, *, now: Optional[float] = None) -> Optional[str]:
        ts = time.time() if now is None else now
        row = self._x("SELECT owner FROM leases WHERE name=? AND expires_at >= ?", (name, ts)).fetchone()
        return row["owner"] if row else None

    def get_meeting(self, meeting_id: str) -> Optional[Meeting]:
        row = self._x(f"SELECT {_MEETING_COLS} FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        return meeting_from_row(row) if row else None

    def find_meeting(self, id_or_prefix: str, space: Optional[str] = None) -> Optional[Meeting]:
        """Exact id, else a *unique* prefix match (users type the first few chars); with ``space``, only
        that space's meetings are candidates."""
        needle = (id_or_prefix or "").strip().lower()
        if not needle:
            return None
        exact = self.get_meeting(needle)
        if exact and (space is None or exact.space == space):
            return exact
        where, params = ("id LIKE ? ESCAPE '\\'", [needle.replace("%", "\\%").replace("_", "\\_") + "%"])
        if space is not None:
            where += " AND space=?"
            params.append(space)
        rows = self._x(f"SELECT {_MEETING_COLS} FROM meetings WHERE {where} LIMIT 2", params).fetchall()
        return meeting_from_row(rows[0]) if len(rows) == 1 else None

    def meetings_without_job(self, states: Iterable[MeetingState], *, updated_before: float) -> list[str]:
        """Ids of meetings in ``states`` with no job row, whose row was last written before the cutoff."""
        wanted = [s.value for s in states]
        if not wanted:
            return []
        rows = self._x(f"SELECT m.id FROM meetings m LEFT JOIN jobs j ON j.meeting_id = m.id"
                       f" WHERE m.state IN ({','.join('?' * len(wanted))}) AND j.id IS NULL AND m.updated_at < ?",
                       (*wanted, float(updated_before))).fetchall()
        return [r["id"] for r in rows]

    def list_meetings(self, limit: int = 20, states: Optional[Iterable[MeetingState]] = None, *,
                      space: Optional[str] = None) -> list[Meeting]:
        """Newest first; ``space=None`` lists every space (only the pipeline's own recovery does that)."""
        wanted = [s.value for s in states] if states is not None else None
        if wanted is not None and not wanted:
            return []
        clauses, params = [], []
        if wanted:
            clauses.append(f"state IN ({','.join('?' * len(wanted))})")
            params += wanted
        if space is not None:
            clauses.append("space=?")
            params.append(space)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._x(f"SELECT {_MEETING_COLS} FROM meetings {where} ORDER BY started_at DESC LIMIT ?",
                       (*params, int(limit))).fetchall()
        return [meeting_from_row(r) for r in rows]

    def space_meeting_count(self, space: str) -> int:
        return int(self._x("SELECT COUNT(*) FROM meetings WHERE space=?", (space,)).fetchone()[0])

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

    def search(self, query: str, space: str, limit: int = 10) -> list[dict[str, Any]]:
        """AND of the query's words within ``space``; each word is quoted so FTS syntax is inert."""
        words = _WORD_RE.findall(query or "")
        if not words:
            return []
        match = " ".join('"' + w.replace('"', "") + '"' for w in words)
        rows = self._x(
            "SELECT u.meeting_id, u.t0, u.t1, u.speaker_id, u.speaker, u.text, m.title, m.started_at"
            " FROM utterances_fts f JOIN utterances u ON u.id = f.rowid JOIN meetings m ON m.id = u.meeting_id"
            " WHERE utterances_fts MATCH ? AND m.space=? ORDER BY bm25(utterances_fts), m.started_at DESC LIMIT ?",
            (match, space, int(limit))).fetchall()
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

    # -- people links & learned projects (per space) -------------------------------------------
    def set_link(self, space: str, discord_user_id: str, *, linear_user_id: Optional[str] = None,
                 email: Optional[str] = None, name: Optional[str] = None) -> None:
        self._x("INSERT INTO links (space, discord_user_id, linear_user_id, email, name, updated_at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(space, discord_user_id) DO UPDATE SET"
                " linear_user_id=COALESCE(excluded.linear_user_id, linear_user_id),"
                " email=COALESCE(excluded.email, email), name=COALESCE(excluded.name, name),"
                " updated_at=excluded.updated_at", (space, discord_user_id, linear_user_id, email, name, time.time()))

    def get_link(self, space: str, discord_user_id: str) -> Optional[dict[str, Any]]:
        row = self._x("SELECT * FROM links WHERE space=? AND discord_user_id=?", (space, discord_user_id)).fetchone()
        return dict(row) if row else None

    def learn_channel_project(self, space: str, channel_id: str, project_key: str, project_name: str) -> None:
        self._x("INSERT INTO channel_projects (space, channel_id, project_key, project_name, updated_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(space, channel_id) DO UPDATE SET project_key=excluded.project_key,"
                " project_name=excluded.project_name, updated_at=excluded.updated_at",
                (space, channel_id, project_key, project_name, time.time()))

    def channel_project(self, space: str, channel_id: str) -> Optional[dict[str, str]]:
        row = self._x("SELECT project_key, project_name FROM channel_projects WHERE space=? AND channel_id=?",
                      (space, channel_id)).fetchone()
        return dict(row) if row else None

    def learn_project_channel(self, space: str, project: str, channel_id: str) -> None:
        """Remember where tasks of ``project`` (folded name) belong in ``space`` (a 📁 move)."""
        self._x("INSERT INTO project_channels (space, project, channel_id, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(space, project) DO UPDATE SET channel_id=excluded.channel_id,"
                " updated_at=excluded.updated_at", (space, fold(project), str(channel_id), time.time()))

    def project_channel(self, space: str, project: str) -> Optional[str]:
        row = self._x("SELECT channel_id FROM project_channels WHERE space=? AND project=?",
                      (space, fold(project))).fetchone()
        return str(row["channel_id"]) if row else None

    def all_channel_projects(self, space: str) -> list[dict[str, str]]:
        rows = self._x("SELECT channel_id, project_key, project_name FROM channel_projects WHERE space=?"
                       " ORDER BY channel_id", (space,))
        return [dict(r) for r in rows.fetchall()]
