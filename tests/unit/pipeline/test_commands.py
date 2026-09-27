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
    assert "isn't available" in cmds.handle("", CALLER, "meeting")
    assert "isn't available" in cmds.handle("stop", CALLER, "meeting")


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
    assert "waiting to be processed: 0" in cmds.handle("status", CALLER, "meeting")
    assert "No meeting" in cmds.handle("show zzz", CALLER, "meeting")


def test_reprocess_and_project_and_link(prepo, layout, settings, clock, meeting):
    cmds, service, runner = make(prepo, layout, settings, clock)
    mid = processed(service, runner, meeting)
    assert "regenerating the notes" in cmds.handle(f"reprocess {mid} from=analyze", CALLER, "meeting")
    assert prepo.get_job(mid).stage is Stage.ANALYZE
    drain(runner)
    assert "from=deliver" in cmds.handle(f"reprocess {mid} from=bogus", CALLER, "meeting")
    assert "Website" in cmds.handle(f"project {mid} Website", CALLER, "meeting")
    assert "Available" in cmds.handle(f"project {mid} Nope", CALLER, "meeting")
    assert "Linked" in cmds.handle("link <@11> luis@x.io", CALLER, "meeting")
    assert prepo.get_link("11")["email"] == "luis@x.io"
    assert "Usage" in cmds.handle("link", CALLER, "meeting")


def test_config_and_spanish(prepo, layout, settings, clock):
    from meeting_scribe.config import settings_from_mapping
    s = settings_from_mapping({"ui_language": "es"})
    cmds, *_ = make(prepo, layout, lambda: s, clock)
    out = cmds.handle("config", CALLER, "meeting")
    assert "**Kanban**: approve" in out and "kanban_mode" not in out
    assert "Ninguna reunión" in cmds.handle("list", CALLER, "meeting") or "Aún no" in cmds.handle(
        "list", CALLER, "meeting")


def test_handler_never_raises_nor_shows_the_exception(prepo, layout, settings, clock, caplog):
    cmds, service, _ = make(prepo, layout, settings, clock)
    service.search = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone"))
    out = cmds.handle("search x", CALLER, "meeting")
    assert "db gone" not in out and "RuntimeError" not in out and "administrator" in out
    assert "db gone" in caplog.text  # the details stay in the log for the administrator


def test_only_a_reprocess_from_deliver_asks_to_move_notes_out_of_a_dm(prepo, layout, settings, clock, meeting):
    """DESIGN §19: the move out of a DM is explicit — ``reprocess <id> --from deliver`` (or from=deliver)."""
    from meeting_scribe.domain.models import KV_MOVE_FROM_DM

    cmds, service, runner = make(prepo, layout, settings, clock)
    mid = processed(service, runner, meeting)
    cmds.handle(f"reprocess {mid} from=analyze", CALLER, "meeting")
    assert prepo.kv_get(KV_MOVE_FROM_DM + mid) is None
    drain(runner)
    cmds.handle(f"reprocess {mid} from=deliver", CALLER, "meeting")
    assert prepo.kv_get(KV_MOVE_FROM_DM + mid) == "1"


def test_a_reprocess_that_finally_fails_forgets_the_move_request(prepo, layout, settings, clock, meeting):
    from meeting_scribe.domain.models import KV_MOVE_FROM_DM

    cmds, service, runner = make(prepo, layout, settings, clock)
    mid = processed(service, runner, meeting)
    runner.max_attempts = 1
    service.reprocess(mid, Stage.DELIVER)
    runner.stages.deliver = lambda m: (_ for _ in ()).throw(RuntimeError("cannot move"))
    drain(runner)
    assert prepo.get_meeting(mid).state is MeetingState.FAILED
    assert prepo.kv_get(KV_MOVE_FROM_DM + mid) is None  # a later automatic retry never moves by itself


def test_reprocess_reply_repeats_how_to_move_notes_left_in_a_dm(prepo, layout, settings, clock, meeting):
    from meeting_scribe.domain.models import KV_DM_NOTES

    cmds, service, runner = make(prepo, layout, settings, clock)
    mid = processed(service, runner, meeting)
    prepo.kv_set(KV_DM_NOTES + mid, "no notes channel is configured. Run `hermes meeting-scribe config set x`")
    out = cmds.handle(f"reprocess {mid} from=deliver", CALLER, "meeting")
    assert "direct message" in out and "config set" not in out  # admin commands stay in the CLI/doctor
    drain(runner)
    out = cmds.handle(f"reprocess {mid} from=analyze", CALLER, "meeting")
    assert "`from=deliver`" in out and "config set" not in out


def test_states_and_stages_are_shown_in_plain_words(prepo, layout, settings, clock, meeting):
    cmds, service, runner = make(prepo, layout, settings, clock)
    mid = processed(service, runner, meeting)
    listed = cmds.handle("list", CALLER, "meeting")
    assert "Ready" in listed and " done" not in listed
    runner.max_attempts = 1
    service.reprocess(mid, Stage.DELIVER)
    runner.stages.deliver = lambda m: (_ for _ in ()).throw(RuntimeError("HTTP 403 Missing Access"))
    drain(runner)
    status = cmds.handle("status", CALLER, "meeting")
    assert "Couldn't finish" in status and "posting the notes" in status
    assert "403" not in status and "RuntimeError" not in status and "failed" not in status
    reply = cmds.handle(f"reprocess {mid} from=transcribe", CALLER, "meeting")
    assert "starting again from the audio" in reply and "from transcribe" not in reply


def test_states_and_stages_in_spanish(prepo, layout, clock, meeting):
    from meeting_scribe.config import settings_from_mapping
    s = settings_from_mapping({"ui_language": "es"})
    cmds, service, runner = make(prepo, layout, lambda: s, clock)
    mid = processed(service, runner, meeting)
    assert "Lista" in cmds.handle("list", CALLER, "meeting")
    assert "volviendo a publicar las notas" in cmds.handle(f"reprocess {mid} from=deliver", CALLER, "meeting")


def test_every_state_and_stage_has_a_label():
    from meeting_scribe.commands import stage_label, state_label
    for lang in ("en", "es"):
        for st in MeetingState:
            assert state_label(st, lang) != st.value
        for sg in Stage:
            assert stage_label(sg, lang) != sg.value
        for sg in (Stage.TRANSCRIBE, Stage.ANALYZE, Stage.DELIVER):
            assert stage_label(sg, lang, redo=True) != sg.value
