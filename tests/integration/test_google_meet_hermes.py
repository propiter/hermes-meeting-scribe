"""Google Meet import end to end through the REAL Hermes plugin loader (DESIGN §17). No network.

A throwaway HERMES_HOME; the plugin is loaded by Hermes' PluginManager; the Meet REST API and the
Google token endpoint are answered by an injected in-process transport; the Discord side is the
unit-suite fake adapter behind the real sink + real ViewKit (discord.py). A deterministic analyzer
replaces the LLM. Checks: import → TRANSCRIBED → ANALYZE → DELIVER → DONE, notes and the full
transcript attachment land in ``google_meet_discord_channel`` (given by name), and a second sync imports nothing.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
plugins_mod = pytest.importorskip("hermes_cli.plugins", reason="Hermes is not importable (set PYTHONPATH)")
discord = pytest.importorskip("discord")
yaml = pytest.importorskip("yaml")

from meeting_scribe.domain.models import ActionItem, MeetingState, Notes  # noqa: E402
from meeting_scribe.google.oauth import write_private_json  # noqa: E402
from tests.unit.discord_ui.fakes import FakeAdapter, FakeBot  # noqa: E402
from tests.unit.gmeet.fake_meet import FakeMeet  # noqa: E402
from tests.unit.gmeet.fakes import CLIENT_JSON, FakeTransport, jresp  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
IGNORE = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "*.pyc", "tests")


class DeterministicAnalyzer:
    def analyze(self, meeting, utterances, candidates):
        owner = next((u.speaker_id for u in utterances if "presentación" in u.text), None)
        return Notes(meeting_title="Plan de lanzamiento", tldr="Lanzar el lunes.", summary="Se revisó el lanzamiento.",
                     decisions=("Lanzar el lunes",), language="es",
                     action_items=(ActionItem(id="i1", title="Preparar la presentación", owner_speaker_id=owner,
                                              owner_name="Guest Two"),))


@pytest.fixture
def manager(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(REPO, home / "plugins" / "meeting-scribe", ignore=IGNORE)
    # by NAME (DESIGN §19): resolved at delivery time against the fake server below
    settings = {"google_meet_enabled": True, "google_meet_discord_channel": "#meet-notes", "kanban_mode": "off",
                "linear_mode": "off", "audio_retention": "none"}
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["meeting-scribe"], "entries": {
        "meeting-scribe": {"settings": settings}}}}), encoding="utf-8")
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)  # not a gateway unless a test says so
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: bundled)
    mgr = plugins_mod.PluginManager()
    mgr.discover_and_load()
    yield mgr, home
    import meeting_scribe.plugin as plugin

    for rt in list(plugin.RUNTIMES.values()):
        rt.close()
    plugin.RUNTIMES.clear()
    if "meeting-scribe" in mgr._plugins:
        mgr.unload("meeting-scribe")


def test_meet_conference_is_imported_processed_and_delivered_with_transcript(manager):
    mgr, home = manager
    loaded = mgr._plugins["meeting-scribe"]
    assert loaded.enabled and loaded.module is not None, getattr(loaded, "error", None)
    import meeting_scribe.discord_ui as ui
    import meeting_scribe.plugin as plugin

    rt = next(iter(plugin.RUNTIMES.values()))
    assert rt.settings().google_meet_enabled

    # Google side: stored client + token (as `google connect` leaves them) and a fake API.
    gdir = home / "plugin-data" / "meeting-scribe" / "google"
    write_private_json(gdir / "client.json", CLIENT_JSON)
    write_private_json(gdir / "token.json", {"access_token": "at", "refresh_token": "rt", "expires_at": 0,
                                             "connected_at": time.time() - 86400})
    meet = FakeMeet()
    meet.add("conf1")

    def handler(method, url, headers, body):
        if url.startswith("https://oauth2.googleapis.com/token"):
            return jresp(200, {"access_token": "at-new", "expires_in": 3600})
        return meet(method, url, headers, body)
    rt.google_transport = FakeTransport(handler)

    # Discord side: fake adapter on a real running loop, real sink + ViewKit.
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    bot = FakeBot()
    chat = bot.add(777, "meet-notes", threads_ok=False)
    state = ui.state_for(rt)
    adapter = FakeAdapter(bot)
    state.adapter_ref = lambda: adapter
    state.loop = loop
    try:
        svc = rt.service()
        svc.runner.stages.analyzer = DeterministicAnalyzer()
        report = rt.meet_importer().sync(ended_after=rt.meet_importer().window_start(days=3))
        assert report.errors == [] and len(report.imported) == 1, report.as_dict()
        mid = report.imported[0]
        assert svc.repo.get_meeting(mid).state is MeetingState.TRANSCRIBED
        while svc.runner.run_once():
            pass
        m = svc.repo.get_meeting(mid)
        assert m.state is MeetingState.DONE, svc.repo.get_job(mid)
        contents = "\n".join(msg.content or "" for msg in chat.ordered())
        assert "Lanzar el lunes" in contents and "Preparar la presentación" in contents
        assert "<@gmeet" not in contents
        files = [msg.file for msg in chat.ordered() if getattr(msg, "file", None) is not None]
        assert len(files) == 1 and isinstance(files[0], discord.File)
        assert files[0].filename == "transcript-2026-09-26-plan-de-lanzamiento.md"
        body = files[0].fp.read().decode("utf-8")
        assert "Ana Example:** Hola, revisemos el lanzamiento." in body and "Guest Two:**" in body
        # idempotent: a second sync imports nothing, a redelivery attaches nothing new
        again = rt.meet_importer().sync(ended_after=rt.meet_importer().window_start(days=3))
        assert again.imported == [] and again.already == 1
        svc.reprocess(mid, __import__("meeting_scribe.domain.models", fromlist=["Stage"]).Stage.DELIVER)
        while svc.runner.run_once():
            pass
        assert len([x for x in chat.ordered() if getattr(x, "file", None) is not None]) == 1
        st = json.loads(json.dumps(rt.meet_importer().status()))
        assert st["last_poll_ok"] == "1" and st["last_import_meeting"] == mid
        token = json.loads((gdir / "token.json").read_text())
        assert token["access_token"] == "at-new" and (gdir / "token.json").stat().st_mode & 0o777 == 0o600
    finally:
        loop.call_soon_threadsafe(loop.stop)
