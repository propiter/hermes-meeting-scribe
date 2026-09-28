"""The storage baseline (DESIGN §23): a database written before spaces is backed up, never migrated.

Meetings recorded before spaces carry no space, and the old layout kept every meeting folder
directly under ``meetings/``. Instead of a chain of migrations for disposable data, opening such a
database moves it — and its meeting folders — into ``backup-<UTC timestamp>/`` next to it, and a
fresh baseline database is created. Nothing is deleted: the backup is a complete, readable copy.

The move runs under an exclusive file lock, so two processes opening the store at once retire it
exactly once (the second one finds the new database). SQLite's online backup API copies the old
database including any committed WAL content before the originals are removed.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from ..filelock import file_lock

BASELINE_VERSION = 100  # user_version of the spaces baseline; any older non-zero version is legacy
BACKUP_PREFIX = "backup-"


def _version(db: Path) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def setup_lock(db: Path) -> Any:
    """The cross-process lock under which a store is retired and (re)created. Several processes
    opening a NEW database at once would otherwise race on ``PRAGMA journal_mode=WAL``, which fails
    with ``database is locked`` instead of waiting (SQLite's busy handler does not cover it)."""
    return file_lock(db.with_name(db.name + ".retire.lock"))


def is_current(db: Path, version: int) -> bool:
    """A WAL database already at ``version``: opening it needs no setup (and no lock)."""
    if not db.exists() or not db.with_name(db.name + "-wal").exists() and _journal_mode(db) != "wal":
        return False
    try:
        return _version(db) >= version
    except sqlite3.Error:
        return False


def _journal_mode(db: Path) -> str:
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        finally:
            conn.close()
    except sqlite3.Error:
        return ""


def retire_legacy(db: Path) -> Optional[Path]:
    """Move a pre-spaces database (and the meeting folders it indexed) aside; the backup folder, or
    ``None`` when there was nothing to retire."""
    if not db.exists():
        return None
    with setup_lock(db):
        if not db.exists() or not 0 < _version(db) < BASELINE_VERSION:
            return None
        backup = db.parent / f"{BACKUP_PREFIX}{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        backup.mkdir()
        src = sqlite3.connect(str(db))
        dst = sqlite3.connect(str(backup / db.name))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        for suffix in ("", "-wal", "-shm"):
            db.with_name(db.name + suffix).unlink(missing_ok=True)
        meetings = db.parent / "meetings"
        if meetings.is_dir():
            meetings.rename(backup / "meetings")
        return backup


def backups(root: Path) -> list[Path]:
    """Backups made by :func:`retire_legacy`, oldest first (doctor lists them)."""
    return sorted(p for p in root.glob(f"{BACKUP_PREFIX}*") if p.is_dir())
