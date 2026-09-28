"""Concurrent opening of a pre-spaces database (DESIGN §23): it is backed up exactly once, never migrated.

Replaces the old v1..v8 migration race tests: the baseline no longer migrates legacy rows, so what
must hold under concurrency is that exactly one opener retires the file and every opener then sees
the fresh baseline database.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

from meeting_scribe.storage.baseline import backups
from meeting_scribe.storage.repo import SCHEMA_VERSION, Repository

ROOT = Path(__file__).resolve().parents[3]

OPEN = textwrap.dedent("""
    import sys
    sys.path.insert(0, sys.argv[2])
    from pathlib import Path
    from meeting_scribe.storage.repo import Repository
    r = Repository(Path(sys.argv[1]))
    print(r.user_version(), len(r.list_meetings()))
    r.close()
""")


def make_legacy(path: Path, version: int = 3) -> None:
    """A pre-spaces database (WAL, a committed row) with a meeting folder next to it."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE meetings (id TEXT PRIMARY KEY, data TEXT)")
    conn.execute("INSERT INTO meetings VALUES ('old1', '{}')")
    conn.execute(f"PRAGMA user_version={version}")
    conn.close()
    folder = path.parent / "meetings" / "2026" / "09" / "old1"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "notes.md").write_text("old notes")


def _assert_one_complete_backup(root: Path) -> None:
    saved = backups(root)
    assert len(saved) == 1
    conn = sqlite3.connect(str(saved[0] / "index.sqlite"))
    try:
        assert conn.execute("SELECT id FROM meetings").fetchall() == [("old1",)]
    finally:
        conn.close()
    assert (saved[0] / "meetings" / "2026" / "09" / "old1" / "notes.md").read_text() == "old notes"


def test_legacy_database_is_retired_once_under_concurrent_process_opens(tmp_path):
    failures = []
    for round_ in range(3):
        root = tmp_path / f"r{round_}"
        root.mkdir()
        db = root / "index.sqlite"
        make_legacy(db)
        procs = [subprocess.Popen([sys.executable, "-c", OPEN, str(db), str(ROOT)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(6)]
        for p in procs:
            out, err = p.communicate(timeout=60)
            if p.returncode != 0:
                failures.append(err.strip().splitlines()[-1] if err.strip() else f"exit {p.returncode}")
            else:
                assert out.split() == [str(SCHEMA_VERSION), "0"]
        _assert_one_complete_backup(root)
    assert failures == []


def test_a_new_database_survives_concurrent_process_opens(tmp_path):
    """Several processes creating the store at once used to race on ``PRAGMA journal_mode=WAL``
    (``database is locked``: SQLite's busy handler does not cover it); setup now runs under a lock."""
    failures = []
    for round_ in range(5):
        db = tmp_path / f"n{round_}" / "index.sqlite"
        db.parent.mkdir()
        procs = [subprocess.Popen([sys.executable, "-c", OPEN, str(db), str(ROOT)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(8)]
        for p in procs:
            out, err = p.communicate(timeout=60)
            if p.returncode != 0:
                failures.append(err.strip().splitlines()[-1] if err.strip() else f"exit {p.returncode}")
            else:
                assert out.split() == [str(SCHEMA_VERSION), "0"]
        assert backups(db.parent) == []
    assert failures == []


def test_concurrent_opens_in_threads(tmp_path):
    import threading
    db = tmp_path / "index.sqlite"
    make_legacy(db, version=8)
    errors: list = []
    barrier = threading.Barrier(8)

    def run():
        barrier.wait()
        try:
            Repository(db).close()
        except Exception as exc:  # pragma: no cover - the bug
            errors.append(exc)
    threads = [threading.Thread(target=run) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    r = Repository(db)
    assert r.user_version() == SCHEMA_VERSION and r.list_meetings() == []
    r.close()
    _assert_one_complete_backup(tmp_path)
