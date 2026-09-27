"""Reprocessing an imported (Google Meet) meeting: there is no audio, so TRANSCRIBE becomes ANALYZE."""
from __future__ import annotations

from dataclasses import replace

from meeting_scribe.domain.models import MeetingState, Stage
from meeting_scribe.transcribe.client import SubprocessTranscriber

from .test_meet_import import since, world  # noqa: F401 - fixture reuse
from .test_runner import drain


def _imported(world):
    service, runner, _a, _s, meet, importer = world
    meet.add("r1")
    mid = importer.sync(ended_after=since()).imported[0]
    drain(runner)
    return mid


def test_reprocess_from_transcribe_on_a_meet_meeting_analyzes_instead(world, clock):
    service, runner, analyzer, _s, _m, _i = world
    mid = _imported(world)
    runner.stages.transcriber = SubprocessTranscriber(lambda: service.settings(), lambda: None)  # would fail
    assert service.effective_stage(service.require(mid), Stage.TRANSCRIBE) is Stage.ANALYZE
    service.reprocess(mid, Stage.TRANSCRIBE)
    assert service.repo.get_job(mid).stage is Stage.ANALYZE
    drain(runner)
    assert service.repo.get_meeting(mid).state is MeetingState.DONE and analyzer.calls == 2


def test_runner_reprocess_maps_transcribe_for_imported_meetings(world):
    service, runner, _a, _s, _m, _i = world
    mid = _imported(world)
    assert runner.reprocess(mid, Stage.TRANSCRIBE) is Stage.ANALYZE
    assert service.repo.get_meeting(mid).state is MeetingState.TRANSCRIBED


def test_recover_rewinds_an_imported_meeting_stuck_in_transcribing(world):
    service, runner, _a, _s, _m, _i = world
    mid = _imported(world)
    m = service.repo.get_meeting(mid)
    service.repo.save_meeting(replace(m, state=MeetingState.TRANSCRIBING))  # crash mid-stage (old reprocess)
    service.repo.complete_job(service.repo.get_job(mid).id)
    runner.recover([])
    assert service.repo.get_meeting(mid).state is MeetingState.TRANSCRIBED
    assert service.repo.get_job(mid).stage is Stage.ANALYZE
    drain(runner)
    assert service.repo.get_meeting(mid).state is MeetingState.DONE


def test_command_and_cli_explain_the_mapping(world):
    from meeting_scribe.commands import MeetingCommands
    from meeting_scribe.config import settings_from_mapping
    from meeting_scribe.commands import Caller
    service, runner, _a, _s, _m, _i = world
    mid = _imported(world)
    cmds = MeetingCommands(lambda: service, lambda: settings_from_mapping({}), capture=lambda: None)
    out = cmds.handle(f"reprocess {mid}", Caller("discord", "1", "1"), "meeting")
    assert "regenerating the notes" in out and "without audio" in out.lower()


def test_cli_reprocess_explains_the_mapping(world, capsys):
    from meeting_scribe import cli
    from .test_cli import FakeRuntime, parse
    service, runner, _a, _s, _m, _i = world
    mid = _imported(world)
    code = cli.dispatch(parse(["reprocess", mid, "--now"]), FakeRuntime(service, {}))
    out = capsys.readouterr().out
    assert code == 0 and "without audio" in out.lower() and "done" in out


def test_a_legacy_transcribe_job_on_an_imported_meeting_runs_analyze(world):
    service, runner, analyzer, _s, _m, _i = world
    mid = _imported(world)
    m = service.repo.get_meeting(mid)
    service.repo.save_meeting(replace(m, state=MeetingState.CAPTURED))  # what the old reprocess left behind
    service.repo.enqueue_job(mid, Stage.TRANSCRIBE, now=service.clock.now(), reset_attempts=True)
    drain(runner)
    assert service.repo.get_meeting(mid).state is MeetingState.DONE and analyzer.calls == 2
