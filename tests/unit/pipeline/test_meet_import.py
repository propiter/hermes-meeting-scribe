"""Meet importer (DESIGN §17): enters at TRANSCRIBED, idempotent, window, readiness, lease, poller."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from meeting_scribe.domain.models import SOURCE_GOOGLE_MEET, MeetingState, Stage
from meeting_scribe.google.importer import MeetImporter, MeetPoller, lease_name
from meeting_scribe.google.meet_api import MeetClient
from meeting_scribe.pipeline.service import MeetingService
from meeting_scribe.storage.artifacts import read_transcript
from meeting_scribe.config import settings_from_mapping

from .test_runner import build, drain
from tests.unit.gmeet.fake_meet import FakeMeet
from tests.unit.gmeet.fakes import FakeTransport

LEASE = lease_name("main")  # one poller per space (DESIGN §23)
NOW = datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc)


class Creds:
    transport = None

    def access_token(self, *, force_refresh=False):
        return "tok"


@pytest.fixture
def world(prepo, layout, settings, clock):
    runner, _tr, analyzer, sinks = build(prepo, layout, settings, clock)
    service = MeetingService(prepo, layout, runner, settings, clock=clock, item_sinks=lambda: {},
                             catalogs=lambda: runner.stages.catalogs())
    meet = FakeMeet()
    client = MeetClient(Creds(), transport=FakeTransport(meet))
    importer = MeetImporter(space="main", service=lambda: service, client=lambda: client, clock=lambda: NOW)
    return service, runner, analyzer, sinks, meet, importer


def since():
    return NOW - timedelta(days=1)


def test_import_enters_transcribed_and_runs_analyze_deliver(world, layout):
    service, runner, analyzer, sinks, meet, importer = world
    meet.add("r1")
    report = importer.sync(ended_after=since())
    assert report.errors == [] and len(report.imported) == 1
    m = service.repo.get_meeting(report.imported[0])
    assert m.state is MeetingState.TRANSCRIBED and m.source == SOURCE_GOOGLE_MEET
    assert m.external_id == "conferenceRecords/r1" and m.language == "es" and m.guild_id == ""
    assert m.title == "Google Meet · 2026-09-26 15:00 · abc-mnop-xyz"
    assert {s.name for s in m.speakers} == {"Ana Example", "Guest Two"}
    utts = read_transcript(layout.meeting_folder(m))
    assert [(u.t0, u.speaker) for u in utts] == [(5.0, "Ana Example"), (60.0, "Guest Two"), (120.0, "Ana Example")]
    assert (layout.meeting_folder(m) / "transcript.md").exists()
    assert service.repo.get_job(m.id).stage is Stage.ANALYZE
    drain(runner)
    done = service.repo.get_meeting(m.id)
    assert done.state is MeetingState.DONE and analyzer.calls == 1 and sinks[0].calls == [m.id]
    assert service.search("presentación", "main")  # utterances are indexed for search


def test_same_conference_twice_is_imported_once(world):
    service, runner, _a, _s, meet, importer = world
    meet.add("r1")
    first = importer.sync(ended_after=since())
    second = importer.sync(ended_after=since())
    assert len(first.imported) == 1 and second.imported == [] and second.already == 1
    assert len(service.repo.list_meetings(limit=50)) == 1


def test_concurrent_importers_never_duplicate(world, prepo, layout, settings, clock):
    service, _r, _a, _s, meet, importer = world
    meet.add("r1")
    meet.add("r2")
    results = []
    barrier = threading.Barrier(4)

    def run():
        barrier.wait()
        results.append(importer.sync(ended_after=since()))
    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(len(r.imported) for r in results) == 2
    ids = [m.external_id for m in service.repo.list_meetings(limit=50)]
    assert sorted(ids) == ["conferenceRecords/r1", "conferenceRecords/r2"]
    folders = list((layout.meetings_dir()).rglob("transcript.jsonl"))
    assert len(folders) == 2  # losers cleaned their files


def test_transcript_not_ready_is_retried_next_poll(world):
    service, _r, _a, _s, meet, importer = world
    meet.add("r1", state="ENDED")
    meet.add("r2", state="")  # transcription was off
    rep = importer.sync(ended_after=since())
    assert rep.imported == [] and rep.pending == ["conferenceRecords/r1"] and rep.no_transcript == 1
    meet.transcript_state["r1"] = "FILE_GENERATED"
    rep = importer.sync(ended_after=since())
    assert len(rep.imported) == 1


def test_empty_entries_are_not_imported(world):
    _svc, _r, _a, _s, meet, importer = world
    meet.add("r1", entries=[])
    rep = importer.sync(ended_after=since())
    assert rep.imported == [] and rep.empty == 1


def test_window_uses_connected_at_and_never_imports_history(world):
    _svc, _r, _a, _s, meet, importer = world
    meet.add("old", end="2026-09-20T10:00:00Z", start="2026-09-20T09:00:00Z")
    meet.add("new")
    connected = datetime(2026, 9, 25, tzinfo=timezone.utc).timestamp()
    start = importer.window_start(connected_at=connected)
    rep = importer.sync(ended_after=start)
    assert rep.listed == 1 and len(rep.imported) == 1
    assert importer.window_start(days=90) == NOW - timedelta(days=30)  # Meet keeps 30 days
    assert importer.window_start() == NOW  # never connected: nothing is "new"


def test_dry_run_imports_nothing(world):
    service, _r, _a, _s, meet, importer = world
    meet.add("r1")
    rep = importer.sync(ended_after=since(), dry_run=True)
    assert rep.would_import == ["conferenceRecords/r1"] and rep.imported == []
    assert service.repo.list_meetings(limit=5) == []


def test_403_is_reported_and_status_recorded(world):
    _svc, _r, _a, _s, meet, importer = world
    meet.add("r1")
    meet.status["conferenceRecords"] = 403
    rep = importer.sync(ended_after=since())
    assert rep.errors and rep.errors[0].startswith("forbidden")
    st = importer.status()
    assert st["last_poll_ok"] == "0" and st["last_error"].startswith("forbidden")
    meet.status.clear()
    importer.sync(ended_after=since())
    st = importer.status()
    assert st["last_poll_ok"] == "1" and "last_error" not in st and st["last_import_meeting"]


def test_429_is_temporary(world):
    _svc, _r, _a, _s, meet, importer = world
    meet.add("r1")
    meet.status["conferenceRecords/r1/transcripts"] = 429
    rep = importer.sync(ended_after=since())
    assert rep.errors[0].startswith("temporary")


def test_poller_tick_needs_setting_and_lease(world, prepo):
    _svc, _r, _a, _s, meet, importer = world
    meet.add("r1")
    cfg = {"google_meet_enabled": False}
    settings = lambda space=None: settings_from_mapping(cfg)  # noqa: E731
    connected = (NOW - timedelta(days=1)).timestamp()
    a = MeetPoller(space="main", importer=lambda: importer, repo=lambda: prepo, settings=settings, connected_at=lambda: connected,
                   owner="host:1")
    b = MeetPoller(space="main", importer=lambda: importer, repo=lambda: prepo, settings=settings, connected_at=lambda: connected,
                   owner="host:2")
    assert a.tick() is None  # disabled
    cfg["google_meet_enabled"] = True
    rep = a.tick()
    assert rep is not None and len(rep.imported) == 1
    assert b.tick() is None  # the other process does not poll
    assert prepo.lease_owner(LEASE) == "host:1"
    a.stop()
    assert prepo.lease_owner(LEASE) is None
    assert b.tick() is not None  # takes over after a clean stop


def test_poller_thread_starts_and_stops_cleanly(world, prepo):
    _svc, _r, _a, _s, _meet, importer = world
    p = MeetPoller(space="main", importer=lambda: importer, repo=lambda: prepo,
                   settings=lambda: settings_from_mapping({}), connected_at=lambda: None, owner="x")
    p.start()
    p.start()  # idempotent
    assert p.running
    p.stop(timeout=5)
    assert not p.running


def test_an_import_a_private_rule_covers_is_private_from_the_start(prepo, layout, clock):
    """The row is private as soon as it exists: no window before the first persist (DESIGN §19.2)."""
    from meeting_scribe import privacy

    s = settings_from_mapping({"meeting_routes": ["meet:abc-* = 700:private"]})
    runner, *_ = build(prepo, layout, lambda space=None: s, clock)
    service = MeetingService(prepo, layout, runner, lambda space=None: s, clock=clock, item_sinks=lambda: {},
                             catalogs=lambda: runner.stages.catalogs())
    meet = FakeMeet()
    meet.add("r1")
    client = MeetClient(Creds(), transport=FakeTransport(meet))
    importer = MeetImporter(space="main", service=lambda: service, client=lambda: client, clock=lambda: NOW)
    [mid] = importer.sync(ended_after=since()).imported
    assert privacy.record(prepo, mid) == {"rule": "meet:abc-*", "channel": ""}
