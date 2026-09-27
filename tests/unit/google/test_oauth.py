"""Google OAuth (DESIGN §17): PKCE/state URL, exchange, refresh, invalid_grant, 0600 files, loopback."""
from __future__ import annotations

import base64
import hashlib
import json
import stat
import threading
import urllib.parse
import urllib.request

import pytest

from meeting_scribe.google import oauth
from meeting_scribe.google.oauth import (
    SCOPE, GoogleAuthError, GoogleCredentials, GoogleDisconnected, GoogleFiles, LoopbackReceiver,
)

from .fakes import CLIENT_JSON, FakeTransport, jresp


@pytest.fixture
def files(tmp_path):
    return GoogleFiles(lambda: tmp_path / "data")


@pytest.fixture
def client_file(tmp_path):
    p = tmp_path / "client_secret_desktop.json"
    p.write_text(json.dumps(CLIENT_JSON))
    return p


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_import_client_copies_with_0600_and_validates(files, client_file, tmp_path):
    client = oauth.import_client_file(files, client_file)
    assert client.client_id == "fake-client.apps.example"
    assert "fake-shh" not in repr(client)
    assert files.client_path.exists() and mode(files.client_path) == 0o600 and mode(files.dir) == 0o700
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}))
    with pytest.raises(GoogleAuthError, match="Desktop app"):
        oauth.import_client_file(files, web)
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(GoogleAuthError, match="not valid JSON"):
        oauth.import_client_file(files, bad)


def test_auth_url_has_pkce_state_single_scope_and_offline(files, client_file):
    client = oauth.import_client_file(files, client_file)
    verifier = oauth.make_verifier()
    assert 43 <= len(verifier) <= 128
    url = oauth.build_auth_url(client, redirect_uri="http://127.0.0.1:5555", state="st4te", verifier=verifier)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["scope"] == SCOPE == "https://www.googleapis.com/auth/meetings.space.readonly"
    assert q["state"] == "st4te" and q["code_challenge_method"] == "S256"
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == expected
    assert q["access_type"] == "offline" and q["response_type"] == "code"
    assert q["redirect_uri"] == "http://127.0.0.1:5555"
    assert "client_secret" not in q and "fake-shh" not in url


def test_parse_pasted_redirect():
    assert oauth.parse_redirect("http://127.0.0.1:1234/?state=s1&code=4/abc&scope=x", "s1") == "4/abc"
    assert oauth.parse_redirect("  4/bare-code  ", "s1") == "4/bare-code"
    with pytest.raises(GoogleAuthError, match="state mismatch"):
        oauth.parse_redirect("http://127.0.0.1:1234/?state=evil&code=4/abc", "s1")
    with pytest.raises(GoogleAuthError, match="denied"):
        oauth.parse_redirect("http://127.0.0.1:1234/?error=access_denied&state=s1", "s1")
    with pytest.raises(GoogleAuthError):
        oauth.parse_redirect("", "s1")


def token_server(refresh_error=None):
    def handler(method, url, headers, body):
        form = dict(urllib.parse.parse_qsl(body.decode()))
        assert url == "https://oauth2.googleapis.com/token" and method == "POST"
        if form["grant_type"] == "authorization_code":
            assert form["code_verifier"] and form["code"] == "4/good"
            return jresp(200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3599,
                               "scope": SCOPE, "token_type": "Bearer"})
        if refresh_error:
            return jresp(400, {"error": refresh_error, "error_description": "Token has been expired or revoked."})
        assert form["refresh_token"] == "rt-1"
        return jresp(200, {"access_token": "at-2", "expires_in": 3599})
    return FakeTransport(handler)


def test_exchange_then_refresh_keeps_refresh_token(files, client_file):
    client = oauth.import_client_file(files, client_file)
    tr = token_server()
    tok = oauth.exchange_code(tr, client, code="4/good", verifier="v" * 50, redirect_uri="http://127.0.0.1:9", now=1000)
    assert tok["refresh_token"] == "rt-1" and tok["expires_at"] == 1000 + 3599
    assert tr.calls[0]["form"]["redirect_uri"] == "http://127.0.0.1:9"
    new = oauth.refresh_token(tr, client, tok, now=5000)
    assert new["access_token"] == "at-2" and new["refresh_token"] == "rt-1" and new["expires_at"] == 5000 + 3599


def test_credentials_refresh_automatically_and_persist_0600(files, client_file):
    oauth.import_client_file(files, client_file)
    files.write_token({"access_token": "old", "refresh_token": "rt-1", "expires_at": 1000})
    now = [2000.0]
    creds = GoogleCredentials(files, transport=token_server(), clock=lambda: now[0])
    assert creds.connected()
    assert creds.access_token() == "at-2"  # expired -> refreshed
    assert files.read_token()["access_token"] == "at-2" and mode(files.token_path) == 0o600
    now[0] = 2100
    assert creds.access_token() == "at-2"  # still fresh: no new call
    assert len(creds.transport.calls) == 1


def test_invalid_grant_means_disconnected(files, client_file):
    oauth.import_client_file(files, client_file)
    files.write_token({"access_token": "old", "refresh_token": "rt-1", "expires_at": 0})
    creds = GoogleCredentials(files, transport=token_server(refresh_error="invalid_grant"), clock=lambda: 10.0)
    with pytest.raises(GoogleDisconnected, match="connect"):
        creds.access_token()
    assert not creds.connected()
    with pytest.raises(GoogleDisconnected):  # no hammering the token endpoint afterwards
        creds.access_token()
    assert len(creds.transport.calls) == 1


def test_errors_never_contain_secrets(files, client_file):
    oauth.import_client_file(files, client_file)
    files.write_token({"access_token": "old", "refresh_token": "rt-secret-value", "expires_at": 0})
    creds = GoogleCredentials(files, transport=token_server(refresh_error="server_error"), clock=lambda: 10.0)
    with pytest.raises(GoogleAuthError) as info:
        creds.access_token()
    assert "rt-secret-value" not in str(info.value) and "fake-shh" not in str(info.value)


def test_not_connected(files):
    creds = GoogleCredentials(files, transport=token_server())
    assert not creds.connected()
    with pytest.raises(GoogleDisconnected):
        creds.access_token()


def test_revoke_is_best_effort():
    ok = FakeTransport(lambda *a: jresp(200))
    assert oauth.revoke(ok, {"refresh_token": "rt-1"})
    assert ok.calls[0]["url"] == "https://oauth2.googleapis.com/revoke" and ok.calls[0]["form"] == {"token": "rt-1"}
    assert not oauth.revoke(FakeTransport(lambda *a: jresp(400)), {"refresh_token": "rt-1"})
    assert not oauth.revoke(ok, {})


def test_loopback_receiver_captures_code_and_checks_state():
    rx = LoopbackReceiver("s1")
    uri = rx.redirect_uri
    assert uri.startswith("http://127.0.0.1:")

    def hit():
        with urllib.request.urlopen(f"{uri}/?state=s1&code=4/good", timeout=5) as r:
            assert b"close this tab" in r.read()
    t = threading.Thread(target=hit)
    t.start()
    assert rx.wait(10) == "4/good"
    t.join()


def test_loopback_receiver_rejects_wrong_state_and_times_out():
    rx = LoopbackReceiver("s1")
    t = threading.Thread(target=lambda: urllib.request.urlopen(f"{rx.redirect_uri}/?state=bad&code=x", timeout=5).close())
    t.start()
    with pytest.raises(GoogleAuthError, match="state mismatch"):
        rx.wait(10)
    t.join()
    with pytest.raises(GoogleAuthError, match="timed out"):
        LoopbackReceiver("s1").wait(0.6)


def test_connect_flow_no_browser_paste(files, client_file):
    client = oauth.import_client_file(files, client_file)
    shown: list[str] = []

    def read_line(_prompt):
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(shown[0]).query))["state"]
        return f"http://127.0.0.1:1/?state={state}&code=4/good"
    tok = oauth.connect_flow(files, client, transport=token_server(), no_browser=True, emit=shown.append,
                             read_line=read_line, now=lambda: 777.0)
    assert tok["connected_at"] == 777.0 and files.read_token()["refresh_token"] == "rt-1"
    assert mode(files.token_path) == 0o600


def test_connect_flow_browser_uses_loopback(files, client_file):
    client = oauth.import_client_file(files, client_file)

    def open_browser(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        threading.Thread(target=lambda: urllib.request.urlopen(
            f"{q['redirect_uri']}/?state={q['state']}&code=4/good", timeout=5).close()).start()
        return True
    tok = oauth.connect_flow(files, client, transport=token_server(), no_browser=False, emit=lambda u: None,
                             read_line=lambda p: pytest.fail("should not ask"), open_browser=open_browser,
                             timeout=10)
    assert tok["refresh_token"] == "rt-1"
