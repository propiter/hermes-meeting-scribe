"""Giving an "unidentified participant" track to its owner after the meeting (DESIGN §4.1)."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from meeting_scribe.domain.models import ActionItem, MeetingState, Notes, Speaker, Stage, Utterance
from meeting_scribe.pipeline.service import MeetingService
from meeting_scribe.pipeline.speakers import AssignError, assignments
from meeting_scribe.storage.artifacts import read_meta, read_notes, read_transcript

from .test_cli import FakeRuntime, run
from .test_runner import build, drain

LABEL = "unidentified-1"


class TrackTranscriber:
    """What the transcriber returns for tracks named ``10.ogg`` and ``unidentified-1.ogg``."""

    def __init__(self):
        self.calls = 0

    def transcribe(self, meeting, folder, progress=None):
        self.calls += 1
        return [Utterance(0.0, 2.0, "10", "Ana", "Hola equipo"),
                Utterance(2.5, 4.0, LABEL, "Participante sin identificar", "Yo envío el informe"),
                Utterance(64.0, 70.0, LABEL, "Participante sin identificar", "Y reviso el contrato")]


class Analyzer:
    def analyze(self, meeting, utterances, candidates):
        return Notes(meeting_title="Informe semanal", tldr="t", summary="s", language="es",
                     action_items=(ActionItem(id="a1", title="Enviar informe", owner_speaker_id=LABEL,
                                              owner_name="Participante sin identificar"),
                                   ActionItem(id="a2", title="Saludar", owner_speaker_id="10", owner_name="Ana")))


@pytest.fixture
def world(prepo, layout, settings, clock, meeting):
    runner, _, _, sinks = build(prepo, layout, settings, clock, transcriber=TrackTranscriber())
    runner.stages.analyzer = Analyzer()
    sinks[0].republish = sinks[0].deliver  # this fake models an editable publication sink
    service = MeetingService(prepo, layout, runner, settings, clock=clock, item_sinks=lambda: {},
                             catalogs=lambda: runner.stages.catalogs())
    live = service.begin_recording(replace(meeting, state=MeetingState.RECORDING, ended_at=None,
                                           speakers=(Speaker("10", "Ana"), Speaker("11", "Luis"),
                                                     Speaker(LABEL, "Participante sin identificar"))))
    service.finish_recording(live.id, missing_audio=("11",))
    drain(runner)
    return service, runner, sinks[0], live.id


def test_assignment_refresh_never_arms_dm_move_or_runs_external_sinks(world):
    from meeting_scribe.domain.models import KV_MOVE_FROM_DM
    from .conftest import RecordingSink

    service, runner, publication, mid = world
    external = RecordingSink(name="external")
    runner.stages.sinks = lambda: [publication, external]
    service.assign_speaker(mid, LABEL, "11", actor="11")
    assert service.repo.kv_get(KV_MOVE_FROM_DM + mid) is None
    drain(runner)
    assert len(publication.calls) == 2 and external.calls == []
    assert service.repo.kv_get(KV_MOVE_FROM_DM + mid) is None


def test_correction_after_retranscription_keeps_original_track(world):
    service, runner, _, mid = world
    service.assign_speaker(mid, LABEL, "11", actor="11")
    drain(runner)
    service.reprocess(mid, Stage.TRANSCRIBE)
    drain(runner)
    service.assign_speaker(mid, LABEL, "10", actor="cli", admin=True)
    drain(runner)
    assert [u.track_id for u in read_transcript(service.folder(service.require(mid)))] == [None, LABEL, LABEL]


def test_reanalysis_retains_task_provenance_for_later_corrections(world):
    service, runner, _, mid = world
    service.assign_speaker(mid, LABEL, "11", actor="11")
    drain(runner)
    runner.stages.analyzer.analyze = lambda *args: Notes(
        meeting_title="Informe semanal", tldr="t", summary="s", language="es",
        action_items=(ActionItem(id="a1", title="Enviar informe", owner_speaker_id="11", owner_name="Luis"),))
    service.reprocess(mid, Stage.ANALYZE)
    drain(runner)
    service.assign_speaker(mid, LABEL, "10", admin=True)
    assert service.repo.get_action_item(mid, "a1").owner_speaker_id == "10"


def test_tracks_show_interval_and_lines(world):
    service, _, _, mid = world
    [track] = service.speaker_tracks(service.require(mid))
    assert (track.label, track.lines, track.first, track.last, track.owner) == (LABEL, 2, 2.5, 70.0, None)


def test_assign_renames_transcript_tasks_speakers_and_redelivers_in_place(world):
    service, runner, sink, mid = world
    assert service.require(mid).state is MeetingState.DONE and len(sink.calls) == 1
    done = service.assign_speaker(mid, LABEL, "Luis", actor="11")
    assert (done.user_id, done.lines, done.tasks, done.changed, done.redeliver) == ("11", 2, 1, True, True)
    meeting = service.require(mid)
    folder = service.folder(meeting)
    assert {u.speaker_id for u in read_transcript(folder)} == {"10", "11"}
    assert "**[00:02] Luis:** Yo envío el informe" in (folder / "transcript.md").read_text(encoding="utf-8")
    assert [(r["speaker_id"], r["speaker"]) for r in service.repo.search("contrato", "main")] == [("11", "Luis")]
    notes = read_notes(folder)
    assert [(a.owner_speaker_id, a.owner_name) for a in notes.action_items] == [("11", "Luis"), ("10", "Ana")]
    assert service.repo.get_action_item(mid, "a1").owner_speaker_id == "11"
    assert LABEL not in {s.user_id for s in meeting.speakers} and meeting.missing_audio == ()
    assert LABEL not in {s.user_id for s in read_meta(folder).speakers}
    rows = service.repo._x("SELECT user_id FROM speakers WHERE meeting_id=?", (mid,)).fetchall()
    assert LABEL not in {r["user_id"] for r in rows}
    assert service.repo.get_job(mid).stage == Stage.DELIVER.value
    drain(runner)
    assert len(sink.calls) == 2  # re-delivered: sinks edit what they published
    [track] = service.speaker_tracks(meeting)
    assert (track.owner, track.name, track.lines, track.first) == ("11", "Luis", 2, 2.5)


def test_owner_can_correct_then_undo_without_touching_other_lines_or_tasks(world):
    service, runner, sink, mid = world
    service.assign_speaker(mid, LABEL, "11", actor="11")
    drain(runner)
    service.assign_speaker(mid, LABEL, "10", actor="owner:1", admin=True)
    drain(runner)
    folder = service.folder(service.require(mid))
    assert [u.speaker_id for u in read_transcript(folder)] == ["10", "10", "10"]
    assert [u.track_id for u in read_transcript(folder)] == [None, LABEL, LABEL]
    service.assign_speaker(mid, LABEL, "unassigned", actor="desktop", admin=True)
    drain(runner)
    assert [u.speaker_id for u in read_transcript(folder)] == ["10", LABEL, LABEL]
    assert service.repo.get_action_item(mid, "a1").owner_speaker_id == LABEL
    assert service.repo.get_action_item(mid, "a2").owner_speaker_id == "10"
    assert assignments(service.repo, mid) == {}
    history = service.repo.speaker_history(mid)
    assert [(r["actor"], r["previous_user"], r["next_user"]) for r in history] == [
        ("11", None, "11"), ("owner:1", "11", "10"), ("desktop", "10", None)]
    assert all(r["at"] for r in history)
    with pytest.raises(AssignError, match="owner_required"):
        service.assign_speaker(mid, LABEL, "11", actor="10")


def test_assign_is_idempotent_and_never_moves_a_track_twice(world):
    service, runner, sink, mid = world
    service.assign_speaker(mid, LABEL, "@11", actor="11")
    drain(runner)
    again = service.assign_speaker(mid, LABEL, "11", actor="11")
    assert not again.changed and not again.redeliver and len(sink.calls) == 2
    with pytest.raises(AssignError) as err:
        service.assign_speaker(mid, LABEL, "Ana")
    assert err.value.code == "owner_required"


@pytest.mark.parametrize(("label", "who", "code"), [
    (LABEL, "Nadie", "unknown_person"), ("10", "Luis", "not_unidentified"), ("unidentified-7", "Luis", "unknown_track"),
    (LABEL, LABEL, "unknown_person")])
def test_assign_refuses_what_is_not_a_track_or_a_participant(world, label, who, code):
    service, _, _, mid = world
    with pytest.raises(AssignError) as err:
        service.assign_speaker(mid, label, who)
    assert err.value.code == code
    assert assignments(service.repo, mid) == {}


def test_a_reprocess_from_transcribe_keeps_the_assignment(world):
    service, runner, _, mid = world
    service.assign_speaker(mid, LABEL, "<@11>", actor="11")
    drain(runner)
    service.reprocess(mid, Stage.TRANSCRIBE)
    drain(runner)
    utts = read_transcript(service.folder(service.require(mid)))
    assert [(u.speaker_id, u.speaker) for u in utts][1:] == [("11", "Luis"), ("11", "Luis")]


def test_cli_speaker_list_and_assign(world, capsys):
    service, runner, _, mid = world
    rt = FakeRuntime(service, {"ui_language": "es"})
    code, out = run(rt, ["speaker", "list", mid], capsys)
    assert code == 0 and "una persona" in out and "unidentified-1: 2 línea(s), 00:02–01:10" in out
    code, out = run(rt, ["speaker", "assign", mid, LABEL, "Nadie"], capsys)
    assert code == 2 and "no es uno de los participantes" in out
    code, out = run(rt, ["speaker", "assign", mid, LABEL, "@11"], capsys)
    assert code == 0 and "unidentified-1 ahora es Luis: se movieron 2 línea(s)" in out and "en su sitio" in out
    code, out = run(rt, ["speaker", "assign", mid, LABEL, "Luis"], capsys)
    assert code == 0 and "ya estaba asignada" in out
    code, out = run(rt, ["speaker", "list", mid, "--json"], capsys)
    assert json.loads(out)[0]["owner"] == "11"


def test_a_participant_can_only_claim_a_voice_as_their_own(world):
    service, runner, _, mid = world
    with pytest.raises(AssignError) as err:
        service.assign_speaker(mid, LABEL, "11", actor="10")  # Ana says the voice was Luis
    assert err.value.code == "self_only"
    assert service.repo.speaker_history(mid) == [] and assignments(service.repo, mid) == {}
    done = service.assign_speaker(mid, LABEL, "10", actor="10")  # "that voice is me"
    assert done.user_id == "10" and done.changed


def test_an_operator_may_give_a_voice_to_someone_else(world):
    service, _, _, mid = world
    done = service.assign_speaker(mid, LABEL, "11", actor="owner:1", admin=True)
    assert done.user_id == "11"
    assert [(r["actor"], r["next_user"]) for r in service.repo.speaker_history(mid)] == [("owner:1", "11")]
