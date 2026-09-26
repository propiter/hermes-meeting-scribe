from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from meeting_scribe.domain.models import ActionStatus, MeetingState, Stage
from meeting_scribe.storage.repo import SCHEMA_VERSION, Repository

NOW = datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path / "index.sqlite")
    yield r
    r.close()


def test_schema_version_and_wal(repo, tmp_path):
    assert repo.user_version() == SCHEMA_VERSION
    assert repo.journal_mode() == "wal"
    Repository(tmp_path / "index.sqlite").close()  # re-open is idempotent


def test_meeting_crud(repo, meeting):
    repo.save_meeting(meeting)
    assert repo.get_meeting(meeting.id) == meeting
    repo.save_meeting(replace(meeting, title="New"))
    assert repo.get_meeting(meeting.id).title == "New"
    assert repo.get_meeting("nope") is None
    assert [m.id for m in repo.list_meetings()] == [meeting.id]
    assert repo.list_meetings(states=[MeetingState.DONE]) == []


def test_find_by_prefix(repo, meeting):
    repo.save_meeting(meeting)
    assert repo.find_meeting("k3v7").id == meeting.id
    assert repo.find_meeting("zz") is None


def test_fts_search(repo, meeting, utterances):
    repo.save_meeting(meeting)
    repo.replace_utterances(meeting.id, utterances)
    hits = repo.search("smtp")
    assert len(hits) == 1 and hits[0]["meeting_id"] == meeting.id and hits[0]["speaker"] == "Ana"
    assert repo.search("credenciales viernes")[0]["t0"] == 3.5
    repo.replace_utterances(meeting.id, utterances[:1])
    assert repo.search("credenciales") == []
    assert repo.search('"; DROP TABLE x; --') == []
    assert repo.search("") == []


def test_search_accent_insensitive(repo, meeting, utterances):
    repo.save_meeting(meeting)
    repo.replace_utterances(meeting.id, utterances)
    assert repo.search("migracion")


def test_jobs_lifecycle(repo, meeting):
    repo.save_meeting(meeting)
    repo.enqueue_job(meeting.id, Stage.TRANSCRIBE, now=NOW)
    repo.enqueue_job(meeting.id, Stage.TRANSCRIBE, now=NOW)  # dedup on meeting
    job = repo.next_job(now=NOW)
    assert job.meeting_id == meeting.id and job.stage is Stage.TRANSCRIBE and job.attempts == 0
    repo.mark_job_running(job.id, now=NOW)
    assert repo.next_job(now=NOW) is None
    repo.fail_job(job.id, Stage.TRANSCRIBE, "boom", retry_at=NOW + timedelta(seconds=30))
    assert repo.next_job(now=NOW) is None
    retried = repo.next_job(now=NOW + timedelta(seconds=31))
    assert retried.attempts == 1 and retried.error == "boom" and retried.failed_stage is Stage.TRANSCRIBE
    repo.fail_job(retried.id, Stage.ANALYZE, "x", retry_at=None)
    assert repo.next_job(now=NOW + timedelta(days=1)) is None
    assert repo.get_job(meeting.id).state == "failed"
    repo.enqueue_job(meeting.id, Stage.ANALYZE, now=NOW, reset_attempts=True)
    j = repo.next_job(now=NOW)
    assert j.stage is Stage.ANALYZE and j.attempts == 0
    repo.complete_job(j.id)
    assert repo.get_job(meeting.id).state == "done"
    assert repo.pending_job_count() == 0


def test_requeue_running_jobs_on_startup(repo, meeting):
    repo.save_meeting(meeting)
    repo.enqueue_job(meeting.id, Stage.TRANSCRIBE, now=NOW)
    j = repo.next_job(now=NOW)
    repo.mark_job_running(j.id, now=NOW)
    assert repo.requeue_running() == 1
    assert repo.next_job(now=NOW).id == j.id


def test_deliveries_idempotent(repo, meeting):
    repo.save_meeting(meeting)
    key = "mtg:k3v7q2ab:a1"
    assert repo.get_delivery("kanban", key) is None
    repo.record_delivery(meeting.id, "kanban", key, external_id="t_1", url=None)
    repo.record_delivery(meeting.id, "kanban", key, external_id="t_2", url=None)
    assert repo.get_delivery("kanban", key)["external_id"] == "t_1"
    assert repo.get_delivery("linear", key) is None
    assert len(repo.list_deliveries(meeting.id)) == 1


def test_upsert_delivery_replaces_pointer(repo, meeting):
    repo.save_meeting(meeting)
    key = "mtg:k3v7q2ab:notes"
    repo.upsert_delivery(meeting.id, "discord", key, external_id="a", url="u1")
    repo.upsert_delivery(meeting.id, "discord", key, external_id="b", url="u2")
    row = repo.get_delivery("discord", key)
    assert (row["external_id"], row["url"]) == ("b", "u2")
    assert len(repo.list_deliveries(meeting.id)) == 1


def test_action_items(repo, meeting, notes):
    repo.save_meeting(meeting)
    repo.sync_action_items(meeting.id, notes.action_items)
    items = repo.list_action_items(meeting.id)
    assert [a.id for a in items] == ["a0000000001", "a0000000002"]
    repo.set_action_status(meeting.id, "a0000000001", ActionStatus.APPROVED)
    repo.sync_action_items(meeting.id, notes.action_items)  # re-analysis keeps human decisions
    assert repo.get_action_item(meeting.id, "a0000000001").status is ActionStatus.APPROVED
    repo.sync_action_items(meeting.id, notes.action_items[:1])
    assert [a.id for a in repo.list_action_items(meeting.id)] == ["a0000000001"]


def test_links_and_channel_projects(repo):
    repo.set_link("10", linear_user_id="lin_1", email="ana@x.io", name="Ana")
    assert repo.get_link("10")["linear_user_id"] == "lin_1"
    repo.set_link("10", linear_user_id="lin_2")
    assert repo.get_link("10")["linear_user_id"] == "lin_2" and repo.get_link("10")["email"] == "ana@x.io"
    assert repo.get_link("99") is None
    repo.learn_channel_project("200", "hermes:p1", "Website")
    assert repo.channel_project("200") == {"project_key": "hermes:p1", "project_name": "Website"}
    assert repo.channel_project("201") is None
    assert repo.all_channel_projects() == [{"channel_id": "200", "project_key": "hermes:p1",
                                            "project_name": "Website"}]


def test_migration_from_older_version(tmp_path):
    import sqlite3
    db = tmp_path / "old.sqlite"
    sqlite3.connect(db).close()
    r = Repository(db)
    assert r.user_version() == SCHEMA_VERSION
    r.close()


def test_claim_job_is_exclusive(repo, meeting):
    # Two processes (gateway worker + `hermes meeting-scribe process`) may race for one job.
    repo.save_meeting(meeting)
    repo.enqueue_job(meeting.id, Stage.TRANSCRIBE, now=NOW)
    job = repo.next_job(now=NOW)
    assert repo.claim_job(job.id, now=NOW) is True
    assert repo.claim_job(job.id, now=NOW) is False
    assert repo.get_job(meeting.id).state == "running"
