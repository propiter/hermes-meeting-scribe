"""Desktop settings form, LLM chain editing and dashboard-side doctor (no Runtime, no secrets)."""
import json
from types import SimpleNamespace

import pytest

from meeting_scribe import llm_config as lc
from meeting_scribe.config import SPEC
from meeting_scribe.desktop import settings as ds
from meeting_scribe.storage.repo import Repository
from tests.unit.test_llm_config import MemStore


class MemSettings:
    def __init__(self, settings=None):
        self.entry = {"settings": dict(settings or {})}
        self.writes = []

    def store(self):
        def write(key, value):
            self.writes.append((key, value))
            node = self.entry["settings"]
            *parents, leaf = key.split(".")
            for p in parents:
                node = node.setdefault(p, {})
            node[leaf] = value
        return ds.SettingsStore(lambda: self.entry, write)


def test_view_is_generated_from_the_central_spec():
    mem = MemSettings({"transcribe_language": "en", "kanban_mode": "maybe"})
    view = ds.settings_view(mem.store(), "es")
    assert set(view["values"]) == set(SPEC)  # every setting, none duplicated in the page
    assert view["values"]["transcribe_language"] == {"value": "en", "origin": "configured"}
    assert view["values"]["transcribe_model"]["origin"] == "default"
    assert view["values"]["kanban_mode"]["origin"] == "invalid"
    keys = [f["key"] for f in view["schema"]["fields"] if f["storage"] == "plugin"]
    assert sorted(keys) == sorted(SPEC)
    assert {f["key"] for f in view["schema"]["fields"] if f["storage"] == "hermes"} >= {"llm_fallback_chain"}
    assert view["schema"]["language"] == "es"


def test_view_reads_nested_and_legacy_config_subtree():
    store = ds.SettingsStore(lambda: {"config": {"transcribe": {"language": "en"}}}, lambda k, v: None)
    assert ds.settings_view(store, "en")["values"]["transcribe_language"]["value"] == "en"


def test_set_setting_validates_like_the_cli(tmp_path):
    mem = MemSettings()
    store = mem.store()
    assert ds.set_setting(store, None, "pipeline_max_attempts", "5")["value"] == 5
    with pytest.raises(KeyError):
        ds.set_setting(store, None, "nope", "1")
    with pytest.raises(ValueError):
        ds.set_setting(store, None, "kanban_mode", "maybe")
    assert mem.writes == [("pipeline_max_attempts", 5)]


def test_destination_change_requeues_waiting_deliveries(tmp_path, meeting):
    from datetime import datetime, timezone
    from meeting_scribe.domain.models import Stage

    repo = Repository(tmp_path / "index.sqlite")
    repo.save_meeting(meeting)
    repo.enqueue_job(meeting.id, Stage.DELIVER, now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    repo.kv_set("pipeline.waiting_destination." + meeting.id, "no channel")
    out = ds.set_setting(MemSettings().store(), repo, "delivery_discord_channel", "123")
    assert out["requeued"] == 1
    job = repo.get_job(meeting.id)
    assert job is not None and job.next_retry_at < datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()
    repo.close()


def test_llm_update_primary_and_fallbacks_through_llm_config():
    store = MemStore({"provider": "a", "model": "m1", "base_url": "https://u:pw@llm.example/v1?key=s"})
    view = ds.llm_update(store, {"fallback_chain": [{"provider": "b", "model": "m2"}]})
    assert [f["provider"] for f in view["fallback_chain"]] == ["b"]
    shown = view["base_url"]
    assert "pw" not in json.dumps(view) and "key=s" not in json.dumps(view)
    # Saving the form back with the displayed (redacted) base_url keeps the real one.
    ds.llm_update(store, {"provider": "a", "model": "m3", "base_url": shown})
    assert store.task["base_url"] == "https://u:pw@llm.example/v1?key=s" and store.task["model"] == "m3"
    ds.llm_update(store, {"fallback_chain": []})
    assert lc.view(store).fallback_chain == ()
    for bad in ({}, {"fallback_chain": "x"}, {"fallback_chain": ["x"]}, {"fallback_chain": [{"provider": "b"}]},
                {"fallback_chain": [{"provider": "b", "model": "m"}] * 11}, {"timeout": "soon"}):
        with pytest.raises(ValueError):
            ds.llm_update(store, bad)


def test_invalid_fallback_does_not_write_primary():
    store = MemStore({"provider": "old", "model": "original"})
    before = dict(store.task)
    with pytest.raises(ValueError):
        ds.llm_update(store, {"provider": "new", "fallback_chain": [{"provider": "openai"}]})
    assert store.task == before


def test_llm_update_keeps_redacted_fallback_base_url():
    store = MemStore({"provider": "a", "model": "m1",
                      "fallback_chain": [{"provider": "b", "model": "m2", "base_url": "https://k@x.example/v1"}]})
    view = ds.llm_view(store)
    ds.llm_update(store, {"fallback_chain": view["fallback_chain"]})
    assert store.task["fallback_chain"][0]["base_url"] == "https://k@x.example/v1"


def test_dashboard_doctor_runs_every_check_without_runtime(tmp_path):
    env = ds.DashboardDoctorEnv(store=MemSettings().store(), root=tmp_path,
                                repo=Repository(tmp_path / "index.sqlite"),
                                kanban=SimpleNamespace(list_boards=lambda: []), aux_store=MemStore())
    out = ds.run_doctor(env)
    names = [c["name"] for c in out["checks"]]
    assert "storage" in names and "capture" in names and "google_meet" in names
    crashed = [c for c in out["checks"] if "Error" in c["detail"] and c["status"] == "fail"]
    assert crashed == [], crashed
    env.repo.close()
