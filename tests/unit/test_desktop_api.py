"""The Desktop REST API through FastAPI's TestClient, with isolated data dir / settings / LLM store."""
import contextlib
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from meeting_scribe.desktop import api  # noqa: E402
from meeting_scribe.domain.models import MeetingState  # noqa: E402
from meeting_scribe.storage.artifacts import write_notes  # noqa: E402
from meeting_scribe.storage.layout import Layout  # noqa: E402
from meeting_scribe.storage.repo import Repository  # noqa: E402
from tests.unit.test_desktop_settings import MemSettings  # noqa: E402
from tests.unit.test_llm_config import MemStore  # noqa: E402

PREFIX = "/api/plugins/meeting-scribe"


@pytest.fixture
def env(tmp_path, meeting, notes, utterances):
    homes = {"": tmp_path / "default", "other": tmp_path / "other"}
    for h in homes.values():
        h.mkdir()
    current = {"home": homes[""]}
    scopes = []

    @contextlib.contextmanager
    def scope(profile):
        scopes.append(profile)
        if (profile or "") not in homes:
            from fastapi import HTTPException
            raise HTTPException(404, "no such profile")  # what Hermes' scope raises
        before = current["home"]
        current["home"] = homes[profile or ""]
        try:
            yield
        finally:
            current["home"] = before
    mem, aux = MemSettings({"transcribe_language": "es"}), MemStore({"provider": "a", "model": "m"})
    api.SEAMS.update({"data_dir": lambda: current["home"], "settings_store": mem.store,
                      "aux_store": lambda: aux, "profile_scope": scope, "secret": lambda n: None,
                      "kanban": lambda: type("K", (), {"list_boards": staticmethod(lambda: [])})()})
    root = homes[""]
    layout = Layout(lambda: root)
    folder = layout.meeting_folder(meeting)
    folder.mkdir(parents=True)
    m = replace(meeting, folder=layout.relative(folder), state=MeetingState.DONE)
    repo = Repository(root / "index.sqlite")
    repo.save_meeting(m)
    repo.replace_utterances(m.id, utterances)
    repo.sync_action_items(m.id, notes.action_items)
    write_notes(folder, m, notes, "es")
    repo.close()
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    yield {"client": TestClient(app), "meeting": m, "folder": folder, "mem": mem, "aux": aux,
           "scopes": scopes, "homes": homes}
    api.reset_seams()


def test_library_detail_and_transcript(env):
    c, mid = env["client"], env["meeting"].id
    page = c.get(f"{PREFIX}/v1/meetings", params={"q": "credenciales"}).json()
    assert [m["id"] for m in page["items"]] == [mid] and page["facets"]["total"] == 1
    assert c.get(f"{PREFIX}/v1/meetings", params={"state": "bogus"}).status_code == 400
    assert c.get(f"{PREFIX}/v1/meetings", params={"cursor": "!!"}).status_code == 400
    assert c.get(f"{PREFIX}/v1/meetings", params={"limit": 1000}).status_code == 422
    d = c.get(f"{PREFIX}/v1/meetings/{mid}").json()
    assert d["notes"]["summary"] and d["tasks"] and d["transcript_total"] == 3
    assert d["audio"] == {"available": False, "reason": "not_retained"}
    t = c.get(f"{PREFIX}/v1/meetings/{mid}/transcript", params={"limit": 2}).json()
    assert len(t["items"]) == 2 and t["next_cursor"]
    assert c.get(f"{PREFIX}/v1/meetings/missing").status_code == 404
    assert c.get(f"{PREFIX}/v1/meetings/..%2F..%2Fetc").status_code == 404


def test_profile_query_scopes_the_request_and_is_validated(env):
    c = env["client"]
    assert c.get(f"{PREFIX}/v1/meetings", params={"profile": "other"}).json()["items"] == []
    assert env["scopes"][-1] == "other"
    assert c.get(f"{PREFIX}/v1/meetings", params={"profile": "current"}).json()["facets"]["total"] == 1
    assert env["scopes"][-1] is None
    assert c.get(f"{PREFIX}/v1/meetings", params={"profile": "../x"}).status_code == 400
    assert c.get(f"{PREFIX}/v1/meetings", params={"profile": "ghost"}).status_code == 404


def test_audio_streams_with_range_and_refuses_symlinks(env, tmp_path):
    c, mid, folder = env["client"], env["meeting"].id, env["folder"]
    assert c.get(f"{PREFIX}/v1/meetings/{mid}/audio").status_code == 404
    payload = b"OggS" + bytes(range(256)) * 4
    (folder / "recording.ogg").write_bytes(payload)
    d = c.get(f"{PREFIX}/v1/meetings/{mid}").json()
    assert d["audio"]["available"] and d["audio"]["stream_path"] == f"/v1/meetings/{mid}/audio"
    full = c.get(f"{PREFIX}/v1/meetings/{mid}/audio")
    assert full.status_code == 200 and full.content == payload
    assert full.headers["content-type"].startswith("audio/ogg")
    assert full.headers["content-disposition"].startswith("inline")
    part = c.get(f"{PREFIX}/v1/meetings/{mid}/audio", headers={"Range": "bytes=4-9"})
    assert part.status_code == 206 and part.content == payload[4:10]
    assert c.head(f"{PREFIX}/v1/meetings/{mid}/audio").status_code == 200
    (folder / "recording.ogg").unlink()
    (tmp_path / "secret.ogg").write_bytes(b"secret")
    (folder / "recording.ogg").symlink_to(tmp_path / "secret.ogg")
    assert c.get(f"{PREFIX}/v1/meetings/{mid}/audio").status_code == 400


def test_audio_prefers_the_listening_copy_over_the_multitrack_archive(env):
    c, mid, folder = env["client"], env["meeting"].id, env["folder"]
    (folder / "recording.mka").write_bytes(b"\x1aE\xdf\xa3matroska")
    d = c.get(f"{PREFIX}/v1/meetings/{mid}").json()
    assert d["audio"]["reason"] == "multitrack" and d["audio"]["can_prepare"] and "stream_path" not in d["audio"]
    assert c.get(f"{PREFIX}/v1/meetings/{mid}/audio").status_code == 404  # never the .mka
    (folder / "playback.ogg").write_bytes(b"OggS-listening-copy")
    d = c.get(f"{PREFIX}/v1/meetings/{mid}").json()["audio"]
    assert d["available"] and d["original"] and d["path"].endswith("playback.ogg")
    got = c.get(f"{PREFIX}/v1/meetings/{mid}/audio")
    assert got.status_code == 200 and got.content == b"OggS-listening-copy"


def test_detail_says_which_task_destinations_are_on(env):
    d = env["client"].get(f"{PREFIX}/v1/meetings/{env['meeting'].id}").json()
    assert d["destinations"]["discord"] is True
    assert d["destinations"]["kanban"] in ("approve", "auto", "off") and d["destinations"]["linear"] in (
        "approve", "auto", "off")


def test_channel_and_project_filters_reach_the_library(env):
    c = env["client"]
    page = c.get(f"{PREFIX}/v1/meetings", params={"channel": "nope", "project": "Proyecto Alfa"}).json()
    assert page["items"] == [] and "channels" in page["facets"] and "projects" in page["facets"]


def test_reprocess_requires_confirmation_and_is_only_queued(env):
    c, mid = env["client"], env["meeting"].id
    url = f"{PREFIX}/v1/meetings/{mid}/commands"
    body = {"request_id": "req-1", "action": "reprocess", "stage": "analyze"}
    assert c.post(url, json=body).status_code == 400
    assert c.post(url, json={**body, "confirm": "yes"}).status_code == 400
    assert c.post(url, json={**body, "confirm": True, "request_id": "bad id"}).status_code == 400
    assert c.post(url, json={**body, "confirm": True, "action": "delete"}).status_code == 400
    ok = c.post(url, json={**body, "confirm": True})
    assert ok.status_code == 200 and ok.json()["state"] == "queued"
    assert c.post(url, json={**body, "confirm": True}).json()["id"] == "req-1"  # retry is inert
    assert c.get(f"{PREFIX}/v1/commands/req-1").json()["state"] == "queued"
    assert c.get(f"{PREFIX}/v1/commands/nope").status_code == 404
    repo = Repository(env["homes"][""] / "index.sqlite")
    assert repo.get_job(mid) is None  # the dashboard never ran or queued a stage itself
    repo.close()
    assert c.post(f"{PREFIX}/v1/meetings/missing/commands", json={**body, "confirm": True,
                                                                  "request_id": "r2"}).status_code == 404


def test_acknowledge_requires_confirmation_and_never_executes(env):
    c, mid = env["client"], env["meeting"].id
    c.post(f"{PREFIX}/v1/meetings/{mid}/commands", json={"request_id": "uncertain", "action": "reprocess", "stage": "deliver", "confirm": True})
    repo = Repository(env["homes"][""] / "index.sqlite")
    repo._x("UPDATE desktop_commands SET state='unknown' WHERE id='uncertain'")
    url = f"{PREFIX}/v1/commands/uncertain/acknowledge"
    assert c.post(url, json={}).status_code == 400
    assert c.post(url, json={"confirm": True}).json()["state"] == "acknowledged"
    assert repo.get_job(mid) is None
    repo.close()


def test_status_google_and_doctor_leak_no_secrets(env):
    c = env["client"]
    gdir = env["homes"][""] / "google"
    gdir.mkdir()
    (gdir / "client.json").write_text(json.dumps({"installed": {"client_id": "CID", "client_secret": "CSECRET"}}))
    (gdir / "token.json").write_text(json.dumps({"refresh_token": "RTOKEN", "access_token": "ATOKEN"}))
    st = c.get(f"{PREFIX}/v1/status").json()
    assert st["worker"]["state"] == "unknown" and st["google"]["connected"] is True
    doc = c.get(f"{PREFIX}/v1/doctor").json()
    assert {"storage", "google_meet"} <= {x["name"] for x in doc["checks"]}
    blob = json.dumps([st, doc, c.get(f"{PREFIX}/v1/settings").json()])
    for secret in ("RTOKEN", "ATOKEN", "CSECRET", "CID"):
        assert secret not in blob


def test_settings_read_write_and_llm(env):
    c = env["client"]
    s = c.get(f"{PREFIX}/v1/settings", params={"lang": "es-ES"}).json()
    assert s["schema"]["language"] == "es" and s["values"]["transcribe_language"]["value"] == "es"
    assert s["llm"]["provider"] == "a"
    ok = c.put(f"{PREFIX}/v1/settings/pipeline_max_attempts", json={"value": "4"})
    assert ok.status_code == 200 and ok.json()["value"] == 4
    assert env["mem"].writes[-1] == ("pipeline_max_attempts", 4)
    assert c.put(f"{PREFIX}/v1/settings/pipeline_max_attempts", json={"value": "0"}).status_code == 400
    assert c.put(f"{PREFIX}/v1/settings/nope", json={"value": "1"}).status_code == 400
    assert c.put(f"{PREFIX}/v1/settings/kanban_mode", json={}).status_code == 400
    llm = c.put(f"{PREFIX}/v1/llm", json={"fallback_chain": [{"provider": "b", "model": "m2"}]})
    assert llm.status_code == 200 and llm.json()["fallback_chain"][0]["provider"] == "b"
    assert c.put(f"{PREFIX}/v1/llm", json={}).status_code == 400


def test_managed_setting_is_forbidden(env):
    def refuse(key, value):
        raise PermissionError("managed by your administrator")
    from meeting_scribe.desktop.settings import SettingsStore
    api.SEAMS["settings_store"] = lambda: SettingsStore(lambda: {}, refuse)
    r = env["client"].put(f"{PREFIX}/v1/settings/kanban_mode", json={"value": "auto"})
    assert r.status_code == 403


def test_dashboard_loader_exposes_the_router():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_meeting_scribe_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.router is api.router
    manifest = json.loads((path.parent / "manifest.json").read_text())
    assert manifest["name"] == "meeting-scribe" and manifest["api"] == "plugin_api.py"
    assert (path.parent / manifest["entry"]).is_file()
