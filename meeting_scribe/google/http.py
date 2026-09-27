"""Minimal HTTP transport (stdlib ``urllib``) behind a seam so tests never touch the network.

A transport is ``request(method, url, *, headers, body, timeout) -> Response``. HTTP error statuses are
RETURNED (not raised) so callers decide what a 401/403/429 means; only connection-level failures
raise :class:`TransportError`. Response bodies are never logged: they can hold tokens.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol

USER_AGENT = "hermes-meeting-scribe"


class TransportError(ConnectionError):
    """Network-level failure (DNS, TLS, timeout): always retryable."""


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except ValueError:
            return {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Transport(Protocol):
    def request(self, method: str, url: str, *, headers: Optional[Mapping[str, str]] = None,
                body: Optional[bytes] = None, timeout: float = 30.0) -> Response: ...


class UrllibTransport:
    def request(self, method: str, url: str, *, headers: Optional[Mapping[str, str]] = None,
                body: Optional[bytes] = None, timeout: float = 30.0) -> Response:
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
            raise ValueError("refusing a non-HTTPS URL")
        req = urllib.request.Request(url, data=body, method=method,
                                     headers={"User-Agent": USER_AGENT, **dict(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https only (checked)
                return Response(resp.status, resp.read(), dict(resp.headers.items()))
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.read() or b"", dict(exc.headers.items()) if exc.headers else {})
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}") from None
