"""Schema v4 (DESIGN §17): meeting source/external id, idempotent imports, kv status, named leases."""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace

import pytest

from meeting_scribe.domain.models import SOURCE_DISCORD, SOURCE_GOOGLE_MEET, MeetingState, Utterance
from meeting_scribe.storage import repo as repo_mod
from meeting_scribe.storage.repo import SCHEMA_VERSION, Repository


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path / "index.sqlite")
    yield r
    r.close()


def _imported(meeting, ext="conferenceRecords/abc-123", mid="m4imp001"):
    return replace(meeting, id=mid, source=SOURCE_GOOGLE_MEET, external_id=ext, state=MeetingState.TRANSCRIBED)


UTTS = [Utterance(0.0, 2.0, "gmeet:p1", "Ana", "Hola equipo"), Utterance(2.0, 4.0, "gmeet:p2", "Luis", "Listo")]


def test_schema_is_v5():
    assert SCHEMA_VERSION == 5  # v5: desktop_commands (Desktop operator queue)


def test_meeting_source_defaults_to_discord_and_round_trips(repo, meeting):
    assert meeting.source == SOURCE_DISCORD and meeting.external_id is None
    repo.save_meeting(meeting)
    assert repo.get_meeting(meeting.id).source == SOURCE_DISCORD
    row = repo._x("SELECT source, external_id FROM meetings WHERE id=?", (meeting.id,)).fetchone()
    assert (row["source"], row["external_id"]) == ("discord", None)


def test_import_is_idempotent_on_source_and_external_id(repo, meeting):
    m = _imported(meeting)
    assert repo.create_imported_meeting(m, UTTS) is True
    assert repo.create_imported_meeting(replace(m, id="m4imp002"), UTTS) is False  # same conference
    assert repo.get_meeting("m4imp002") is None
    assert repo.utterance_count(m.id) == 2 and repo.search("equipo")[0]["meeting_id"] == m.id
    assert repo.find_by_external(SOURCE_GOOGLE_MEET, m.external_id).id == m.id
    assert repo.known_external_ids(SOURCE_GOOGLE_MEET) == {m.external_id}
    # Discord meetings never collide (external_id NULL is not unique-constrained)
    repo.save_meeting(replace(meeting, id="d1"))
    repo.save_meeting(replace(meeting, id="d2"))
    assert {x.id for x in repo.list_meetings()} >= {"d1", "d2", m.id}


def test_import_requires_external_id(repo, meeting):
    with pytest.raises(ValueError):
        repo.create_imported_meeting(replace(meeting, source=SOURCE_GOOGLE_MEET), UTTS)


def test_two_repositories_racing_import_only_one_wins(tmp_path, meeting):
    """Two processes (two connections) import the same conference at the same time."""
    path = tmp_path / "index.sqlite"
    repos = [Repository(path), Repository(path)]
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def go(i):
        barrier.wait()
        results.append(repos[i].create_imported_meeting(_imported(meeting, mid=f"race000{i}"), UTTS))
    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [False, True]
    assert len(repos[0].known_external_ids(SOURCE_GOOGLE_MEET)) == 1
    [r.close() for r in repos]


def test_kv(repo):
    assert repo.kv_get("x") is None
    repo.kv_set("gmeet:last_poll", "2026-09-27T10:00:00+00:00")
    repo.kv_set("gmeet:error", "boom")
    assert repo.kv_get("gmeet:last_poll").startswith("2026")
    assert set(repo.kv_prefix("gmeet:")) == {"gmeet:last_poll", "gmeet:error"}
    repo.kv_set("gmeet:error", None)
    assert repo.kv_get("gmeet:error") is None


def test_named_lease(repo):
    assert repo.acquire_lease("gmeet-poll", "A", ttl=60, now=1000)
    assert not repo.acquire_lease("gmeet-poll", "B", ttl=60, now=1030)  # A still holds it
    assert repo.acquire_lease("gmeet-poll", "A", ttl=60, now=1050)  # renew
    assert repo.lease_owner("gmeet-poll", now=1100) == "A"
    assert repo.acquire_lease("gmeet-poll", "B", ttl=60, now=1200)  # expired -> taken over
    repo.release_lease("gmeet-poll", "A")  # not A's anymore: no-op
    assert repo.lease_owner("gmeet-poll", now=1200) == "B"
    repo.release_lease("gmeet-poll", "B")
    assert repo.lease_owner("gmeet-poll", now=1200) is None


def test_v3_database_upgrades_to_v4_in_place(tmp_path, meeting):
    """A real v3 file (0.2.0) with a Discord meeting keeps its data and gains the new columns."""
    path = tmp_path / "index.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(repo_mod._V1 + repo_mod._V2 + repo_mod._V3 + "PRAGMA user_version=3;")
    conn.execute("INSERT INTO meetings (id, guild_id, channel_id, state, started_at, title, folder, data, updated_at)"
                 " VALUES (?,?,?,?,?,?,?,?,?)",
                 (meeting.id, meeting.guild_id, meeting.channel_id, meeting.state.value,
                  meeting.started_at.isoformat(), meeting.title, "", '{"id": "%s", "guild_id": "100", '
                  '"channel_id": "200", "channel_name": "Daily Sync", "started_at": "%s", "state": "captured"}'
                  % (meeting.id, meeting.started_at.isoformat()), 0.0))
    conn.commit()
    conn.close()
    r = Repository(path)
    try:
        assert r.user_version() == SCHEMA_VERSION  # upgrades straight through v4 and v5
        assert r._x("SELECT COUNT(*) FROM desktop_commands").fetchone()[0] == 0
        got = r.get_meeting(meeting.id)
        assert got.source == SOURCE_DISCORD and got.external_id is None and got.channel_name == "Daily Sync"
        row = r._x("SELECT source FROM meetings WHERE id=?", (meeting.id,)).fetchone()
        assert row["source"] == "discord"
        assert r.create_imported_meeting(_imported(meeting), UTTS)
    finally:
        r.close()
