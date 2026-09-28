"""Crash leftovers of an import are healed while running (finding 13); the poller stops promptly and
releases its lease from its own thread (finding 14)."""
from __future__ import annotations

import threading
import time
from datetime import timedelta

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.domain.models import MeetingState
from meeting_scribe.google.importer import MeetPoller

from .test_meet_import import LEASE, NOW, since, world  # noqa: F401 - fixture reuse


# -- finding 13 ------------------------------------------------------------------------------------
def test_transcribed_row_without_job_is_requeued_by_the_running_worker(world):
    service, runner, *_rest, meet, importer = world
    meet.add("r1")
    mid = importer.sync(ended_after=since()).imported[0]
    service.repo._x("DELETE FROM jobs WHERE meeting_id=?", (mid,))  # crash between the row and the enqueue
    runner.recover(())  # start-up already happened before the crash in real life
    service.repo._x("DELETE FROM jobs WHERE meeting_id=?", (mid,))
    runner.STRAGGLER_AGE_SECONDS = -5  # the row was just written (wall clock)
    runner.clock.advance(runner.RECLAIM_SECONDS + 1)
    runner.run_once()
    assert service.repo.get_job(mid) is not None
    assert service.repo.get_meeting(mid).state is not MeetingState.TRANSCRIBED  # it was processed


def test_fresh_rows_are_not_touched_by_the_sweep(world):
    service, runner, *_rest, meet, importer = world
    meet.add("r1")
    mid = importer.sync(ended_after=since()).imported[0]
    runner.recover(())
    service.repo._x("DELETE FROM jobs WHERE meeting_id=?", (mid,))
    runner.clock.advance(runner.RECLAIM_SECONDS + 1)
    runner.run_once()  # default age guard: a row updated seconds ago may still be mid-import elsewhere
    assert service.repo.get_job(mid) is None


def test_orphan_folder_of_a_crashed_import_is_removed(world, layout, monkeypatch):
    service, _runner, *_rest, meet, importer = world
    meet.add("r1")

    def crash(*a, **k):
        raise SystemExit("killed")  # process dies after the files, before the row
    monkeypatch.setattr(service.repo, "create_imported_meeting", crash)
    try:
        importer.sync(ended_after=since())
    except SystemExit:
        pass
    monkeypatch.undo()
    folders = [p for p in layout.meetings_dir().rglob("meta.json")]
    assert len(folders) == 1  # the leftover
    service.clean_import_leftovers(older_than=0)
    assert list(layout.meetings_dir().rglob("meta.json")) == []
    rep = importer.sync(ended_after=since())
    assert len(rep.imported) == 1 and len(list(layout.meetings_dir().rglob("meta.json"))) == 1


def test_leftover_cleanup_keeps_folders_of_committed_rows(world, layout):
    service, _runner, *_rest, meet, importer = world
    meet.add("r1")
    mid = importer.sync(ended_after=since()).imported[0]
    service.clean_import_leftovers(older_than=0)
    assert (layout.meeting_folder(service.repo.get_meeting(mid)) / "meta.json").exists()


def test_leftover_cleanup_respects_the_age_guard(world, layout, monkeypatch):
    service, _runner, *_rest, meet, importer = world
    meet.add("r1")
    monkeypatch.setattr(service.repo, "create_imported_meeting", lambda *a: (_ for _ in ()).throw(SystemExit()))
    try:
        importer.sync(ended_after=since())
    except SystemExit:
        pass
    service.clean_import_leftovers()  # default guard: might be another process importing right now
    assert len(list(layout.meetings_dir().rglob("meta.json"))) == 1


# -- finding 14 ------------------------------------------------------------------------------------
def test_sync_stops_between_pages_without_importing_half_a_transcript(world):
    service, *_rest, meet, importer = world
    meet.add("r1")
    meet.page_size = 1  # 3 entries -> 3 pages
    stop = threading.Event()
    real = meet.__call__

    def handler(method, url, headers, body):
        resp = real(method, url, headers, body)
        if "/entries" in url:
            stop.set()
        return resp
    importer._client().transport.handler = handler
    rep = importer.sync(ended_after=since(), should_stop=stop.is_set)
    assert rep.imported == [] and rep.errors == []
    assert sum("/entries" in r for r in meet.requests) == 1
    assert service.repo.known_external_ids("main", "google_meet") == set()


def test_stop_does_not_wait_for_the_rest_of_the_pass_and_the_thread_releases_the_lease(world, prepo):
    _svc, *_rest, meet, importer = world
    for i in range(5):
        meet.add(f"r{i}", end=f"2026-09-26T15:3{i}:00Z")
    inside, release = threading.Event(), threading.Event()
    real = meet.__call__

    def handler(method, url, headers, body):
        if url.split("?")[0].endswith("/transcripts"):
            inside.set()
            release.wait(5)
        return real(method, url, headers, body)
    importer._client().transport.handler = handler
    p = MeetPoller(space="main", importer=lambda: importer, repo=lambda: prepo,
                   settings=lambda: settings_from_mapping({"google_meet_enabled": True}),
                   connected_at=lambda: (NOW - timedelta(days=1)).timestamp(), owner="me")
    p.FIRST_DELAY = 0.01
    p.start()
    assert inside.wait(5)
    thread = p._thread
    t0 = time.monotonic()
    p.stop(timeout=0.2)
    assert time.monotonic() - t0 < 1
    assert prepo.lease_owner(LEASE) == "me"  # the thread still runs: it owns the release
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert [r for r in meet.requests if r != "conferenceRecords"] == ["conferenceRecords/r0/transcripts"]
    assert prepo.lease_owner(LEASE) is None


def test_stop_while_the_caller_holds_the_runtime_lock_does_not_deadlock(world, prepo):
    """Runtime.close() holds its lock while stopping; the exiting thread must not need it."""
    _svc, *_rest, meet, importer = world
    runtime_lock = threading.RLock()

    def repo():
        with runtime_lock:
            return prepo
    p = MeetPoller(space="main", importer=lambda: importer, repo=repo,
                   settings=lambda: settings_from_mapping({"google_meet_enabled": True}),
                   connected_at=lambda: (NOW - timedelta(days=1)).timestamp(), owner="me")
    p.FIRST_DELAY = 0.01
    p.start()
    deadline = time.monotonic() + 5
    while prepo.lease_owner(LEASE) != "me" and time.monotonic() < deadline:
        time.sleep(0.01)
    with runtime_lock:
        t0 = time.monotonic()
        p.stop(timeout=5)
        assert time.monotonic() - t0 < 2
    assert prepo.lease_owner(LEASE) is None
