"""Desktop operator commands: durable queue in SQLite, executed only by the gateway's worker."""
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from meeting_scribe.desktop import control
from meeting_scribe.desktop.control import Commands, execute_one
from meeting_scribe.domain.models import MeetingState, Stage
from meeting_scribe.storage.repo import Repository


@pytest.fixture
def repo(tmp_path, meeting):
    r = Repository(tmp_path / "index.sqlite")
    r.save_meeting(replace(meeting, state=MeetingState.DONE))
    yield r
    r.close()


def fake_service(repo, calls, fail=None):
    def reprocess(mid, stage):
        if fail:
            raise fail
        calls.append((mid, stage))
    return SimpleNamespace(repo=repo, reprocess=reprocess, require=lambda mid: repo.get_meeting(mid))


def test_submit_is_idempotent_and_execution_is_gateway_side(repo, meeting):
    queue = Commands(repo)
    body = {"action": "reprocess", "stage": "analyze"}
    first = queue.submit("request-1", meeting.id, body)
    assert first["state"] == "queued" and first["stage"] == "analyze"
    assert queue.submit("request-1", meeting.id, dict(body)) == first  # an HTTP retry is inert
    with pytest.raises(ValueError):  # same id, different content
        queue.submit("request-1", meeting.id, {"action": "reprocess", "stage": "deliver"})
    with pytest.raises(ValueError):  # one pending command per meeting
        queue.submit("request-2", meeting.id, body)
    assert repo.get_job(meeting.id) is None  # submitting never touches the pipeline
    calls = []
    assert execute_one(fake_service(repo, calls)) is True
    assert calls == [(meeting.id, Stage.ANALYZE)]
    assert queue.get("request-1")["state"] == "done"
    assert execute_one(fake_service(repo, calls)) is False


@pytest.mark.parametrize("rid,body", [
    ("bad id!", {"action": "reprocess", "stage": "analyze"}),
    ("x" * 101, {"action": "reprocess", "stage": "analyze"}),
    ("ok", {"action": "shell", "stage": "analyze"}),
    ("ok", {"action": "dismiss", "item_id": "a1"}),
    ("ok", {"action": "reprocess", "stage": "archive"}),
    ("ok", {"action": "reprocess", "stage": "analyze", "actor": "spoof"}),
    ("ok", {"action": "reprocess"}),
])
def test_submit_rejects_anything_but_reprocess(repo, meeting, rid, body):
    with pytest.raises(ValueError):
        Commands(repo).submit(rid, meeting.id, body)


def test_submit_refuses_unknown_recording_and_running(repo, meeting):
    queue = Commands(repo)
    with pytest.raises(KeyError):
        queue.submit("a", "missing", {"action": "reprocess", "stage": "deliver"})
    repo.save_meeting(replace(meeting, state=MeetingState.RECORDING))
    with pytest.raises(ValueError, match="recording"):
        queue.submit("b", meeting.id, {"action": "reprocess", "stage": "deliver"})
    repo.save_meeting(replace(meeting, state=MeetingState.ANALYZING))
    now = datetime.now(timezone.utc)
    repo.enqueue_job(meeting.id, Stage.ANALYZE, now=now)
    repo.claim_job(repo.get_job(meeting.id).id, now=now, owner="test")
    with pytest.raises(ValueError, match="processed"):
        queue.submit("c", meeting.id, {"action": "reprocess", "stage": "deliver"})


def test_failures_are_redacted_and_do_not_raise(repo, meeting):
    Commands(repo).submit("r", meeting.id, {"action": "reprocess", "stage": "deliver"})
    assert execute_one(fake_service(repo, [], fail=RuntimeError("boom Bearer sk-secret123456"))) is True
    got = Commands(repo).get("r")
    assert got["state"] == "failed"
    assert "boom" in got["error"] and "sk-secret123456" not in got["error"]


def test_stale_running_without_death_proof_stays_blocked(repo, meeting):
    Commands(repo).submit("r", meeting.id, {"action": "reprocess", "stage": "deliver"})
    repo._x("UPDATE desktop_commands SET state='running', updated_at=?", (time.time() - 3600,))
    assert Commands(repo).get("r")["state"] == "running"
    assert Commands(repo).get("r")["stalled"] is True
    with pytest.raises(ValueError):
        Commands(repo).submit("another", meeting.id, {"action": "reprocess", "stage": "deliver"})
    assert execute_one(fake_service(repo, [])) is False


def test_two_connections_cannot_submit_distinct_requests_for_one_meeting(repo, tmp_path, meeting):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    other = Repository(tmp_path / "index.sqlite")
    start = threading.Barrier(2)
    # Stretch the original check/insert race without relying on a lock-aware barrier.
    for r in (repo, other):
        original = r._x
        def delayed(sql, params=(), original=original):
            result = original(sql, params)
            if sql.startswith("SELECT id FROM desktop_commands"):
                time.sleep(0.1)
            return result
        r._x = delayed
    def submit(r, rid):
        start.wait()
        try:
            return Commands(r).submit(rid, meeting.id, {"action": "reprocess", "stage": "deliver"})["state"]
        except ValueError:
            return "refused"
    try:
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(submit, repo, "race-a")
            b = pool.submit(submit, other, "race-b")
            assert sorted([a.result(), b.result()]) == ["queued", "refused"]
    finally:
        other.close()


def test_dead_executor_reconciles_durably_and_requires_explicit_ack(repo, tmp_path, meeting):
    import subprocess
    import sys
    queue = Commands(repo)
    body = {"action": "reprocess", "stage": "deliver"}
    queue.submit("orphan", meeting.id, body)
    code = '''
import os, sys
from pathlib import Path
from types import SimpleNamespace
from meeting_scribe.storage.repo import Repository
from meeting_scribe.desktop.control import execute_one
r = Repository(Path(sys.argv[1]))
execute_one(SimpleNamespace(repo=r, require=r.get_meeting, reprocess=lambda *a: os._exit(0)))
'''
    subprocess.run([sys.executable, "-c", code, str(tmp_path / "index.sqlite")], check=True)
    assert queue.get("orphan")["state"] == "unknown"
    assert repo._x("SELECT state FROM desktop_commands WHERE id='orphan'").fetchone()[0] == "unknown"
    with pytest.raises(ValueError):
        queue.submit("new", meeting.id, body)
    with pytest.raises(ValueError):
        queue.submit("new", meeting.id, body)
    assert execute_one(fake_service(repo, [])) is False
    assert queue.acknowledge("orphan")["state"] == "acknowledged"
    assert queue.acknowledge("orphan")["state"] == "acknowledged"
    assert queue.submit("orphan", meeting.id, body)["state"] == "acknowledged"
    assert queue.submit("new", meeting.id, body)["state"] == "queued"


def test_submit_reconciles_dead_executor_without_a_poll(repo, tmp_path, meeting):
    """After a page reload nobody polls the old id: submit itself must surface the orphan as unknown."""
    import subprocess
    import sys
    queue = Commands(repo)
    body = {"action": "reprocess", "stage": "deliver"}
    queue.submit("orphan2", meeting.id, body)
    code = """
import os, sys
from pathlib import Path
from types import SimpleNamespace
from meeting_scribe.storage.repo import Repository
from meeting_scribe.desktop.control import execute_one
r = Repository(Path(sys.argv[1]))
execute_one(SimpleNamespace(repo=r, require=r.get_meeting, reprocess=lambda *a: os._exit(0)))
"""
    subprocess.run([sys.executable, "-c", code, str(tmp_path / "index.sqlite")], check=True)
    with pytest.raises(ValueError):
        queue.submit("new2", meeting.id, body)
    raw = repo._x("SELECT state FROM desktop_commands WHERE id='orphan2'").fetchone()[0]
    assert raw == "unknown"


def test_pulse_is_throttled(repo):
    control._last_pulse.clear()
    control.pulse(repo)
    first = repo.kv_get(control.HEARTBEAT_KV)
    control.pulse(repo)
    assert repo.kv_get(control.HEARTBEAT_KV) == first
    control.pulse(repo, force=True)
    assert float(repo.kv_get(control.HEARTBEAT_KV)) >= float(first)


def test_gateway_worker_consumes_commands_and_publishes_heartbeat(tmp_path, meeting):
    from tests.unit.test_runtime import host
    from meeting_scribe.runtime import Runtime
    control._last_pulse.clear()
    h, _ = host(tmp_path)
    rt = Runtime(h)
    repo = rt.repo()
    repo.save_meeting(replace(meeting, state=MeetingState.DONE))
    queue = Commands(repo)
    queue.submit("worker-test", meeting.id, {"action": "reprocess", "stage": "deliver"})
    rt.start_pipeline()
    try:
        deadline = time.monotonic() + 5
        while queue.get("worker-test")["state"] in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(0.02)
        assert queue.get("worker-test")["state"] == "done"
        assert repo.get_job(meeting.id) is not None  # the worker's own reprocess queued the stage
        assert float(repo.kv_get(control.HEARTBEAT_KV)) > 0
    finally:
        rt.close()


def test_submit_refuses_a_discarded_recording(repo, meeting):
    repo.save_meeting(replace(meeting, state=MeetingState.EMPTY))
    with pytest.raises(ValueError, match="no audio"):
        Commands(repo).submit("request-e", meeting.id, {"action": "reprocess", "stage": "transcribe"})
