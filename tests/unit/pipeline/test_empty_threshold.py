"""A connection is not a meeting; a room that stayed is not a connection.

Measured over 52 published meetings: the ones with nothing in them held 0.7-29.6 s of speech and the
shortest real meeting 67.7 s — a 38-second gap, with the floor at 45 s in the middle of it. The
room's own duration tells the two apart, and it is what keeps the rule honest: a LONG room is never
discarded for being quiet, because a room that stayed and said nothing is a broken capture, and
discarding it would hide that.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.domain.models import MeetingState, Speaker, Utterance
from meeting_scribe.pipeline.service import MeetingService

from .test_runner import build, drain


def _transcriber(speech_seconds: float):
    class T:
        def transcribe(self, meeting, folder, progress=None):
            return [Utterance(0.0, speech_seconds, "10", "Ana", "hola")] if speech_seconds else []
    return T


@pytest.fixture
def run(prepo, layout, settings, clock, meeting):
    """Run one meeting end to end, choosing how much was said and how long the room was open."""
    def go(*, speech: float, minutes: float, cfg=None):
        use = cfg or settings
        runner, _, _, _ = build(prepo, layout, use, clock, transcriber=_transcriber(speech))
        service = MeetingService(prepo, layout, runner, use, clock=clock, item_sinks=lambda: {},
                                 catalogs=lambda: runner.stages.catalogs())
        live = service.begin_recording(replace(
            meeting, state=MeetingState.RECORDING,
            ended_at=meeting.started_at + timedelta(minutes=minutes),
            speakers=(Speaker("10", "Ana"),)))
        service.finish_recording(live.id)
        drain(runner)
        return service.require(live.id)
    return go


def test_a_connection_with_a_few_seconds_of_speech_is_not_a_meeting(run):
    """Measured: "no", "Pero esa cara...", "me conecto ya" — 6.5 s in a 15-second connection."""
    assert run(speech=6.5, minutes=0.25).state is MeetingState.EMPTY


def test_a_long_room_is_never_discarded_for_being_quiet(run):
    """Measured: 37 minutes on the clock, 9.4 s transcribed — a broken capture, not an empty room.
    Discarding it would delete the only evidence that the audio was lost."""
    assert run(speech=9.4, minutes=37).state is MeetingState.DONE


def test_a_real_meeting_is_untouched(run):
    """Measured: the shortest real meeting on record holds 67.7 s of speech."""
    assert run(speech=67.7, minutes=30).state is MeetingState.DONE


def test_the_floor_can_be_turned_off(run):
    cfg = lambda space=None: settings_from_mapping(  # noqa: E731 - a settings(space) callable
        {"audio_retention": "none", "min_speech_seconds": 0})
    assert run(speech=6.5, minutes=0.25, cfg=cfg).state is MeetingState.DONE


def test_nothing_at_all_is_still_discarded_with_the_floor_off(run):
    """No utterance keeps the old outcome whatever the floor says: there is nothing to analyse."""
    cfg = lambda space=None: settings_from_mapping(  # noqa: E731
        {"audio_retention": "none", "min_speech_seconds": 0})
    assert run(speech=0.0, minutes=0.25, cfg=cfg).state is MeetingState.EMPTY
