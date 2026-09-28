"""Meet REST client (pagination, 401 refresh-once, 403, 429) and entries → Utterance conversion."""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone

import pytest

from meeting_scribe.google import convert
from meeting_scribe.google.http import TransportError
from meeting_scribe.google.meet_api import MeetAuthError, MeetClient, MeetForbidden, MeetRetryLater

from .fakes import FakeTransport, jresp


class StubCreds:
    def __init__(self):
        self.refreshes = 0
        self.transport = None

    def access_token(self, *, force_refresh=False):
        if force_refresh:
            self.refreshes += 1
        return f"tok{self.refreshes}"


def q(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def test_list_conference_records_filters_and_paginates():
    def handler(method, url, headers, body):
        assert url.startswith("https://meet.googleapis.com/v2/conferenceRecords?")
        page = q(url).get("pageToken")
        if page is None:
            return jresp(200, {"conferenceRecords": [{"name": "conferenceRecords/a", "endTime": "2026-09-02T00:00:00Z"},
                                                     {"name": "conferenceRecords/live"}],
                               "nextPageToken": "p2"})
        return jresp(200, {"conferenceRecords": [{"name": "conferenceRecords/b", "endTime": "2026-09-03T00:00:00Z"}]})
    tr = FakeTransport(handler)
    client = MeetClient(StubCreds(), transport=tr)
    recs = client.conference_records(ended_after="2026-09-01T00:00:00Z")
    # Conferences still in progress (no endTime) are skipped client-side.
    assert [r["name"] for r in recs] == ["conferenceRecords/a", "conferenceRecords/b"]
    # The live API rejects ``end_time IS NOT NULL`` (HTTP 400 "Invalid filter"); only comparisons are sent.
    assert q(tr.calls[0]["url"])["filter"] == 'end_time>="2026-09-01T00:00:00Z"'
    assert q(tr.calls[1]["url"])["pageToken"] == "p2"
    assert tr.calls[0]["headers"]["Authorization"] == "Bearer tok0"


def test_list_conference_records_without_window_sends_no_filter():
    tr = FakeTransport(lambda m, url, h, b: jresp(200, {"conferenceRecords": [{"name": "conferenceRecords/live"}]}))
    assert MeetClient(StubCreds(), transport=tr).conference_records() == []
    assert "filter" not in q(tr.calls[0]["url"])


def test_entries_path_and_key():
    tr = FakeTransport(lambda m, url, h, b: jresp(200, {"transcriptEntries": [{"text": "hi"}]}))
    out = MeetClient(StubCreds(), transport=tr).entries("conferenceRecords/a/transcripts/t1")
    assert out == [{"text": "hi"}]
    assert tr.calls[0]["url"].startswith("https://meet.googleapis.com/v2/conferenceRecords/a/transcripts/t1/entries?")


def test_401_refreshes_once_then_retries():
    seen = []

    def handler(m, url, headers, b):
        seen.append(headers["Authorization"])
        return jresp(401) if len(seen) == 1 else jresp(200, {"transcripts": []})
    creds = StubCreds()
    assert MeetClient(creds, transport=FakeTransport(handler)).transcripts("conferenceRecords/a") == []
    assert seen == ["Bearer tok0", "Bearer tok1"] and creds.refreshes == 1


def test_401_twice_is_auth_error():
    creds = StubCreds()
    with pytest.raises(MeetAuthError):
        MeetClient(creds, transport=FakeTransport(lambda *a: jresp(401))).transcripts("conferenceRecords/a")
    assert creds.refreshes == 1


def test_403_and_429_and_network():
    err = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Meet API disabled"}}
    with pytest.raises(MeetForbidden, match="PERMISSION_DENIED"):
        MeetClient(StubCreds(), transport=FakeTransport(lambda *a: jresp(403, err))).participants("conferenceRecords/a")
    with pytest.raises(MeetRetryLater) as info:
        MeetClient(StubCreds(), transport=FakeTransport(lambda *a: jresp(429, {}, {"Retry-After": "30"}))).transcripts("x")
    assert info.value.retry_after == 30.0
    with pytest.raises(MeetRetryLater):
        MeetClient(StubCreds(), transport=FakeTransport(lambda *a: jresp(503))).transcripts("x")

    def boom(*a):
        raise TransportError("timeout")
    with pytest.raises(MeetRetryLater):
        MeetClient(StubCreds(), transport=FakeTransport(boom)).transcripts("x")


# -- conversion -----------------------------------------------------------------------------------
START = datetime(2026, 9, 20, 15, 0, 0, tzinfo=timezone.utc)
PARTICIPANTS = [
    {"name": "conferenceRecords/a/participants/111", "signedinUser": {"user": "users/1", "displayName": "Ada Lovel"}},
    {"name": "conferenceRecords/a/participants/222", "anonymousUser": {"displayName": "Guest  Visitor"}},
    {"name": "conferenceRecords/a/participants/333", "phoneUser": {"displayName": "Phone ***-123"}},
]
ENTRIES = [
    {"participant": "conferenceRecords/a/participants/222", "text": "Second line", "languageCode": "es-ES",
     "startTime": "2026-09-20T15:00:12.500Z", "endTime": "2026-09-20T15:00:15Z"},
    {"participant": "conferenceRecords/a/participants/111", "text": "  Hello   everyone ", "languageCode": "es-ES",
     "startTime": "2026-09-20T15:00:03.123456789Z", "endTime": "2026-09-20T15:00:06Z"},
    {"participant": "conferenceRecords/a/participants/333", "text": "From the phone", "languageCode": "en-US",
     "startTime": "2026-09-20T15:01:00Z", "endTime": "2026-09-20T15:01:02Z"},
    {"participant": "conferenceRecords/a/participants/999", "text": "Who am I", "languageCode": "es-419",
     "startTime": "2026-09-20T15:02:00Z", "endTime": "2026-09-20T15:02:01Z"},
    {"participant": "conferenceRecords/a/participants/111", "text": "", "startTime": "2026-09-20T15:03:00Z"},
]


def test_parse_time_nanos_and_z():
    assert convert.parse_time("2026-09-20T15:00:03.123456789Z") == datetime(2026, 9, 20, 15, 0, 3, 123456,
                                                                            tzinfo=timezone.utc)
    assert convert.parse_time("2026-09-20T15:00:03Z").tzinfo is not None
    assert convert.parse_time("nope") is None and convert.parse_time(None) is None


def test_entries_to_utterances_relative_times_and_names():
    speakers = convert.speakers_from(PARTICIPANTS, ENTRIES)
    by = {s.user_id: s.name for s in speakers}
    assert by == {"gmeet:222": "Guest Visitor", "gmeet:111": "Ada Lovel", "gmeet:333": "Phone ***-123",
                  "gmeet:999": "Participant 1"}
    utts = convert.to_utterances(ENTRIES, speakers, START)
    assert [(u.t0, u.speaker, u.text) for u in utts] == [
        (3.123, "Ada Lovel", "Hello everyone"), (12.5, "Guest Visitor", "Second line"),
        (60.0, "Phone ***-123", "From the phone"), (120.0, "Participant 1", "Who am I")]
    assert utts[0].t1 == 6.0 and utts[0].speaker_id == "gmeet:111" and utts[0].confidence == 1.0


def test_majority_language_is_short_code():
    assert convert.majority_language(ENTRIES) == "es"
    assert convert.majority_language([{"text": "x"}]) is None


def test_default_title():
    assert convert.default_title(START) == "Google Meet · 2026-09-20 15:00"
    assert convert.default_title(START, "abc-mnop-xyz").endswith("· abc-mnop-xyz")


def test_only_signed_in_attendees_carry_a_google_account():
    parts = [{"name": "conferenceRecords/r/participants/1", "signedinUser": {"user": "users/71", "displayName": "Ana"}},
             {"name": "conferenceRecords/r/participants/2", "anonymousUser": {"displayName": "users/71"}},
             {"name": "conferenceRecords/r/participants/3", "signedinUser": {"user": "ana@example.com",
                                                                            "displayName": "Ana"}}]
    entries = [{"participant": f"conferenceRecords/r/participants/{i}"} for i in (1, 2, 3)]
    speakers = convert.speakers_from(parts, entries)
    assert [s.google_user for s in speakers] == ["users/71", "", ""]
