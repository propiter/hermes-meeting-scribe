"""Models and fallbacks (``auxiliary.meeting_scribe``) through an in-memory store."""
from __future__ import annotations

import time
from typing import Any

import pytest

from meeting_scribe import llm_config as lc


class MemStore:
    def __init__(self, task: dict | None = None, main: lc.Link = lc.Link("mainprov", "main-model")):
        self.task = dict(task or {})
        self.main = main
        self.writes: list[dict] = []
        self.probes: dict[str, Any] = {}

    def user_task_config(self):
        return self.task

    def main_model(self):
        return self.main

    def write(self, values):
        self.writes.append(dict(values))
        self.task.update(values)

    def probe(self, link, timeout):
        outcome = self.probes.get(link.provider, (True, "answered"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_defaults_mean_hermes_main_model_and_no_fallback():
    v = lc.view(MemStore())
    assert v.primary == lc.Link("auto") and v.fallback_chain == ()
    assert v.effective_primary == lc.Link("mainprov", "main-model")
    assert v.sources["provider"] == "plugin-default" and v.sources["fallback_chain"] == "none"
    assert v.timeout == 600


def test_user_config_wins_and_sources_say_so():
    store = MemStore({"provider": "prov-a", "model": "model-a", "timeout": 120,
                      "fallback_chain": [{"provider": "prov-b", "model": "m-b"}, {"model": "orphan"}]})
    v = lc.view(store)
    assert v.effective_primary == lc.Link("prov-a", "model-a") and v.timeout == 120
    assert v.fallback_chain == (lc.Link("prov-b", "m-b"),)
    assert v.sources["provider"] == "hermes-config" and v.sources["fallback_chain"] == "hermes-config"
    assert any("fallback_chain[1]" in p for p in v.problems)


@pytest.mark.parametrize("spec,link", [
    ("prov-a", lc.Link("prov-a")), ("prov-a:vendor/model-x", lc.Link("prov-a", "vendor/model-x")),
    ("custom:local:llama3:8b", lc.Link("custom:local", "llama3:8b")),
])
def test_parse_link(spec, link):
    assert lc.parse_link(spec) == link


@pytest.mark.parametrize("spec", ["", "bad provider", "prov:has space", "../x"])
def test_parse_link_rejects(spec):
    with pytest.raises(ValueError):
        lc.parse_link(spec)


def test_set_primary_writes_only_given_keys():
    store = MemStore()
    lc.set_primary(store, provider="prov-a", model="model-a")
    assert store.writes == [{"provider": "prov-a", "model": "model-a"}]
    with pytest.raises(ValueError):
        lc.set_primary(store)
    with pytest.raises(ValueError):
        lc.set_primary(store, timeout=0)


def test_fallback_add_remove_set_clear_keep_order():
    store = MemStore()
    lc.fallback_add(store, lc.parse_link("prov-b:m1"))
    lc.fallback_add(store, lc.parse_link("prov-c:m2"))
    lc.fallback_add(store, lc.parse_link("prov-d"), position=1)
    assert [lk.provider for lk in lc.view(store).fallback_chain] == ["prov-d", "prov-b", "prov-c"]
    with pytest.raises(ValueError, match="already"):
        lc.fallback_add(store, lc.parse_link("prov-b:m1"))
    lc.fallback_remove(store, "2")
    assert [lk.provider for lk in lc.view(store).fallback_chain] == ["prov-d", "prov-c"]
    lc.fallback_remove(store, "prov-c")
    assert [lk.provider for lk in lc.view(store).fallback_chain] == ["prov-d"]
    with pytest.raises(ValueError):
        lc.fallback_remove(store, "9")
    lc.fallback_set(store, [lc.parse_link("x:1"), lc.parse_link("y")])
    assert store.task["fallback_chain"] == [{"provider": "x", "model": "1"}, {"provider": "y"}]
    lc.fallback_clear(store)
    assert store.task["fallback_chain"] == [] and lc.view(store).fallback_chain == ()


def test_test_chain_probes_every_link_and_redacts():
    store = MemStore({"provider": "prov-a", "fallback_chain": [{"provider": "prov-b"}, {"provider": "prov-c"}]})
    store.probes = {"prov-b": (False, "401 bad key sk-abcdefghijklmnop"), "prov-c": RuntimeError("down")}
    rows = lc.test_chain(store)
    assert [r["role"] for r in rows] == ["primary", "fallback 1", "fallback 2"]
    assert [r["ok"] for r in rows] == [True, False, False]
    assert "sk-abcdefghijklmnop" not in rows[1]["detail"] and "down" in rows[2]["detail"]


def test_run_with_deadline_abandons_a_hung_call():
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        lc.run_with_deadline(lambda: time.sleep(5), 0.2, name="t")
    assert time.monotonic() - t0 < 2
    assert lc.run_with_deadline(lambda: 7, 1, name="t") == 7
    with pytest.raises(KeyError):
        lc.run_with_deadline(lambda: {}["x"], 1, name="t")


def test_view_serialises():
    d = lc.view(MemStore({"fallback_chain": [{"provider": "b"}]})).to_dict()
    assert d["config_path"] == "auxiliary.meeting_scribe" and d["fallback_chain"] == [{"provider": "b"}]
    assert d["effective"] == {"provider": "mainprov", "model": "main-model"}
