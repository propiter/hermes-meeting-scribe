"""Forgetting a meeting erases everything that points at it — and nothing else.

The "nothing else" is the part worth testing: a wrong clause in ``forget_meeting`` would wipe rows of
every other meeting in the same index, and nothing in the app would say so.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from meeting_scribe.domain.models import ActionItem, Utterance
from meeting_scribe.storage.repo import Repository


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path / "index.sqlite")
    yield r
    r.close()


# The tables this test actually fills, so the "other meeting is untouched" check can be exact.
SEEDED = ("speakers", "utterances", "utterances_fts", "deliveries", "action_items", "item_sinks",
          "speaker_audit", "jobs")


def _seed(repo: Repository, meeting, *, item: str) -> str:
    mid = meeting.id
    repo.save_meeting(meeting)
    repo.upsert_speakers(mid, meeting.speakers)
    repo.replace_utterances(mid, [Utterance(0.0, 2.0, "10", "Ana", "hola")])
    repo.sync_action_items(mid, [ActionItem(id=item, title="revisar")])
    repo.audit_speaker(mid, "unidentified-1", "tester", None, "10", "2026-10-19T00:00:00+00:00")
    repo.kv_set("pipeline.sinks_done." + mid, '{"notes": "x"}')
    repo.kv_set("speakers.assigned." + mid, "{}")
    repo._x("INSERT INTO deliveries (meeting_id, sink, key, external_id, url, created_at, state)"
            " VALUES (?,?,?,?,?,?,?)", (mid, "discord", f"mtg:{mid}:notes", "{}", "", 0.0, "done"))
    repo._x("INSERT INTO item_sinks (meeting_id, item_id, sink, status, updated_at)"
            " VALUES (?,?,?,?,?)", (mid, item, "kanban", "approved", 0.0))
    repo._x("INSERT INTO jobs (meeting_id, stage, state, attempts, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)", (mid, "transcribe", "done", 0, 0.0, 0.0))
    return mid


def test_forget_erases_every_row_pointing_at_the_meeting(repo, meeting):
    mid = _seed(repo, meeting, item="a0000000001")

    repo.forget_meeting(mid)

    assert repo.get_meeting(mid) is None
    for table in Repository.MEETING_TABLES:
        assert repo._x(f"SELECT 1 FROM {table} WHERE meeting_id=?", (mid,)).fetchall() == [], table
    for prefix in Repository.MEETING_KV_PREFIXES:
        assert repo.kv_get(prefix + mid) is None, prefix


def test_forget_leaves_every_other_meeting_alone(repo, meeting):
    """The safety property: one meeting's id must not take another's rows with it."""
    mid = _seed(repo, meeting, item="a0000000001")
    other = _seed(repo, replace(meeting, id="zzzzzzzz", channel_id="201"), item="a0000000002")

    repo.forget_meeting(mid)

    assert repo.get_meeting(other) is not None
    for table in SEEDED:
        assert repo._x(f"SELECT 1 FROM {table} WHERE meeting_id=?", (other,)).fetchall() != [], table
    assert repo.kv_get("pipeline.sinks_done." + other) is not None
    assert repo.list_action_items(other) != []
