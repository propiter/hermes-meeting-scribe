"""``hermes meeting-scribe google …`` and the doctor check (DESIGN §17). Fake HTTP only."""
from __future__ import annotations

import json
import stat
import urllib.parse
from datetime import datetime, timezone

import pytest

from meeting_scribe import cli, cli_google
from meeting_scribe.doctor import check_google_meet
from meeting_scribe.google.importer import MeetImporter
from meeting_scribe.google.meet_api import MeetClient
from meeting_scribe.google.oauth import GoogleCredentials, GoogleFiles
from tests.unit.gmeet.fake_meet import FakeMeet
from tests.unit.gmeet.fakes import CLIENT_JSON, FakeTransport, jresp

from .test_cli import FakeRuntime, parse
from .test_commands import make

NOW = datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc)


class GoogleRuntime(FakeRuntime):
    def __init__(self, service, cfg, data_dir):
        super().__init__(service, cfg)
        self.meet = FakeMeet()
        self.revoked: list = []

        def handler(method, url, headers, body):
            if url.startswith("https://oauth2.googleapis.com/token"):
                form = dict(urllib.parse.parse_qsl(body.decode()))
                if form["grant_type"] == "authorization_code":
                    return jresp(200, {"access_token": "at", "refresh_token": "rt-secret", "expires_in": 3600})
                return jresp(200, {"access_token": "at2", "expires_in": 3600})
            if url.startswith("https://oauth2.googleapis.com/revoke"):
                self.revoked.append(dict(urllib.parse.parse_qsl(body.decode())))
                return jresp(200)
            return self.meet(method, url, headers, body)
        self.google_transport = FakeTransport(handler)
        self._files = GoogleFiles(lambda: data_dir)

    def google_files(self):
        return self._files

    def google_credentials(self):
        return GoogleCredentials(self._files, transport=self.google_transport)

    def google_connected_at(self):
        v = (self._files.read_token() or {}).get("connected_at")
        return float(v) if v else None

    def meet_importer(self):
        return MeetImporter(service=self.service, client=lambda: MeetClient(self.google_credentials()),
                            clock=lambda: NOW)


@pytest.fixture
def grt(prepo, layout, settings, clock, tmp_path):
    _, service, _runner = make(prepo, layout, settings, clock)
    return GoogleRuntime(service, {}, tmp_path / "data")


@pytest.fixture
def client_file(tmp_path):
    p = tmp_path / "client_secret.json"
    p.write_text(json.dumps(CLIENT_JSON))
    return p


def run(rt, argv, capsys):
    code = cli.dispatch(parse(argv), rt)
    return code, capsys.readouterr().out


def connect(grt, client_file, capsys, monkeypatch):
    shown = {}

    def fake_input(prompt=""):
        return f"http://127.0.0.1:1/?state={shown['state']}&code=4/abc"
    real_emit_print = cli_google._print

    def capture(text):
        if text.startswith("https://accounts.google.com/"):
            shown["state"] = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(text).query))["state"]
        real_emit_print(text)
    monkeypatch.setattr(cli_google, "_print", capture)
    monkeypatch.setattr(cli_google, "_READ_LINE", fake_input)
    return run(grt, ["google", "connect", "--client-secret", str(client_file), "--no-browser"], capsys)


def test_connect_no_browser_stores_0600_and_prints_no_secret(grt, client_file, capsys, monkeypatch):
    code, out = connect(grt, client_file, capsys, monkeypatch)
    assert code == 0, out
    assert "Google connected" in out and "google_meet_enabled true" in out
    assert "fake-shh" not in out and "rt-secret" not in out and "4/abc" not in out
    tok = grt.google_files().token_path
    assert stat.S_IMODE(tok.stat().st_mode) == 0o600
    assert json.loads(tok.read_text())["connected_at"] > 0


def test_connect_without_client_is_input_error(grt, capsys):
    code, out = run(grt, ["google", "connect", "--no-browser"], capsys)
    assert code == 2 and "Desktop app" in out


def test_status_sync_disconnect(grt, client_file, capsys, monkeypatch):
    code, out = run(grt, ["google", "status"], capsys)
    assert code == 0 and "not connected" in out
    code, out = run(grt, ["google", "sync"], capsys)
    assert code == 1
    connect(grt, client_file, capsys, monkeypatch)
    monkeypatch.setattr(cli_google, "_print", lambda text: print(text))
    grt.meet.add("r1")
    code, out = run(grt, ["google", "sync", "--days", "3", "--dry-run"], capsys)
    assert code == 0 and "conferenceRecords/r1" in out
    assert grt.service().repo.list_meetings(limit=5) == []
    code, out = run(grt, ["google", "sync", "--days", "3", "--json"], capsys)
    data = json.loads(out)
    assert code == 0 and len(data["imported"]) == 1
    code, out = run(grt, ["google", "status", "--json"], capsys)
    st = json.loads(out)
    assert st["connected"] and st["last_poll_ok"] == "1" and st["last_import_meeting"] == data["imported"][0]
    code, out = run(grt, ["google", "disconnect"], capsys)
    assert code == 0 and "revoked" in out and grt.revoked == [{"token": "rt-secret"}]
    assert not grt.google_files().token_path.exists()


def test_sync_rejects_bad_since(grt, client_file, capsys, monkeypatch):
    connect(grt, client_file, capsys, monkeypatch)
    code, out = run(grt, ["google", "sync", "--since", "yesterday"], capsys)
    assert code == 2


def test_setup_flags_enable_google(grt, capsys):
    code, _ = run(grt, ["setup", "--non-interactive", "--google-meet", "--google-meet-channel", "4242"], capsys)
    assert code == 0 and grt.cfg["google_meet_enabled"] is True and grt.cfg["google_meet_discord_channel"] == "4242"


def test_doctor_check(grt, client_file, capsys, monkeypatch):
    assert check_google_meet(grt).status == "ok"  # disabled
    grt.cfg["google_meet_enabled"] = True
    assert check_google_meet(grt).status == "fail"  # no client
    connect(grt, client_file, capsys, monkeypatch)
    res = check_google_meet(grt)
    # finding 16: no notes channel -> warn (Meet notes would land in the home channel)
    assert res.status == "warn" and "token OK" in res.detail and "google_meet_discord_channel" in res.detail
    grt.cfg["google_meet_discord_channel"] = "4242"
    res = check_google_meet(grt)
    assert res.status == "ok" and "4242" in res.detail
    grt.cfg["google_meet_discord_channel"] = ""
    grt.cfg["delivery_discord_channel"] = "77"
    assert check_google_meet(grt).status == "ok"


def test_sync_never_prints_a_traceback(grt, client_file, capsys, monkeypatch):
    connect(grt, client_file, capsys, monkeypatch)
    monkeypatch.setattr(cli_google, "_print", lambda text: print(text))

    def broken():
        raise OSError("disk full")
    monkeypatch.setattr(grt, "meet_importer", broken)
    code, out = run(grt, ["google", "sync"], capsys)
    assert code == 1 and "OSError: disk full" in out and "Traceback" not in out


def test_doctor_reports_given_up_records(grt, client_file, capsys, monkeypatch):
    from meeting_scribe.google.importer import MAX_RECORD_FAILURES
    grt.cfg["google_meet_enabled"] = True
    grt.cfg["google_meet_discord_channel"] = "4242"
    connect(grt, client_file, capsys, monkeypatch)
    grt.meet.add("old")
    grt.meet.status["conferenceRecords/old/transcripts"] = 404
    imp = grt.meet_importer()
    for _ in range(MAX_RECORD_FAILURES + 1):
        imp.sync(ended_after=imp.window_start(days=3))
    res = check_google_meet(grt)
    assert res.status == "warn" and "skipped after repeated errors" in res.detail


def test_reconnect_says_the_window_is_kept(grt, client_file, capsys, monkeypatch):
    connect(grt, client_file, capsys, monkeypatch)
    first = json.loads(grt.google_files().token_path.read_text())["connected_at"]
    code, out = connect(grt, client_file, capsys, monkeypatch)
    assert code == 0 and "reconnected" in out
    assert json.loads(grt.google_files().token_path.read_text())["connected_at"] == first


# -- finding 16: enabling Meet without a notes channel is flagged ------------------------------------
def test_setup_enabling_google_without_channel_warns(grt, capsys):
    code, out = run(grt, ["setup", "--non-interactive", "--google-meet"], capsys)
    assert code == 0 and grt.cfg["google_meet_enabled"] is True
    assert "google_meet_discord_channel" in out and "home" in out


def test_setup_interactive_asks_for_the_meet_channel_again_when_left_empty(grt, capsys, monkeypatch):
    answers = iter([""] * 10 + ["y", "", "5555"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers, ""))
    code, out = run(grt, ["setup"], capsys)
    assert code == 0 and grt.cfg["google_meet_discord_channel"] == "5555"


def test_config_set_enable_without_channel_warns(grt, capsys):
    code, out = run(grt, ["config", "set", "google_meet_enabled", "true"], capsys)
    assert code == 0 and "google_meet_discord_channel" in out
    grt.cfg["delivery_discord_channel"] = "77"
    code, out = run(grt, ["config", "set", "google_meet_enabled", "true"], capsys)
    assert code == 0 and "google_meet_discord_channel" not in out


# -- finding 18: channel ids are validated instead of silently ignored -------------------------------
@pytest.mark.parametrize("key", ["google_meet_discord_channel", "delivery_discord_channel"])
def test_config_set_rejects_non_numeric_channel(grt, capsys, key):
    code, out = run(grt, ["config", "set", key, "general"], capsys)
    assert code == 2 and key in out and key not in grt.cfg


def test_config_set_accepts_a_channel_mention_and_stores_the_id(grt, capsys):
    code, _ = run(grt, ["config", "set", "google_meet_discord_channel", "<#123456789012345678>"], capsys)
    assert code == 0 and grt.cfg["google_meet_discord_channel"] == "123456789012345678"


def test_doctor_warns_about_a_non_numeric_channel_in_config():
    from meeting_scribe.config import settings_from_mapping
    s = settings_from_mapping({"google_meet_discord_channel": "meet-notes"})
    assert s.google_meet_discord_channel == ""
    assert any("google_meet_discord_channel" in w for w in s.warnings)
