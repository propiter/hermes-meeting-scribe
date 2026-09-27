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
    lc.fallback_add(store, lc.parse_link("prov-d:m0"), position=1)
    assert [lk.provider for lk in lc.view(store).fallback_chain] == ["prov-d", "prov-b", "prov-c"]
    with pytest.raises(ValueError, match="already"):
        lc.fallback_add(store, lc.parse_link("prov-b:m1"))
    lc.fallback_remove(store, "2")
    assert [lk.provider for lk in lc.view(store).fallback_chain] == ["prov-d", "prov-c"]
    lc.fallback_remove(store, "prov-c")
    assert [lk.provider for lk in lc.view(store).fallback_chain] == ["prov-d"]
    with pytest.raises(ValueError):
        lc.fallback_remove(store, "9")
    lc.fallback_set(store, [lc.parse_link("x:1"), lc.parse_link("y:2")])
    assert store.task["fallback_chain"] == [{"provider": "x", "model": "1"}, {"provider": "y", "model": "2"}]
    lc.fallback_clear(store)
    assert store.task["fallback_chain"] == [] and lc.view(store).fallback_chain == ()


def test_test_chain_probes_every_link_and_redacts():
    store = MemStore({"provider": "prov-a", "fallback_chain": [{"provider": "prov-b", "model": "b"},
                                                               {"provider": "prov-c", "model": "c"}]})
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


# -- review I5: Hermes ignores a fallback without a model ---------------------------------------------
def test_a_fallback_needs_a_model():
    store = MemStore()
    with pytest.raises(ValueError, match="model"):
        lc.fallback_add(store, lc.parse_link("prov-b"))
    with pytest.raises(ValueError, match="model"):
        lc.fallback_set(store, [lc.parse_link("prov-b:m1"), lc.parse_link("prov-c")])
    assert store.writes == []


def test_existing_fallbacks_without_a_model_are_reported():
    v = lc.view(MemStore({"fallback_chain": [{"provider": "prov-b"}, {"provider": "prov-c", "model": "m"}]}))
    assert [lk.provider for lk in v.fallback_chain] == ["prov-b", "prov-c"]  # still listed (to remove it)
    assert any("fallback_chain[0]" in p and "model" in p for p in v.problems)
    assert not any("fallback_chain[1]" in p for p in v.problems)


# -- review I6: hand-written keys of an entry survive add/remove/set -----------------------------------
HAND = [{"provider": "prov-b", "model": "m-b", "key_env": "ALT_KEY", "api_mode": "codex_responses"},
        {"provider": "prov-c", "model": "m-c", "transport": "responses", "api_key": "${OTHER}"}]


def test_add_remove_set_keep_unknown_keys_of_other_entries():
    import copy

    store = MemStore({"fallback_chain": copy.deepcopy(HAND)})
    lc.fallback_add(store, lc.parse_link("prov-d:m-d"), position=1)
    assert store.task["fallback_chain"][1:] == HAND
    lc.fallback_remove(store, "1")
    assert store.task["fallback_chain"] == HAND
    lc.fallback_remove(store, "prov-c")
    assert store.task["fallback_chain"] == HAND[:1]
    lc.fallback_set(store, [lc.parse_link("prov-e:m-e"), lc.parse_link("prov-b:m-b")])
    assert store.task["fallback_chain"] == [{"provider": "prov-e", "model": "m-e"}, HAND[0]]


def test_remove_by_position_counts_listed_entries_and_keeps_malformed_ones():
    raw = [{"model": "orphan"}, {"provider": "prov-b", "model": "m"}, {"provider": "prov-c", "model": "n"}]
    store = MemStore({"fallback_chain": list(raw)})
    lc.fallback_remove(store, "1")
    assert store.task["fallback_chain"] == [raw[0], raw[2]]


# -- review M6: never print a token carried in a base_url ------------------------------------------------
def test_labels_and_json_never_show_url_credentials():
    link = lc.Link("custom:x", "m", "https://user:pw@llm.example/v1?api_key=SECRET&x=1")
    assert "SECRET" not in link.label() and "pw" not in link.label() and "llm.example/v1" in link.label()
    store = MemStore({"provider": "custom:x", "model": "m", "base_url": link.base_url,
                      "fallback_chain": [{"provider": "p", "model": "m", "base_url": link.base_url}]})
    import json

    text = json.dumps(lc.view(store).to_dict())
    assert "SECRET" not in text and "pw@" not in text
    rows = lc.test_chain(store)
    assert all("SECRET" not in r["link"] for r in rows)
    assert "SECRET" not in lc.redact("failed calling https://llm.example/v1?token=SECRET")


# -- review M7: a value equal to the default is reported as "using the default" --------------------------
def test_values_equal_to_the_default_are_reported_as_default():
    store = MemStore()
    lc.set_primary(store, provider="auto", model="vendor/m")
    assert lc.defaults_among({"provider": "auto", "model": "vendor/m"}) == ["provider"]
    assert lc.defaults_among({"timeout": 600.0}) == ["timeout"]


# -- review M5: at most a couple of hung calls are left behind -------------------------------------------
def test_run_with_deadline_refuses_to_pile_up_hung_threads():
    import threading

    release = threading.Event()
    try:
        for _ in range(lc.MAX_ABANDONED):
            with pytest.raises(TimeoutError):
                lc.run_with_deadline(release.wait, 0.05, name="pile")
        with pytest.raises(lc.TooManyHungCalls, match="still running"):
            lc.run_with_deadline(lambda: 1, 1, name="pile")
        assert lc.run_with_deadline(lambda: 2, 1, name="other") == 2  # per task name
    finally:
        release.set()
    deadline = time.time() + 2
    while lc.abandoned_alive("pile") and time.time() < deadline:
        time.sleep(0.01)
    assert lc.run_with_deadline(lambda: 3, 1, name="pile") == 3  # finished threads free the slot
