"""Google Meet with several spaces (DESIGN §23): overlapping meetings are all imported, each space
keeps its own status, record memory, ``Retry-After`` pause and lease."""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.google.importer import MeetImporter, MeetPoller, lease_name
from meeting_scribe.google.meet_api import MeetClient
from meeting_scribe.pipeline.service import MeetingService

from .test_meet_import import NOW, Creds, since
from .test_runner import build
from tests.unit.gmeet.fake_meet import FakeMeet
from tests.unit.gmeet.fakes import FakeTransport

# Two meetings of the same afternoon that overlap in time (15:00-15:30 and 15:10-15:50).
OVERLAP = dict(start="2026-09-26T15:10:00Z", end="2026-09-26T15:50:00Z", space="spaces/sp2")


@pytest.fixture
def two(prepo, layout, settings, clock):
    runner, _tr, _an, _sinks = build(prepo, layout, settings, clock)
    service = MeetingService(prepo, layout, runner, settings, clock=clock, item_sinks=lambda: {},
                             catalogs=lambda: runner.stages.catalogs())
    meets = {"main": FakeMeet(), "team": FakeMeet()}  # each space's own Google account
    importers = {slug: MeetImporter(space=slug, service=lambda: service,
                                    client=lambda m=m: MeetClient(Creds(), transport=FakeTransport(m)),
                                    clock=lambda: NOW)
                 for slug, m in meets.items()}
    return service, meets, importers


def external_ids(service, space):
    return sorted(m.external_id for m in service.repo.list_meetings(limit=50, space=space))


def test_overlapping_meetings_of_one_space_are_all_imported(two):
    service, meets, importers = two
    meets["main"].add("r1")
    meets["main"].add("r2", **OVERLAP)
    report = importers["main"].sync(ended_after=since())
    assert report.errors == [] and len(report.imported) == 2
    assert external_ids(service, "main") == ["conferenceRecords/r1", "conferenceRecords/r2"]
    assert external_ids(service, "team") == []


def test_overlapping_meetings_of_two_spaces_import_in_parallel(two):
    service, meets, importers = two
    meets["main"].add("r1")
    meets["team"].add("r9", **OVERLAP)
    meets["team"].add("r1")  # the same record name in another account: its own meeting
    reports = {}
    barrier = threading.Barrier(2)

    def run(slug):
        barrier.wait()
        reports[slug] = importers[slug].sync(ended_after=since())
    threads = [threading.Thread(target=run, args=(slug,)) for slug in importers]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(reports["main"].imported) == 1 and len(reports["team"].imported) == 2
    assert external_ids(service, "main") == ["conferenceRecords/r1"]
    assert external_ids(service, "team") == ["conferenceRecords/r1", "conferenceRecords/r9"]
    assert {m.space for m in service.repo.list_meetings(limit=50)} == {"main", "team"}


def test_status_and_backoff_are_kept_per_space(two):
    service, meets, importers = two
    meets["main"].add("r1")
    meets["team"].add("r2", **OVERLAP)
    meets["team"].status["conferenceRecords"] = 429
    meets["team"].retry_after = 600
    bad = importers["team"].sync(ended_after=since())
    good = importers["main"].sync(ended_after=since())
    assert bad.errors and bad.errors[0].startswith("temporary") and len(good.imported) == 1
    assert importers["team"].backoff_until() == NOW + timedelta(seconds=600)
    assert importers["main"].backoff_until() is None
    assert importers["team"].status()["last_poll_ok"] == "0"
    assert importers["main"].status()["last_poll_ok"] == "1" and "last_error" not in importers["main"].status()


def test_pollers_of_two_spaces_hold_their_own_lease_and_pause(two, prepo):
    service, meets, importers = two
    meets["main"].add("r1")
    meets["team"].add("r2", **OVERLAP)
    importers["team"].set_status(retry_after_until=(NOW + timedelta(hours=1)).isoformat())
    on = lambda: settings_from_mapping({"google_meet_enabled": True})  # noqa: E731
    connected = (NOW - timedelta(days=1)).timestamp()
    pollers = {slug: MeetPoller(space=slug, importer=lambda s=slug: importers[s], repo=lambda: prepo, settings=on,
                                connected_at=lambda: connected, owner="host:1") for slug in importers}
    assert pollers["team"].tick() is None  # paused by Google: only that space waits
    report = pollers["main"].tick()
    assert report is not None and len(report.imported) == 1
    assert prepo.lease_owner(lease_name("main")) == "host:1" and prepo.lease_owner(lease_name("team")) == "host:1"
    importers["team"].set_status(retry_after_until=None)
    assert len(pollers["team"].tick().imported) == 1
    for p in pollers.values():
        p.stop()
    assert prepo.lease_owner(lease_name("main")) is None and prepo.lease_owner(lease_name("team")) is None
