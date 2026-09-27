"""Composition root wiring with a fake host: no Hermes needed."""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace


from meeting_scribe.runtime import Host, Runtime
from meeting_scribe.sinks.linear import LinearGraphQL, LinearMcp


class FakeKanban:
    def __init__(self):
        self.created = []

    def create_task(self, **kw):
        self.created.append(kw)
        return "t1"

    def list_boards(self):
        return [{"slug": "default", "name": "Default"}]


def host(tmp_path: Path, config=None, secrets=None, mcp_allowed=False):
    cfg = dict(config or {})
    sec = dict(secrets or {})
    return Host(
        get_config=lambda key, default=None: cfg.get(key, default),
        set_config=lambda key, value: cfg.__setitem__(key, value),
        data_dir=lambda: tmp_path / "data",
        llm=lambda: SimpleNamespace(),
        secret=lambda name: sec.get(name),
        spawner=lambda target, *, name, daemon=True: threading.Thread(target=target, name=name, daemon=daemon),
        call_mcp=lambda: (lambda server, tool, args: {"ok": True, "result": "{}"}) if mcp_allowed else None,
        kanban=FakeKanban(),
        project_sources=lambda: [],
        llm_ready=lambda: (True, "ok"),
    ), cfg


def test_settings_are_read_per_call(tmp_path):
    h, cfg = host(tmp_path)
    rt = Runtime(h)
    assert rt.settings().kanban_mode == "approve"
    cfg["kanban_mode"] = "auto"
    assert rt.settings().kanban_mode == "auto"


def test_sinks_composition(tmp_path):
    h, cfg = host(tmp_path)
    rt = Runtime(h)
    names = [s.name for s in rt.sinks()]
    assert names == ["files", "obsidian", "kanban", "linear"]
    assert [s.name for s in rt.sinks() if s.enabled()] == ["files", "kanban"]


def test_extra_sinks_for_phase_b(tmp_path):
    h, _ = host(tmp_path)
    rt = Runtime(h)

    class DiscordSink:
        name = "discord"

        def enabled(self):
            return True

        def deliver(self, meeting, notes, folder):
            raise AssertionError

    rt.add_sink(DiscordSink())
    assert [s.name for s in rt.sinks()][-1] == "discord"


def test_linear_backend_selection(tmp_path):
    h, _ = host(tmp_path, secrets={"LINEAR_API_KEY": "k"})
    assert isinstance(Runtime(h).linear_backend(), LinearGraphQL)
    h, _ = host(tmp_path, mcp_allowed=True)
    assert isinstance(Runtime(h).linear_backend(), LinearMcp)
    h, _ = host(tmp_path)
    assert Runtime(h).linear_backend() is None


def test_owners_fall_back_to_allowed_users(tmp_path):
    h, cfg = host(tmp_path, secrets={"DISCORD_ALLOWED_USERS": "42, 43"})
    rt = Runtime(h)
    assert rt.owners() == ("42",)
    cfg["owners"] = ["7"]
    assert rt.owners() == ("7",)


def test_repo_follows_data_dir(tmp_path):
    root = {"p": tmp_path / "a"}
    h, _ = host(tmp_path)
    h.data_dir = lambda: root["p"]
    rt = Runtime(h)
    first = rt.repo()
    assert rt.repo() is first
    root["p"] = tmp_path / "b"
    assert rt.repo() is not first and (tmp_path / "b" / "index.sqlite").exists()
    rt.close()


def test_service_and_worker_lifecycle(tmp_path):
    h, _ = host(tmp_path)
    rt = Runtime(h)
    svc = rt.service()
    assert svc is rt.service()
    rt.start_pipeline(live_meeting_ids=())
    assert rt.pipeline_running()
    rt.stop_pipeline()
    assert not rt.pipeline_running()
    rt.close()


def test_capture_absent_by_default(tmp_path):
    h, _ = host(tmp_path)
    rt = Runtime(h)
    assert rt.capture is None
    ok, detail = rt.capture_status()
    assert ok is False and "capture" in detail.lower()


def test_catalogs_include_learned_and_linear(tmp_path):
    h, _ = host(tmp_path)
    names = [c.name for c in Runtime(h).catalogs()]
    assert names == ["learned", "linear"]


def test_runtime_is_a_doctor_env(tmp_path):
    h, cfg = host(tmp_path)
    rt = Runtime(h)
    env = rt.doctor_env()
    assert env.data_dir() == tmp_path / "data"
    assert env.kanban_boards()[0]["slug"] == "default"
    assert env.llm_status() == (True, "ok")
    rt.set_config("kanban_mode", "off")
    assert cfg["kanban_mode"] == "off"


def test_switching_data_dir_never_closes_a_repo_in_use(tmp_path):
    """Review finding 7: a handler holding the old service must keep a working connection."""
    root = {"p": tmp_path / "a"}
    h, _ = host(tmp_path)
    h.data_dir = lambda: root["p"]
    rt = Runtime(h)
    old_svc = rt.service()
    root["p"] = tmp_path / "b"
    new_svc = rt.service()
    assert new_svc is not old_svc
    assert old_svc.repo.list_meetings(limit=1) == []  # not "Cannot operate on a closed database"
    root["p"] = tmp_path / "a"
    assert rt.service() is old_svc  # no flip-flop reopen: repos are kept per path
    rt.close()


def test_commands_resolve_the_service_per_call(tmp_path):
    from meeting_scribe.commands import MeetingCommands

    root = {"p": tmp_path / "a"}
    h, _ = host(tmp_path)
    h.data_dir = lambda: root["p"]
    rt = Runtime(h)
    cmds = MeetingCommands(rt.service, rt.settings, capture=lambda: None)
    first = cmds.service
    root["p"] = tmp_path / "b"
    assert cmds.service is not first and cmds.service.repo is rt.repo()
    rt.close()


# -- review finding 5: sink routing -------------------------------------------------------------
def _routing_runtime(tmp_path, cands):
    from meeting_scribe.analyze.projects import CallableCatalog

    h, _ = host(tmp_path)
    h.project_sources = lambda: [CallableCatalog("src", lambda m: list(cands))]
    return Runtime(h)


def test_same_named_projects_route_per_sink(tmp_path, meeting):
    from meeting_scribe.domain.models import ActionItem, Candidate, Notes

    hermes = Candidate("hermes:p", "Website", "hermes", {"project_id": "p"})
    linear = Candidate("linear:L", "Website", "linear", {"project_id": "L", "team_ids": ["t"]})
    rt = _routing_runtime(tmp_path, [hermes, linear])
    item, notes = ActionItem(id="a1", title="x"), Notes("t", "t", "s", project="Website")
    sinks = rt.item_sinks()
    assert sinks["linear"]._project_for(meeting, notes, item) == linear
    assert sinks["kanban"]._project_for(meeting, notes, item) == hermes
    rt.close()


def test_learned_candidate_enriched_by_the_real_catalog(tmp_path, meeting):
    from dataclasses import replace as dc_replace

    from meeting_scribe.domain.models import ActionItem, Candidate, Notes

    linear = Candidate("linear:L", "Website", "linear", {"project_id": "L", "team_ids": ["t"]})
    rt = _routing_runtime(tmp_path, [])
    rt.catalogs = lambda: [__import__("meeting_scribe.analyze.projects", fromlist=["x"]).LearnedCatalog(rt.repo()),
                           __import__("meeting_scribe.analyze.projects", fromlist=["x"]).CallableCatalog(
                               "linear", lambda m: [linear])]
    rt.repo().learn_channel_project(meeting.channel_id, "linear:L", "Website")
    m = dc_replace(meeting, project="Website", project_key="linear:L")
    got = rt.item_sinks()["linear"]._project_for(m, Notes("t", "t", "s"), ActionItem(id="a1", title="x"))
    assert got == linear and got.ref["team_ids"] == ["t"]  # not the ref-less learned stub
    rt.close()


def test_resolved_key_wins_over_first_name_match(tmp_path, meeting):
    from dataclasses import replace as dc_replace

    from meeting_scribe.domain.models import ActionItem, Candidate, Notes

    a = Candidate("linear:A", "Website", "linear", {"project_id": "A", "team_ids": ["ta"]})
    b = Candidate("linear:B", "Website", "linear", {"project_id": "B", "team_ids": ["tb"]})
    rt = _routing_runtime(tmp_path, [a, b])
    m = dc_replace(meeting, project="Website", project_key="linear:B")
    assert rt.item_sinks()["linear"]._project_for(m, Notes("t", "t", "s"), ActionItem(id="a1", title="x")) == b
    rt.close()


def test_extra_catalogs_join_the_candidates(tmp_path):
    """Phase B adds the Discord channels of the guild as project candidates (DESIGN §16)."""
    h, _ = host(tmp_path)
    rt = Runtime(h)
    extra = object()
    rt.add_catalog(extra)
    assert rt.catalogs()[-1] is extra


def test_meet_poller_follows_the_pipeline_lifecycle(tmp_path):
    """DESIGN §17: the poller thread starts with the worker (gateway) and stops with it (reload)."""
    h, cfg = host(tmp_path, {"google_meet_enabled": True})
    rt = Runtime(h)
    rt.start_pipeline()
    try:
        assert rt.meet_poller_running
        rt.start_pipeline()  # idempotent: still one poller
        assert rt.meet_poller_running
    finally:
        rt.stop_pipeline()
    assert not rt.meet_poller_running
    rt.close()


def test_google_paths_live_under_the_profile_data_dir(tmp_path):
    h, _ = host(tmp_path)
    rt = Runtime(h)
    files = rt.google_files()
    assert files.client_path == tmp_path / "data" / "google" / "client.json"
    assert files.token_path == tmp_path / "data" / "google" / "token.json"
    assert rt.google_connected_at() is None and not rt.google_credentials().connected()


def test_close_does_not_deadlock_with_a_poller_waiting_for_the_runtime_lock(tmp_path, caplog, monkeypatch):
    """close() used to hold the runtime lock while joining the poller, whose tick needed that lock:
    the join timed out and the thread then used the closed database (finding 14)."""
    import logging
    import time
    from meeting_scribe.google.importer import MeetPoller
    monkeypatch.setattr(MeetPoller, "FIRST_DELAY", 0.0)
    entered = threading.Event()
    real_tick = MeetPoller.tick

    def slow_tick(self):
        entered.set()
        time.sleep(0.3)  # close() grabs the runtime lock meanwhile
        return real_tick(self)
    monkeypatch.setattr(MeetPoller, "tick", slow_tick)
    h, _cfg = host(tmp_path, config={"google_meet_enabled": True})
    rt = Runtime(h)
    rt.start_pipeline()
    poller = rt._meet_poller
    assert entered.wait(5)
    caplog.set_level(logging.ERROR)
    t0 = time.monotonic()
    rt.close()
    assert time.monotonic() - t0 < 5
    assert not poller.running
    assert not any("closed database" in (r.getMessage() + str(r.exc_info)) for r in caplog.records)
