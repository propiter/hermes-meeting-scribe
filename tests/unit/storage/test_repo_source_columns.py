"""``meetings.source`` / ``external_id`` columns are authoritative over the ``data`` JSON.

Real case: a Google Meet import (columns ``google_meet`` + the conference record) whose JSON was
rewritten by a gateway running pre-v4 code, which knew neither field: ``data`` says
``source=discord, external_id=null``. Read back as Discord, a reprocess would try to transcribe
audio that never existed and the Desktop page would offer "transcribe"."""
import json
import sqlite3
from dataclasses import replace

import pytest

from meeting_scribe.desktop.queries import Library
from meeting_scribe.domain.models import SOURCE_DISCORD, SOURCE_GOOGLE_MEET, MeetingState
from meeting_scribe.storage import repo as repo_mod
from meeting_scribe.storage.repo import Repository

REC = "conferenceRecords/abc-123"


def _clobber_json_like_pre_v4(path, mid):
    """What the old gateway left: columns intact, JSON without source/external_id (defaults)."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    data = json.loads(conn.execute("SELECT data FROM meetings WHERE id=?", (mid,)).fetchone()[0])
    data.pop("source", None)
    data["external_id"] = None
    data["source"] = SOURCE_DISCORD
    conn.execute("UPDATE meetings SET data=? WHERE id=?", (json.dumps(data), mid))
    conn.close()


@pytest.fixture
def imported(tmp_path, meeting):
    path = tmp_path / "index.sqlite"
    r = Repository(path)
    m = replace(meeting, id="g1", source=SOURCE_GOOGLE_MEET, external_id=REC, state=MeetingState.DONE)
    r.save_meeting(m)
    r.close()
    _clobber_json_like_pre_v4(path, m.id)
    r = Repository(path)
    yield r, m
    r.close()


def test_every_read_takes_source_and_external_id_from_the_columns(imported):
    repo, m = imported
    row = repo._x("SELECT source, external_id, json_extract(data, '$.source') AS js FROM meetings").fetchone()
    assert (row["source"], row["external_id"]) == (SOURCE_GOOGLE_MEET, REC)  # the exact production case
    for got in (repo.get_meeting(m.id), repo.find_meeting(m.id), repo.find_meeting("g"),
                repo.find_by_external(SOURCE_GOOGLE_MEET, REC), repo.list_meetings()[0],
                repo.list_meetings(states=[MeetingState.DONE])[0]):
        assert (got.source, got.external_id) == (SOURCE_GOOGLE_MEET, REC)


def test_save_never_degrades_or_changes_the_source_of_an_existing_row(imported):
    repo, m = imported
    stale = replace(m, source=SOURCE_DISCORD, external_id=None, title="Renamed")  # an old in-memory copy
    repo.save_meeting(stale)
    row = repo._x("SELECT source, external_id, title, data FROM meetings WHERE id=?", (m.id,)).fetchone()
    assert (row["source"], row["external_id"], row["title"]) == (SOURCE_GOOGLE_MEET, REC, "Renamed")
    data = json.loads(row["data"])  # the JSON is made coherent again, not left stale
    assert (data["source"], data["external_id"]) == (SOURCE_GOOGLE_MEET, REC)
    repo.save_meeting(replace(m, source="something_else"))
    assert repo.get_meeting(m.id).source == SOURCE_GOOGLE_MEET


def test_reprocess_of_the_clobbered_import_redoes_the_notes_not_the_audio(imported):
    from meeting_scribe.domain.models import Stage
    from meeting_scribe.pipeline.runner import effective_stage

    repo, m = imported
    assert effective_stage(repo.get_meeting(m.id), Stage.TRANSCRIBE) is Stage.ANALYZE


def test_desktop_library_and_detail_show_the_column_source(imported, tmp_path):
    repo, m = imported
    lib = Library(repo, tmp_path)
    assert [x["source"] for x in lib.meetings()["items"]] == [SOURCE_GOOGLE_MEET]
    assert [x["id"] for x in lib.meetings(source=SOURCE_GOOGLE_MEET)["items"]] == [m.id]
    assert lib.audio(m.id)["reason"] == "imported"


def test_migration_repairs_stale_json_idempotently(tmp_path, meeting):
    path = tmp_path / "index.sqlite"
    conn = sqlite3.connect(str(path), isolation_level=None)
    for version, script in enumerate(repo_mod._MIGRATIONS[:7], start=1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={version};\nCOMMIT;")
    stale = json.dumps(replace(meeting, id="g1").to_dict())  # source=discord, external_id=None
    conn.execute("INSERT INTO meetings (id, guild_id, channel_id, state, started_at, title, folder, data, updated_at,"
                 " source, external_id) VALUES ('g1', '1', '2', 'done', '2026-09-01T00:00:00+00:00', 't', '', ?, 0,"
                 " 'google_meet', ?)", (stale, REC))
    conn.execute("INSERT INTO meetings (id, guild_id, channel_id, state, started_at, title, folder, data, updated_at)"
                 " VALUES ('d1', '1', '2', 'done', '2026-09-02T00:00:00+00:00', 't', '', ?, 0)",
                 (json.dumps(replace(meeting, id="d1").to_dict()),))
    conn.close()
    for _ in range(2):
        r = Repository(path)
        raw = {row["id"]: json.loads(row["data"]) for row in r._x("SELECT id, data FROM meetings").fetchall()}
        assert (raw["g1"]["source"], raw["g1"]["external_id"]) == (SOURCE_GOOGLE_MEET, REC)
        assert (raw["d1"]["source"], raw["d1"]["external_id"]) == (SOURCE_DISCORD, None)
        r.close()


def test_persist_writes_meta_json_with_the_column_source(imported, tmp_path):
    """meta.json mirrors the row: a stale in-memory copy cannot write ``discord`` into it either."""
    from meeting_scribe.pipeline.stages import Stages
    from meeting_scribe.storage.artifacts import read_meta
    from meeting_scribe.storage.layout import Layout

    repo, m = imported
    stages = Stages(repo=repo, layout=Layout(lambda: tmp_path / "data"), settings=lambda: None, transcriber=None,
                    analyzer=None, catalogs=lambda: [], sinks=lambda: [], archiver=lambda *a: None)
    saved = stages.persist(replace(m, source=SOURCE_DISCORD, external_id=None))
    assert (saved.source, saved.external_id) == (SOURCE_GOOGLE_MEET, REC)
    meta = read_meta(stages.folder(saved))
    assert (meta.source, meta.external_id) == (SOURCE_GOOGLE_MEET, REC)
