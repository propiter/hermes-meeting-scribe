"""Meet importer resilience: one bad record never blocks the rest; errors never escape ``sync``;
records without a transcript stop being polled; 429 ``Retry-After`` is honoured."""
from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from meeting_scribe.google import oauth
from meeting_scribe.google.http import TransportError
from meeting_scribe.google.importer import MAX_RECORD_FAILURES, MeetImporter
from meeting_scribe.google.meet_api import MeetClient

from .test_meet_import import NOW, since, world  # noqa: F401 - fixture reuse
from tests.unit.gmeet.fakes import CLIENT_JSON, jresp


# -- finding 1: per-record isolation -----------------------------------------------------------------
@pytest.mark.parametrize("prefix,status", [
    ("conferenceRecords/old/transcripts", 403),
    ("conferenceRecords/old/transcripts", 404),
    ("conferenceRecords/old/transcripts/t1/entries", 500),
    ("conferenceRecords/old/participants", 403),
])
def test_one_failing_record_does_not_block_newer_ones(world, prefix, status):
    service, _r, _a, _s, meet, importer = world
    meet.add("old", end="2026-09-26T15:10:00Z")
    meet.add("new", end="2026-09-26T15:40:00Z")
    meet.status[prefix] = status
    rep = importer.sync(ended_after=since())
    assert len(rep.imported) == 1
    assert service.repo.get_meeting(rep.imported[0]).external_id == "conferenceRecords/new"
    assert rep.errors and rep.errors[0].startswith("conferenceRecords/old:")


def test_record_is_given_up_after_repeated_permanent_failures(world):
    _svc, _r, _a, _s, meet, importer = world
    meet.add("old", end="2026-09-26T15:10:00Z")
    meet.status["conferenceRecords/old/transcripts"] = 403
    for _ in range(MAX_RECORD_FAILURES):
        importer.sync(ended_after=since())
    meet.requests.clear()
    rep = importer.sync(ended_after=since())
    assert "conferenceRecords/old/transcripts" not in meet.requests  # no longer retried
    assert rep.given_up == ["conferenceRecords/old"] and rep.errors == []
    st = importer.status()
    assert st["records_given_up"] == "1" and "conferenceRecords/old" in st["records_given_up_last"]


def test_transient_5xx_does_not_count_towards_giving_up(world):
    _svc, _r, _a, _s, meet, importer = world
    meet.add("old", end="2026-09-26T15:10:00Z")
    meet.status["conferenceRecords/old/transcripts"] = 503
    for _ in range(MAX_RECORD_FAILURES + 2):
        rep = importer.sync(ended_after=since())
    assert rep.given_up == [] and rep.errors
    meet.status.clear()
    assert len(importer.sync(ended_after=since()).imported) == 1


def test_429_aborts_the_pass_and_is_honoured(world):
    _svc, _r, _a, _s, meet, importer = world
    meet.add("a", end="2026-09-26T15:10:00Z")
    meet.add("b", end="2026-09-26T15:40:00Z")
    meet.status["conferenceRecords/a/transcripts"] = 429
    meet.retry_after = "120"
    rep = importer.sync(ended_after=since())
    assert rep.imported == [] and rep.errors[0].startswith("temporary")
    assert "conferenceRecords/b/transcripts" not in meet.requests  # the pass stopped at the 429
    assert importer.backoff_until() == NOW + timedelta(seconds=120)


# -- finding 2: nothing escapes sync, status always recorded -------------------------------------------
def _creds_with(tmp_path, meet, fail):
    files = oauth.GoogleFiles(lambda: tmp_path / "d")
    oauth.write_private_json(files.client_path, CLIENT_JSON)
    files.write_token({"access_token": "a", "refresh_token": "r", "expires_at": 0})

    class T:
        def request(self, method, url, **kw):
            if "oauth2" in url:
                return fail()
            return meet(method, url, kw.get("headers") or {}, kw.get("body"))
    return oauth.GoogleCredentials(files, transport=T())


def test_transport_error_on_refresh_is_reported_not_raised(world, tmp_path):
    service, _r, _a, _s, meet, _ = world

    def boom():
        raise TransportError("URLError: timed out")
    creds = _creds_with(tmp_path, meet, boom)
    imp = MeetImporter(service=lambda: service, client=lambda: MeetClient(creds), clock=lambda: NOW)
    meet.add("r1")
    rep = imp.sync(ended_after=since())
    assert rep.errors and "unreachable" in rep.errors[0]
    st = imp.status()
    assert st["last_poll_ok"] == "0" and st["last_poll_at"]


def test_refresh_and_exchange_wrap_transport_errors():
    class T:
        def request(self, *a, **kw):
            raise TransportError("URLError: timed out")
    client = oauth.ClientConfig("id", "secret")
    with pytest.raises(oauth.GoogleAuthError, match="unreachable"):
        oauth.refresh_token(T(), client, {"refresh_token": "r"})
    with pytest.raises(oauth.GoogleAuthError, match="unreachable"):
        oauth.exchange_code(T(), client, code="c", verifier="v" * 50, redirect_uri="http://127.0.0.1:1")


def test_refresh_without_access_token_is_an_auth_error():
    class T:
        def request(self, *a, **kw):
            return jresp(200, {"expires_in": 10})
    with pytest.raises(oauth.GoogleAuthError):
        oauth.refresh_token(T(), oauth.ClientConfig("id", "secret"), {"refresh_token": "r"})


@pytest.mark.parametrize("exc", [sqlite3.OperationalError("database is locked"), ValueError("bad"), OSError("disk")])
def test_unexpected_errors_are_caught_and_status_is_written(world, exc):
    service, _r, _a, _s, meet, _ = world

    def broken_client():
        raise exc
    imp = MeetImporter(service=lambda: service, client=broken_client, clock=lambda: NOW)
    rep = imp.sync(ended_after=since())
    assert rep.errors and type(exc).__name__ in rep.errors[0]
    assert imp.status()["last_poll_ok"] == "0"


def test_status_write_failure_never_raises(world):
    service, _r, _a, _s, meet, importer = world
    meet.add("r1")
    service.repo.close()
    rep = importer.sync(ended_after=since())  # the DB is gone: reported, not raised
    assert rep.errors


# -- finding 7: records without transcript stop being polled ------------------------------------------
def test_old_records_without_transcript_are_not_polled_again(world):
    _svc, _r, _a, _s, meet, importer = world
    for i in range(5):
        meet.add(f"n{i}", state=None, start="2026-09-26T13:00:00Z", end="2026-09-26T14:00:00Z")  # ended 2 h ago
    meet.add("recent", state=None)  # ended 30 min ago: transcription may still show up
    importer.sync(ended_after=since())
    meet.requests.clear()
    rep = importer.sync(ended_after=since())
    transcript_calls = [p for p in meet.requests if p.endswith("/transcripts")]
    assert transcript_calls == ["conferenceRecords/recent/transcripts"]
    assert rep.skipped == 5 and rep.no_transcript == 1


def test_poller_skips_while_google_asked_to_wait(world, prepo):
    from meeting_scribe.config import settings_from_mapping
    from meeting_scribe.google.importer import MeetPoller
    _svc, _r, _a, _s, meet, importer = world
    meet.add("r1")
    meet.status["conferenceRecords"] = 429
    meet.retry_after = "600"
    p = MeetPoller(importer=lambda: importer, repo=lambda: prepo,
                   settings=lambda: settings_from_mapping({"google_meet_enabled": True}),
                   connected_at=lambda: (NOW - timedelta(days=1)).timestamp(), owner="x")
    importer.sync(ended_after=since())  # the listing itself got 429: aborts, remembers Retry-After
    assert importer.backoff_until() == NOW + timedelta(seconds=600)
    meet.status.clear()
    meet.requests.clear()
    assert p.tick() is None and meet.requests == []


def test_given_up_record_does_not_mark_the_poll_failed(world):
    _svc, _r, _a, _s, meet, importer = world
    meet.add("old")
    meet.status["conferenceRecords/old/transcripts"] = 404
    for _ in range(MAX_RECORD_FAILURES + 1):
        importer.sync(ended_after=since())
    st = importer.status()
    assert st["last_poll_ok"] == "1" and st["records_given_up"] == "1"
