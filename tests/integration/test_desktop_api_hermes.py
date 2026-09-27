"""The Desktop REST API mounted by the REAL Hermes web server from a throwaway HERMES_HOME.

Proves: Hermes discovers ``dashboard/manifest.json``, imports ``plugin_api.py`` for an enabled user
plugin and mounts it under ``/api/plugins/meeting-scribe``; the host's auth rejects a request without
the session token; data, settings and the model chain resolve through the request's HERMES_HOME;
settings writes land in that profile's config.yaml through Hermes' own writer; no secret leaks.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("hermes_cli.plugins", reason="Hermes is not importable (set PYTHONPATH)")
yaml = pytest.importorskip("yaml")
pytest.importorskip("fastapi")

REPO = Path(__file__).resolve().parents[2]
IGNORE = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "*.pyc", "tests")
PREFIX = "/api/plugins/meeting-scribe"


@pytest.fixture
def served(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(REPO, home / "plugins" / "meeting-scribe", ignore=IGNORE)
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["meeting-scribe"],
                    "entries": {"meeting-scribe": {"settings": {"transcribe_language": "es"}}}},
        "auxiliary": {"meeting_scribe": {"provider": "openrouter", "model": "some/model",
                                         "base_url": "https://user:pw@llm.example/v1"}}}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)

    from fastapi.testclient import TestClient
    from hermes_cli import web_server, web_server_dashboard

    plugins = web_server._get_dashboard_plugins(force_rescan=True)
    assert any(p["name"] == "meeting-scribe" and p["has_api"] for p in plugins)
    before = len(web_server.app.router.routes)
    web_server_dashboard._mount_plugin_api_routes()
    mounted = [r for r in web_server.app.router.routes[before:] if getattr(r, "path", "").startswith(PREFIX)]
    assert mounted, "meeting-scribe API was not mounted"
    # At startup Hermes mounts plugin routes before its SPA catch-all; a re-mount in a test lands
    # after it, so put ours first (route order is match order).
    routes = web_server.app.router.routes
    routes[:] = mounted + [r for r in routes if r not in mounted]
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield {"client": client, "home": home, "ws": web_server}
    finally:
        web_server.app.router.routes[:] = [r for r in web_server.app.router.routes
                                           if not getattr(r, "path", "").startswith(PREFIX)]
        web_server._get_dashboard_plugins(force_rescan=True)


def _seed(home: Path) -> str:
    from datetime import datetime, timezone

    from meeting_scribe.domain.models import Meeting, MeetingState, Utterance
    from meeting_scribe.storage.layout import Layout
    from meeting_scribe.storage.repo import Repository

    root = home / "plugin-data" / "meeting-scribe"
    root.mkdir(parents=True, exist_ok=True)
    layout = Layout(lambda: root)
    m = Meeting(id="int0001a", guild_id="100", channel_id="200", channel_name="Daily Sync",
                started_at=datetime(2026, 9, 26, 15, 4, tzinfo=timezone.utc), state=MeetingState.DONE,
                title="Daily Sync")
    folder = layout.meeting_folder(m)
    folder.mkdir(parents=True)
    repo = Repository(root / "index.sqlite")
    repo.save_meeting(replace(m, folder=layout.relative(folder), state=MeetingState.DONE))
    repo.replace_utterances(m.id, [Utterance(0.0, 2.0, "10", "Ana", "Hola equipo")])
    repo.close()
    (folder / "recording.ogg").write_bytes(b"OggS" + b"\0" * 64)
    return m.id


def test_mounted_api_is_authenticated_and_profile_scoped(served):
    c, home = served["client"], served["home"]
    mid = _seed(home)
    from fastapi.testclient import TestClient

    anon = TestClient(served["ws"].app)
    assert anon.get(f"{PREFIX}/v1/meetings").status_code == 401  # the host's auth, not ours
    page = c.get(f"{PREFIX}/v1/meetings").json()
    assert [m["id"] for m in page["items"]] == [mid]
    detail = c.get(f"{PREFIX}/v1/meetings/{mid}").json()
    assert detail["audio"]["available"] is True
    assert detail["audio"]["path"].startswith(str(home / "plugin-data" / "meeting-scribe"))
    part = c.get(f"{PREFIX}/v1/meetings/{mid}/audio", headers={"Range": "bytes=0-3"})
    assert part.status_code == 206 and part.content == b"OggS"


def test_settings_and_llm_round_trip_through_hermes_config(served):
    c, home = served["client"], served["home"]
    r0 = c.get(f"{PREFIX}/v1/settings", params={"lang": "es"})
    assert r0.status_code == 200, r0.text
    s = r0.json()
    assert s["values"]["transcribe_language"] == {"value": "es", "origin": "configured"}
    assert s["llm"]["provider"] == "openrouter" and "pw" not in json.dumps(s["llm"])
    assert c.put(f"{PREFIX}/v1/settings/pipeline_max_attempts", json={"value": 6}).status_code == 200
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["plugins"]["entries"]["meeting-scribe"]["settings"]["pipeline_max_attempts"] == 6
    shown = s["llm"]["base_url"]
    r = c.put(f"{PREFIX}/v1/llm", json={"model": "other/model", "base_url": shown,
                                        "fallback_chain": [{"provider": "nous", "model": "m2"}]})
    assert r.status_code == 200, r.text
    aux = yaml.safe_load((home / "config.yaml").read_text())["auxiliary"]["meeting_scribe"]
    assert aux["model"] == "other/model" and aux["base_url"] == "https://user:pw@llm.example/v1"
    assert aux["fallback_chain"] == [{"provider": "nous", "model": "m2"}]


def test_invalid_llm_edit_leaves_config_byte_identical(served):
    c, home = served["client"], served["home"]
    path = home / "config.yaml"
    before = path.read_bytes()
    r = c.put(f"{PREFIX}/v1/llm", json={"model": "new", "fallback_chain": [{"provider": "openai"}]})
    assert r.status_code == 400
    assert path.read_bytes() == before


def test_llm_atomic_write_preserves_unknown_secrets_and_managed_keys(served, monkeypatch):
    from hermes_cli import config as hc, managed_scope
    c, home = served["client"], served["home"]
    path = home / "config.yaml"
    cfg = yaml.safe_load(path.read_text())
    task = cfg["auxiliary"]["meeting_scribe"]
    task.update({"api_key": "opaque-secret", "transport": "custom",
                 "fallback_chain": [{"provider": "nous", "model": "backup", "api_key": "fallback-secret"}]})
    path.write_text(yaml.safe_dump(cfg))
    before = path.read_bytes()
    monkeypatch.setattr(managed_scope, "is_key_managed", lambda key: key.endswith("fallback_chain"))
    body = {"model": "new", "fallback_chain": [{"provider": "nous", "model": "backup"}]}
    assert c.put(f"{PREFIX}/v1/llm", json=body).status_code == 403
    assert path.read_bytes() == before
    monkeypatch.setattr(managed_scope, "is_key_managed", lambda key: False)
    original = hc.save_config
    writes = []
    def save(*args, **kwargs):
        writes.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(hc, "save_config", save)
    assert c.put(f"{PREFIX}/v1/llm", json=body).status_code == 200
    assert len(writes) == 1
    task = yaml.safe_load(path.read_text())["auxiliary"]["meeting_scribe"]
    assert task["api_key"] == "opaque-secret" and task["transport"] == "custom"
    assert task["fallback_chain"][0]["api_key"] == "fallback-secret"


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/v1/settings", None), ("HEAD", "/v1/settings", None),
    ("POST", "/v1/meetings/int0001a/commands", {"request_id": "anon", "action": "reprocess", "stage": "deliver", "confirm": True}),
    ("PUT", "/v1/llm", {"model": "unauthorized"}),
])
def test_host_auth_all_methods_without_token(served, method, path, body):
    from fastapi.testclient import TestClient
    before = (served["home"] / "config.yaml").read_bytes()
    anon = TestClient(served["ws"].app)
    assert anon.request(method, PREFIX + path, json=body).status_code == 401
    assert (served["home"] / "config.yaml").read_bytes() == before


def test_status_doctor_and_reprocess_queue(served):
    c, home = served["client"], served["home"]
    mid = _seed(home)
    r0 = c.get(f"{PREFIX}/v1/status")
    assert r0.status_code == 200, r0.text
    st = r0.json()
    assert st["worker"]["state"] == "unknown" and st["google"]["connected"] is False
    doc = c.get(f"{PREFIX}/v1/doctor")
    assert doc.status_code == 200 and {"storage", "settings"} <= {x["name"] for x in doc.json()["checks"]}
    q = c.post(f"{PREFIX}/v1/meetings/{mid}/commands",
               json={"request_id": "int-1", "action": "reprocess", "stage": "deliver", "confirm": True})
    assert q.status_code == 200 and q.json()["state"] == "queued"
    assert c.get(f"{PREFIX}/v1/commands/int-1").json()["state"] == "queued"  # waits for the gateway


def test_profile_query_uses_hermes_scope(served):
    c = served["client"]
    assert c.get(f"{PREFIX}/v1/meetings", params={"profile": "current"}).status_code == 200
    assert c.get(f"{PREFIX}/v1/meetings", params={"profile": "no-such-profile"}).status_code == 404
    assert c.get(f"{PREFIX}/v1/meetings", params={"profile": "../etc"}).status_code == 400
