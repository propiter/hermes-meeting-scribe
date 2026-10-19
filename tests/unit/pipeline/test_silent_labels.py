"""A silent "unidentified participant" track is not a participant (DESIGN §4.1).

A Discord connection that only streams silence still gets its own track, because the receiver cannot
know it will never speak. Keeping its label in the participant list repeats a person who already has
a track of their own and invents a participant who never said a word — measured on a real meeting:
eight such tracks, every one at -91 dB, listed right after the eight people who talked.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from meeting_scribe.domain.models import MeetingState, Speaker, Utterance
from meeting_scribe.pipeline.service import MeetingService
from meeting_scribe.pipeline.stages import spoken_speakers
from meeting_scribe.storage.artifacts import read_meta

from .test_runner import build, drain

SILENT = "unidentified-1"   # never produced a line, and the receiver named it after its owner
SILENT2 = "unidentified-2"  # never produced a line either: still "Participante sin identificar 2"
HEARD = "unidentified-3"    # a label that DID speak: it is a real person waiting for a name


class SilentLabelsTranscriber:
    """Two people talk; the tracks of the silent connections produce no utterances at all."""

    def transcribe(self, meeting, folder, progress=None):
        return [Utterance(0.0, 2.0, "10", "Ana", "Hola equipo"),
                Utterance(2.5, 4.0, "11", "Luis", "Yo envío el informe"),
                Utterance(5.0, 6.0, HEARD, "Participante sin identificar 3", "Y reviso el contrato")]


@pytest.fixture
def world(prepo, layout, settings, clock, meeting):
    runner, _, _, _ = build(prepo, layout, settings, clock, transcriber=SilentLabelsTranscriber())
    service = MeetingService(prepo, layout, runner, settings, clock=clock, item_sinks=lambda: {},
                             catalogs=lambda: runner.stages.catalogs())
    live = service.begin_recording(replace(meeting, state=MeetingState.RECORDING, ended_at=None,
                                           speakers=(Speaker("10", "Ana"), Speaker("11", "Luis"),
                                                     Speaker(SILENT, "Juan David"),
                                                     Speaker(SILENT2, "Participante sin identificar 2"),
                                                     Speaker(HEARD, "Participante sin identificar 3"))))
    service.finish_recording(live.id)
    drain(runner)
    return service, live.id


def test_silent_labels_are_not_participants(world):
    service, mid = world
    meeting = service.require(mid)
    assert {s.user_id for s in meeting.speakers} == {"10", "11", HEARD}
    assert meeting.state is MeetingState.DONE


def test_silent_labels_leave_the_meta_and_the_index(world):
    service, mid = world
    folder = service.folder(service.require(mid))
    assert {s.user_id for s in read_meta(folder).speakers} == {"10", "11", HEARD}
    rows = service.repo._x("SELECT user_id FROM speakers WHERE meeting_id=?", (mid,)).fetchall()
    assert {r["user_id"] for r in rows} == {"10", "11", HEARD}


def test_a_label_that_spoke_keeps_its_lines_and_stays_assignable(world):
    service, mid = world
    [track] = service.speaker_tracks(service.require(mid))
    assert (track.label, track.lines) == (HEARD, 1)


def test_a_label_relabelled_to_its_owner_is_not_listed_twice():
    """Its audio went to the owner's own track, so the owner is already a participant."""
    speakers = (Speaker("10", "Ana"), Speaker(SILENT, "Ana"))
    kept = spoken_speakers(speakers, (Utterance(0.0, 2.0, "10", "Ana", "Hola"),))
    assert [s.user_id for s in kept] == ["10"]


def test_a_person_without_audio_keeps_being_a_participant():
    """The rule drops labels only: someone who stayed silent (or was never heard) is still a person
    the notes may assign a task to."""
    speakers = (Speaker("10", "Ana"), Speaker("11", "Luis"), Speaker(SILENT, "Juan David"))
    kept = spoken_speakers(speakers, (Utterance(0.0, 2.0, "10", "Ana", "Hola"),))
    assert [s.user_id for s in kept] == ["10", "11"]
