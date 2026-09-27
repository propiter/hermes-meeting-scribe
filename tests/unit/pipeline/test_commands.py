from dataclasses import replace

from meeting_scribe.commands import Caller, MeetingCommands
from meeting_scribe.domain.models import MeetingState, Stage
from meeting_scribe.pipeline.service import MeetingService

from .test_runner import build, drain

CALLER = Caller(platform="discord", chat_id="555", user_id="10", thread_id="")


class FakeCapture:
    def __init__(self):
        self.calls = []

    def start(self, caller, target):
        self.calls.append(("start", target))
        return "recording!"

    def stop(self, caller):
        self.calls.append(("stop",))
        return "stopped"

    def live_meeting_ids(self):
        return set()


def make(prepo, layout, settings, clock, capture=None):
    runner, *_ = build(prepo, layout, settings, clock)
    service = MeetingService(prepo, layout, runner, settings, clock=clock, item_sinks=lambda: {},
                             catalogs=lambda: runner.stages.catalogs())
    return MeetingCommands(service, settings, capture=lambda: capture), service, runner


def processed(service, runner, meeting):
    live = service.begin_recording(replace(meeting, state=MeetingState.RECORDING, ended_at=None))
    service.finish_recording(live.id)
    drain(runner)
    return live.id


def test_help_and_unknown(prepo, layout, settings, clock):
    cmds, *_ = make(prepo, layout, settings, clock)
    assert "/meeting" in cmds.handle("help", CALLER, "meeting")
    assert "`bogus`" in cmds.handle("bogus", CALLER, "meeting")


def test_start_stop_without_capture_is_honest(prepo, layout, settings, clock):
    cmds, *_ = make(prepo, layout, settings, clock)
    assert "not available" in cmds.handle("", CALLER, "meeting")
    assert "not available" in cmds.handle("stop", CALLER, "meeting")


def test_start_stop_delegate_to_capture(prepo, layout, settings, clock):
    cap = FakeCapture()
    cmds, *_ = make(prepo, layout, settings, clock, capture=cap)
    assert cmds.handle("", CALLER, "rec") == "recording!"
    assert cmds.handle("start <#123>", CALLER, "rec") == "recording!"
    assert cmds.handle("stop", CALLER, "rec") == "stopped"
    assert cap.calls == [("start", None), ("start", "<#123>"), ("stop",)]


def test_list_show_search_status(prepo, layout, settings, clock, meeting):
    cmds, service, runner = make(prepo, layout, settings, clock)
    mid = processed(service, runner, meeting)
    assert mid in cmds.handle("list 5", CALLER, "meeting")
    assert "Informe semanal" in cmds.handle(f"show {mid[:5]}", CALLER, "meeting")
    assert "Yo envío el informe" in cmds.handle("search informe", CALLER, "meeting")
    assert "Queue: 0" in cmds.handle("status", CALLER, "meeting")
    assert "No meeting" in cmds.handle("show zzz", CALLER, "meeting")


def test_reprocess_and_project_and_link(prepo, layout, settings, clock, meeting):
    cmds, service, runner = make(prepo, layout, settings, clock)
    mid = processed(service, runner, meeting)
    assert "analyze" in cmds.handle(f"reprocess {mid} from=analyze", CALLER, "meeting")
    assert prepo.get_job(mid).stage is Stage.ANALYZE
    drain(runner)
    assert "Unknown stage" in cmds.handle(f"reprocess {mid} from=bogus", CALLER, "meeting")
    assert "Website" in cmds.handle(f"project {mid} Website", CALLER, "meeting")
    assert "Candidates" in cmds.handle(f"project {mid} Nope", CALLER, "meeting")
    assert "Linked" in cmds.handle("link <@11> luis@x.io", CALLER, "meeting")
    assert prepo.get_link("11")["email"] == "luis@x.io"
    assert "Usage" in cmds.handle("link", CALLER, "meeting")


def test_config_and_spanish(prepo, layout, settings, clock):
    from meeting_scribe.config import settings_from_mapping
    s = settings_from_mapping({"ui_language": "es"})
    cmds, *_ = make(prepo, layout, lambda: s, clock)
    assert "`kanban_mode` = approve" in cmds.handle("config", CALLER, "meeting")
    assert "Ninguna reunión" in cmds.handle("list", CALLER, "meeting") or "Aún no" in cmds.handle(
        "list", CALLER, "meeting")


def test_handler_never_raises(prepo, layout, settings, clock):
    cmds, service, _ = make(prepo, layout, settings, clock)
    service.search = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone"))
    assert "db gone" in cmds.handle("search x", CALLER, "meeting")
