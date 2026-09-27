"""Minimal Google Meet REST v2 client (DESIGN §17), stdlib only.

Resources used (verified against https://developers.google.com/workspace/meet/api/reference/rest/v2):

* ``GET conferenceRecords`` — ``filter`` on ``start_time``/``end_time`` comparisons only (e.g.
  ``end_time>="2026-01-01T00:00:00Z"``; ``IS NOT NULL`` is rejected live), ``pageSize`` ≤ 100, ``pageToken``.
* ``GET {conferenceRecords/*}/transcripts`` — ``state`` STARTED | ENDED | FILE_GENERATED.
* ``GET {conferenceRecords/*/transcripts/*}/entries`` — ``participant``, ``text``, ``languageCode``,
  ``startTime``, ``endTime``; ``pageSize`` ≤ 100.
* ``GET {conferenceRecords/*}/participants`` — ``signedinUser|anonymousUser|phoneUser.displayName``;
  ``pageSize`` ≤ 250.
* ``GET {spaces/*}`` — ``meetingCode`` (best effort, used only as a label).

Error policy: 401 → refresh the token and retry ONCE; 403 → :class:`MeetForbidden` (scope/admin
policy; not retried until the next poll); 429/5xx/network → :class:`MeetRetryLater`.
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Iterator, Mapping, Optional

from .http import Response, Transport, TransportError, UrllibTransport
from .oauth import GoogleCredentials

BASE_URL = "https://meet.googleapis.com/v2/"
MAX_PAGES = 500  # 50k entries at pageSize=100: far beyond a 10-hour meeting; a runaway guard


class MeetApiError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class MeetAuthError(MeetApiError):
    """Still 401 after a token refresh."""


class MeetForbidden(MeetApiError):
    """403: the scope was not granted, the Meet API is disabled, or an admin policy blocks access."""


class MeetRetryLater(MeetApiError):
    """429 / 5xx / network: try again on the next poll."""

    def __init__(self, message: str, *, status: int = 0, retry_after: Optional[float] = None) -> None:
        super().__init__(message, status=status)
        self.retry_after = retry_after


def _api_message(resp: Response) -> str:
    data = resp.json()
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return f"HTTP {resp.status} {err.get('status') or ''}: {str(err.get('message') or '')[:300]}".strip()
    return f"HTTP {resp.status}"


class MeetClient:
    def __init__(self, creds: GoogleCredentials, *, transport: Optional[Transport] = None,
                 base_url: str = BASE_URL) -> None:
        self.creds = creds
        self.transport = transport or creds.transport or UrllibTransport()
        self.base_url = base_url.rstrip("/") + "/"

    # -- plumbing -----------------------------------------------------------------------------
    def _url(self, path: str, params: Optional[Mapping[str, Any]] = None) -> str:
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        query = f"?{urllib.parse.urlencode(clean)}" if clean else ""
        return f"{self.base_url}{urllib.parse.quote(path, safe='/-_.~')}{query}"

    def _send(self, url: str, token: str) -> Response:
        try:
            return self.transport.request("GET", url, headers={"Authorization": f"Bearer {token}",
                                                               "Accept": "application/json"}, timeout=30.0)
        except TransportError as exc:
            raise MeetRetryLater(f"network error: {exc}") from None

    def get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        url = self._url(path, params)
        resp = self._send(url, self.creds.access_token())
        if resp.status == 401:
            resp = self._send(url, self.creds.access_token(force_refresh=True))
            if resp.status == 401:
                raise MeetAuthError(_api_message(resp), status=401)
        if resp.status == 403:
            raise MeetForbidden(_api_message(resp), status=403)
        if resp.status == 429 or resp.status >= 500:
            retry = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            try:
                after = float(retry) if retry else None
            except ValueError:
                after = None
            raise MeetRetryLater(_api_message(resp), status=resp.status, retry_after=after)
        if not resp.ok:
            raise MeetApiError(_api_message(resp), status=resp.status)
        data = resp.json()
        return data if isinstance(data, dict) else {}

    def paged(self, path: str, key: str, params: Optional[Mapping[str, Any]] = None) -> Iterator[dict[str, Any]]:
        token: Optional[str] = None
        for _ in range(MAX_PAGES):
            data = self.get(path, {**dict(params or {}), "pageToken": token})
            yield from (x for x in data.get(key) or () if isinstance(x, dict))
            token = data.get("nextPageToken")
            if not token:
                return
        raise MeetApiError(f"{path}: more than {MAX_PAGES} pages")

    # -- resources ------------------------------------------------------------------------------
    def conference_records(self, *, ended_after: Optional[str] = None,
                           started_after: Optional[str] = None) -> list[dict[str, Any]]:
        """Finished conferences, newest first (API default order).

        The live API rejects ``end_time IS NOT NULL`` (HTTP 400 "Invalid filter was provided"), so only
        time comparisons go to the server and conferences still in progress (no ``endTime``) are
        dropped here.
        """
        parts = []
        if ended_after:
            parts.append(f'end_time>="{ended_after}"')
        if started_after:
            parts.append(f'start_time>="{started_after}"')
        params: dict[str, Any] = {"pageSize": 100}
        if parts:
            params["filter"] = " AND ".join(parts)
        return [r for r in self.paged("conferenceRecords", "conferenceRecords", params) if r.get("endTime")]

    def transcripts(self, record: str) -> list[dict[str, Any]]:
        return list(self.paged(f"{record}/transcripts", "transcripts", {"pageSize": 100}))

    def entries(self, transcript: str) -> list[dict[str, Any]]:
        return list(self.paged(f"{transcript}/entries", "transcriptEntries", {"pageSize": 100}))

    def participants(self, record: str) -> list[dict[str, Any]]:
        return list(self.paged(f"{record}/participants", "participants", {"pageSize": 250}))

    def space(self, name: str) -> dict[str, Any]:
        return self.get(name)
