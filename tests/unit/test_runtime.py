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
    cfg["kanban.mode"] = "auto"
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
    rt.set_config("kanban.mode", "off")
    assert cfg["kanban.mode"] == "off"


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
