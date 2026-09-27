"""Test doubles for the Google integration: a scripted HTTP transport (no network, ever)."""
from __future__ import annotations

import json
import urllib.parse
from typing import Any, Callable, Mapping, Optional

from meeting_scribe.google.http import Response

Handler = Callable[[str, str, Mapping[str, str], Optional[bytes]], Response]


def jresp(status: int, data: Any = None, headers: Optional[Mapping[str, str]] = None) -> Response:
    return Response(status, json.dumps(data if data is not None else {}).encode(), dict(headers or {}))


class FakeTransport:
    """Routes requests to a handler; records (method, url, headers, form/body)."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        form = dict(urllib.parse.parse_qsl(body.decode())) if body else {}
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "form": form})
        return self.handler(method, url, dict(headers or {}), body)


CLIENT_JSON = {"installed": {"client_id": "fake-client.apps.example", "client_secret": "fake-shh",
                             "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                             "token_uri": "https://oauth2.googleapis.com/token",
                             "redirect_uris": ["http://localhost"]}}
