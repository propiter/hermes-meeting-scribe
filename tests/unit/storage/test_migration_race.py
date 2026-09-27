"""Concurrent opening of a pre-v4 database (review finding 6): the migration runs exactly once."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

from meeting_scribe.storage import repo as repo_mod
from meeting_scribe.storage.repo import SCHEMA_VERSION, Repository

ROOT = Path(__file__).resolve().parents[3]

OPEN = textwrap.dedent("""
    import sys
    sys.path.insert(0, sys.argv[2])
    from pathlib import Path
    from meeting_scribe.storage.repo import Repository
    r = Repository(Path(sys.argv[1]))
    print(r.user_version(), r.get_meeting("old1").source)
    r.close()
""")


def make_v3(path: Path) -> None:
    """A real v3 database (the shipped v1..v3 scripts) with one meeting row in it."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    for version, script in enumerate(repo_mod._MIGRATIONS[:3], start=1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={version};\nCOMMIT;")
    data = ('{"id": "old1", "guild_id": "1", "channel_id": "2", "channel_name": "c", '
            '"started_at": "2026-09-01T00:00:00+00:00", "state": "done", "title": "t"}')
    conn.execute("INSERT INTO meetings (id, guild_id, channel_id, state, started_at, title, folder, data, updated_at)"
                 " VALUES ('old1', '1', '2', 'done', '2026-09-01T00:00:00+00:00', 't', '', ?, 0)", (data,))
    conn.close()


def test_v3_database_migrates_once_under_concurrent_opens(tmp_path):
    failures = []
    for round_ in range(4):
        db = tmp_path / f"r{round_}.sqlite"
        make_v3(db)
        procs = [subprocess.Popen([sys.executable, "-c", OPEN, str(db), str(ROOT)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(6)]
        for p in procs:
            out, err = p.communicate(timeout=60)
            if p.returncode != 0:
                failures.append(err.strip().splitlines()[-1] if err.strip() else f"exit {p.returncode}")
            else:
                assert out.split() == [str(SCHEMA_VERSION), "discord"]
    assert failures == []


def test_concurrent_opens_in_threads(tmp_path):
    import threading
    db = tmp_path / "t.sqlite"
    make_v3(db)
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
    assert r.user_version() == SCHEMA_VERSION and r.get_meeting("old1").source == "discord"
    r.close()
