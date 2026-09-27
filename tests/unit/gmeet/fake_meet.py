"""A scripted Google Meet REST v2 server for unit/integration tests (transport level, no network)."""
from __future__ import annotations

import urllib.parse
from typing import Any

from .fakes import jresp

START = "2026-09-26T15:00:00Z"
END = "2026-09-26T15:30:00Z"


def record(rid: str, *, start: str = START, end: str = END, space: str = "spaces/sp1") -> dict:
    return {"name": f"conferenceRecords/{rid}", "startTime": start, "endTime": end,
            "expireTime": "2026-10-26T15:30:00Z", "space": space}


def entries_for(rid: str) -> list[dict]:
    p = f"conferenceRecords/{rid}/participants"
    return [
        {"name": f"conferenceRecords/{rid}/transcripts/t1/entries/e1", "participant": f"{p}/p1",
         "text": "Hola, revisemos el lanzamiento.", "languageCode": "es-ES",
         "startTime": "2026-09-26T15:00:05Z", "endTime": "2026-09-26T15:00:08Z"},
        {"name": f"conferenceRecords/{rid}/transcripts/t1/entries/e2", "participant": f"{p}/p2",
         "text": "Yo preparo la presentación para el viernes.", "languageCode": "es-ES",
         "startTime": "2026-09-26T15:01:00Z", "endTime": "2026-09-26T15:01:04Z"},
        {"name": f"conferenceRecords/{rid}/transcripts/t1/entries/e3", "participant": f"{p}/p1",
         "text": "Perfecto, decidimos lanzar el lunes.", "languageCode": "es-ES",
         "startTime": "2026-09-26T15:02:00Z", "endTime": "2026-09-26T15:02:03Z"},
    ]


class FakeMeet:
    """Holds records/transcripts/entries/participants; answers like meet.googleapis.com/v2."""

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.transcript_state: dict[str, str] = {}
        self.entries: dict[str, list[dict]] = {}
        self.status: dict[str, int] = {}  # path prefix -> forced HTTP status
        self.requests: list[str] = []
        self.page_size = 2
        self.retry_after: Any = None  # Retry-After header sent with forced 429/503 answers

    def add(self, rid: str, *, state: str = "FILE_GENERATED", entries: Any = None, **kw: Any) -> None:
        self.records.append(record(rid, **kw))
        if state:
            self.transcript_state[rid] = state
        self.entries[rid] = entries_for(rid) if entries is None else entries

    def _page(self, key: str, items: list, query: dict) -> Any:
        start = int(query.get("pageToken") or 0)
        chunk = items[start:start + self.page_size]
        body: dict[str, Any] = {key: chunk}
        if start + self.page_size < len(items):
            body["nextPageToken"] = str(start + self.page_size)
        return jresp(200, body)

    def __call__(self, method: str, url: str, headers: dict, body: Any) -> Any:
        parts = urllib.parse.urlsplit(url)
        assert parts.netloc == "meet.googleapis.com" and parts.path.startswith("/v2/"), url
        assert headers.get("Authorization", "").startswith("Bearer ")
        path = urllib.parse.unquote(parts.path[len("/v2/"):])
        query = dict(urllib.parse.parse_qsl(parts.query))
        self.requests.append(path)
        for prefix, status in self.status.items():
            if path.startswith(prefix):
                headers = {"Retry-After": str(self.retry_after)} if self.retry_after is not None else {}
                return jresp(status, {"error": {"code": status, "status": "FORCED", "message": "forced"}}, headers)
        segs = path.split("/")
        if path == "conferenceRecords":
            flt = query.get("filter", "")
            recs = [r for r in self.records if not ('end_time>="' in flt and
                                                    r["endTime"] < flt.split('end_time>="')[1].split('"')[0])]
            return self._page("conferenceRecords", recs, query)
        if len(segs) == 3 and segs[2] == "transcripts":
            rid = segs[1]
            st = self.transcript_state.get(rid)
            items = [{"name": f"conferenceRecords/{rid}/transcripts/t1", "state": st,
                      "startTime": START}] if st else []
            return self._page("transcripts", items, query)
        if len(segs) == 5 and segs[4] == "entries":
            return self._page("transcriptEntries", self.entries.get(segs[1], []), query)
        if len(segs) == 3 and segs[2] == "participants":
            rid = segs[1]
            items = [{"name": f"conferenceRecords/{rid}/participants/p1", "signedinUser": {"displayName": "Ana Example"}},
                     {"name": f"conferenceRecords/{rid}/participants/p2", "anonymousUser": {"displayName": "Guest Two"}}]
            return self._page("participants", items, query)
        if segs[0] == "spaces":
            return jresp(200, {"name": path, "meetingCode": "abc-mnop-xyz"})
        return jresp(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": path}})
