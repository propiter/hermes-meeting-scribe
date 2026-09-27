"""Google OAuth 2.0 for installed apps (DESIGN §17): Authorization Code + PKCE + loopback redirect.

Every user brings THEIR OWN OAuth client (Cloud Console → "Desktop app" credentials JSON). The
client JSON and the token live under the profile's plugin data dir::

    <HERMES_HOME>/plugin-data/meeting-scribe/google/client.json   (0600)
    <HERMES_HOME>/plugin-data/meeting-scribe/google/token.json    (0600)

Only one scope is ever requested: ``meetings.space.readonly`` (sensitive, not restricted). No Drive,
no userinfo — so the account identity is unknown and status shows just "connected".

Endpoints and parameters follow https://developers.google.com/identity/protocols/oauth2/native-app.
Secrets (client_secret, tokens, codes) are never logged nor put in exception messages.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .http import Response, Transport, TransportError, UrllibTransport

SCOPE = "https://www.googleapis.com/auth/meetings.space.readonly"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
REFRESH_MARGIN = 120.0  # refresh this many seconds before expiry


class GoogleAuthError(RuntimeError):
    """OAuth failed (bad client file, denied consent, state mismatch, token endpoint error)."""


class GoogleDisconnected(GoogleAuthError):
    """No usable token (never connected, revoked, expired refresh token → ``invalid_grant``)."""


# -- files ------------------------------------------------------------------------------------------
def write_private_json(path: Path, data: Mapping[str, Any]) -> None:
    """Atomic write with mode 0600 from the first byte (the directory is made 0700)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dict(data), fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class GoogleFiles:
    """Paths of the per-profile Google credentials; the root is resolved on every call."""

    def __init__(self, data_dir: Callable[[], Path]) -> None:
        self._data_dir = data_dir

    @property
    def dir(self) -> Path:
        return Path(self._data_dir()) / "google"

    @property
    def client_path(self) -> Path:
        return self.dir / "client.json"

    @property
    def token_path(self) -> Path:
        return self.dir / "token.json"

    def read_token(self) -> Optional[dict[str, Any]]:
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def write_token(self, token: Mapping[str, Any]) -> None:
        write_private_json(self.token_path, token)

    def delete_token(self) -> None:
        self.token_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    client_secret: str
    token_uri: str = TOKEN_URI
    auth_uri: str = AUTH_URI

    def __repr__(self) -> str:  # never print the secret by accident
        return f"ClientConfig(client_id={self.client_id!r}, client_secret='***')"


def parse_client_json(data: Any) -> ClientConfig:
    """Cloud Console "Desktop app" JSON: ``{"installed": {"client_id", "client_secret", ...}}``."""
    if not isinstance(data, dict):
        raise GoogleAuthError("client file is not a JSON object")
    if "installed" not in data:
        kind = next(iter(data), "?") if data else "empty"
        raise GoogleAuthError(f"expected OAuth credentials of type 'Desktop app' (key 'installed'), got '{kind}'")
    inst = data["installed"]
    cid, secret = str(inst.get("client_id") or ""), str(inst.get("client_secret") or "")
    if not cid or not secret:
        raise GoogleAuthError("client file lacks client_id/client_secret")
    token_uri = str(inst.get("token_uri") or TOKEN_URI)
    auth_uri = str(inst.get("auth_uri") or AUTH_URI)
    for uri in (token_uri, auth_uri):
        if not uri.startswith("https://"):
            raise GoogleAuthError("client file has a non-HTTPS endpoint")
    return ClientConfig(cid, secret, token_uri, auth_uri)


def import_client_file(files: GoogleFiles, source: Path) -> ClientConfig:
    """Validate ``source`` and copy it (0600) into the profile's storage."""
    try:
        raw = json.loads(Path(source).expanduser().read_text(encoding="utf-8"))
    except OSError as exc:
        raise GoogleAuthError(f"cannot read client file: {exc.strerror or exc}") from None
    except ValueError:
        raise GoogleAuthError("client file is not valid JSON") from None
    client = parse_client_json(raw)
    write_private_json(files.client_path, raw)
    return client


def load_client(files: GoogleFiles) -> ClientConfig:
    try:
        raw = json.loads(files.client_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise GoogleDisconnected("no Google OAuth client stored; run `hermes meeting-scribe google connect`") from None
    return parse_client_json(raw)


# -- PKCE / URLs ------------------------------------------------------------------------------------
def make_verifier() -> str:
    """RFC 7636 code verifier: 43–128 unreserved characters (``token_urlsafe`` alphabet qualifies)."""
    return secrets.token_urlsafe(64)[:96]


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_auth_url(client: ClientConfig, *, redirect_uri: str, state: str, verifier: str) -> str:
    query = {"client_id": client.client_id, "redirect_uri": redirect_uri, "response_type": "code",
             "scope": SCOPE, "state": state, "code_challenge": challenge_for(verifier),
             "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"}
    return f"{client.auth_uri}?{urllib.parse.urlencode(query)}"


def parse_redirect(text: str, expected_state: str) -> str:
    """The authorization code from a pasted redirect URL (state verified) or a bare pasted code."""
    text = (text or "").strip()
    if not text:
        raise GoogleAuthError("nothing pasted")
    if "://" not in text and "?" not in text and "code=" not in text:
        return text  # a bare code (the user copied only the value)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(text).query or text.lstrip("?"))
    if "error" in query:
        raise GoogleAuthError(f"authorization denied: {query['error'][0]}")
    if query.get("state", [""])[0] != expected_state:
        raise GoogleAuthError("state mismatch (stale or forged redirect); start `google connect` again")
    code = query.get("code", [""])[0]
    if not code:
        raise GoogleAuthError("no authorization code in the pasted URL")
    return code


# -- token endpoint -----------------------------------------------------------------------------------
def _post_form(transport: Transport, url: str, form: Mapping[str, str]) -> Response:
    return transport.request("POST", url, headers={"Content-Type": "application/x-www-form-urlencoded"},
                             body=urllib.parse.urlencode(dict(form)).encode("ascii"), timeout=30.0)


def _post_token(transport: Transport, url: str, form: Mapping[str, str]) -> Response:
    """POST to the token endpoint; a network failure is a (retryable) :class:`GoogleAuthError`."""
    try:
        return _post_form(transport, url, form)
    except TransportError as exc:
        raise GoogleAuthError(f"Google token endpoint unreachable ({exc}); will retry") from None


def _token_error(resp: Response) -> str:
    data = resp.json()
    return str(data.get("error") or f"HTTP {resp.status}") if isinstance(data, dict) else f"HTTP {resp.status}"


def _token_from(data: Mapping[str, Any], now: float, previous: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    if not isinstance(data, Mapping) or not data.get("access_token"):
        raise GoogleAuthError("token endpoint returned no access token")
    token = dict(previous or {})
    token.update({"access_token": data["access_token"], "token_type": data.get("token_type", "Bearer"),
                  "expires_at": now + float(data.get("expires_in") or 3600), "scope": data.get("scope", SCOPE)})
    if data.get("refresh_token"):
        token["refresh_token"] = data["refresh_token"]
    return token


def exchange_code(transport: Transport, client: ClientConfig, *, code: str, verifier: str, redirect_uri: str,
                  now: Optional[float] = None) -> dict[str, Any]:
    resp = _post_token(transport, client.token_uri, {
        "code": code, "client_id": client.client_id, "client_secret": client.client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier})
    if not resp.ok:
        raise GoogleAuthError(f"token exchange failed: {_token_error(resp)}")
    data = resp.json()
    if not data.get("access_token") or not data.get("refresh_token"):
        raise GoogleAuthError("token exchange returned no refresh token; remove the app's access at "
                              "https://myaccount.google.com/permissions and connect again")
    return _token_from(data, time.time() if now is None else now)


def refresh_token(transport: Transport, client: ClientConfig, token: Mapping[str, Any], *,
                  now: Optional[float] = None) -> dict[str, Any]:
    if not token.get("refresh_token"):
        raise GoogleDisconnected("no refresh token; run `hermes meeting-scribe google connect`")
    resp = _post_token(transport, client.token_uri, {
        "client_id": client.client_id, "client_secret": client.client_secret,
        "refresh_token": str(token["refresh_token"]), "grant_type": "refresh_token"})
    if not resp.ok:
        err = _token_error(resp)
        if err in ("invalid_grant", "unauthorized_client", "invalid_client"):
            raise GoogleDisconnected(f"Google access was revoked or expired ({err}); "
                                     "run `hermes meeting-scribe google connect` again")
        raise GoogleAuthError(f"token refresh failed: {err}")
    return _token_from(resp.json(), time.time() if now is None else now, token)


def revoke(transport: Transport, token: Mapping[str, Any]) -> bool:
    """Best effort: revoke the refresh token (revokes the whole grant). ``False`` on any failure."""
    value = token.get("refresh_token") or token.get("access_token")
    if not value:
        return False
    try:
        return _post_form(transport, REVOKE_URI, {"token": str(value)}).ok
    except (TransportError, ValueError):
        return False


# -- loopback receiver ---------------------------------------------------------------------------------
_DONE_HTML = (b"<!doctype html><meta charset=utf-8><title>meeting-scribe</title>"
              b"<p>meeting-scribe: Google authorization received. You can close this tab.</p>")


class LoopbackReceiver:
    """One-shot ``http.server`` on ``127.0.0.1:<free port>`` that captures the OAuth redirect."""

    def __init__(self, expected_state: str, *, port: int = 0) -> None:
        self.expected_state = expected_state
        self.result: Optional[str] = None
        self.error: Optional[str] = None
        self._got = threading.Event()
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                if not query.get("code") and not query.get("error"):
                    self.send_response(404)
                    self.end_headers()
                    return  # favicon or a stray request: keep waiting
                if query.get("error"):
                    receiver.error = f"authorization denied: {query['error'][0]}"
                elif query.get("state", [""])[0] != receiver.expected_state:
                    receiver.error = "state mismatch"
                else:
                    receiver.result = query["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_DONE_HTML)
                receiver._got.set()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the query holds the code: never log
                return

        self._server = http.server.HTTPServer(("127.0.0.1", port), Handler)
        self._server.timeout = 0.5

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def wait(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        try:
            while not self._got.is_set() and time.monotonic() < deadline:
                self._server.handle_request()
        finally:
            self._server.server_close()
        if self.error:
            raise GoogleAuthError(self.error)
        if self.result is None:
            raise GoogleAuthError("timed out waiting for the Google redirect")
        return self.result

    def close(self) -> None:
        self._server.server_close()


# -- credentials used by the API client --------------------------------------------------------------
class GoogleCredentials:
    """Access tokens for the API client: refreshed automatically, persisted 0600, thread-safe."""

    def __init__(self, files: GoogleFiles, *, transport: Optional[Transport] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.files = files
        self.transport = transport or UrllibTransport()
        self.clock = clock
        self._lock = threading.Lock()

    def connected(self) -> bool:
        token = self.files.read_token()
        return bool(token and token.get("refresh_token") and not token.get("disconnected")) and \
            self.files.client_path.exists()

    def token(self) -> dict[str, Any]:
        token = self.files.read_token()
        if not token or not token.get("refresh_token"):
            raise GoogleDisconnected("Google is not connected; run `hermes meeting-scribe google connect`")
        if token.get("disconnected"):
            raise GoogleDisconnected("Google access was revoked or expired; run `hermes meeting-scribe google connect`")
        return token

    def access_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            token = self.token()
            fresh = token.get("access_token") and float(token.get("expires_at") or 0) - REFRESH_MARGIN > self.clock()
            if fresh and not force_refresh:
                return str(token["access_token"])
            try:
                new = refresh_token(self.transport, load_client(self.files), token, now=self.clock())
            except GoogleDisconnected:
                self.files.write_token({**token, "access_token": None, "disconnected": True})
                raise
            self.files.write_token(new)
            return str(new["access_token"])


def connect_flow(files: GoogleFiles, client: ClientConfig, *, transport: Transport, no_browser: bool,
                 emit: Callable[[str], None], read_line: Callable[[str], str],
                 open_browser: Optional[Callable[[str], bool]] = None, timeout: float = 300.0,
                 now: Callable[[], float] = time.time) -> dict[str, Any]:
    """Run the consent flow and store the token; returns the stored token (never printed)."""
    state = secrets.token_urlsafe(24)
    verifier = make_verifier()
    receiver: Optional[LoopbackReceiver] = None
    if no_browser:
        redirect_uri = f"http://127.0.0.1:{_free_port()}"
    else:
        receiver = LoopbackReceiver(state)
        redirect_uri = receiver.redirect_uri
    url = build_auth_url(client, redirect_uri=redirect_uri, state=state, verifier=verifier)
    emit(url)
    opened = False
    if receiver is not None and open_browser is not None:
        try:
            opened = bool(open_browser(url))
        except Exception:  # no display / no browser: fall back to pasting
            opened = False
    if receiver is not None and opened:
        code = receiver.wait(timeout)
    else:
        if receiver is not None:
            receiver.close()
        code = parse_redirect(read_line("paste"), state)
    token = exchange_code(transport, client, code=code, verifier=verifier, redirect_uri=redirect_uri, now=now())
    # Re-connecting (revoked/expired access, a new client) keeps the ORIGINAL connection time: the
    # poll window starts there (clamped to Meet's 30-day retention), so meetings that ended while
    # access was broken are still imported. Only `google disconnect` (which deletes the token)
    # starts a fresh window.
    previous = files.read_token() or {}
    first = previous.get("connected_at")
    if isinstance(first, (int, float)) and first > 0:
        token["connected_at"] = float(first)
        token["reconnected_at"] = now()
    else:
        token["connected_at"] = now()
    files.write_token(token)
    return token


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
