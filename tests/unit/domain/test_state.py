"""Meeting state machine: the durable pipeline relies on these transitions being exhaustive."""
import pytest

from meeting_scribe.domain.models import (
    STAGE_ORDER, InvalidTransition, MeetingState, Stage, rewind_target, stage_after, transition,
)


HAPPY_PATH = [
    MeetingState.RECORDING, MeetingState.CAPTURED, MeetingState.TRANSCRIBING, MeetingState.TRANSCRIBED,
    MeetingState.ANALYZING, MeetingState.ANALYZED, MeetingState.DELIVERING, MeetingState.DONE,
]


def test_happy_path_is_allowed():
    state = HAPPY_PATH[0]
    for nxt in HAPPY_PATH[1:]:
        state = transition(state, nxt)
    assert state is MeetingState.DONE


@pytest.mark.parametrize("state", [s for s in MeetingState if s not in (MeetingState.DONE, MeetingState.FAILED)])
def test_every_active_state_can_fail(state):
    assert transition(state, MeetingState.FAILED) is MeetingState.FAILED


@pytest.mark.parametrize("src,dst", [
    (MeetingState.RECORDING, MeetingState.DONE),
    (MeetingState.CAPTURED, MeetingState.ANALYZING),
    (MeetingState.DONE, MeetingState.DELIVERING),
    (MeetingState.FAILED, MeetingState.DONE),
])
def test_invalid_transitions_raise(src, dst):
    with pytest.raises(InvalidTransition):
        transition(src, dst)


def test_rewind_for_reprocess_or_resume():
    # A crash mid-stage leaves "transcribing": resume rewinds to the stage input state.
    assert rewind_target(Stage.TRANSCRIBE) is MeetingState.CAPTURED
    assert rewind_target(Stage.ANALYZE) is MeetingState.TRANSCRIBED
    assert rewind_target(Stage.DELIVER) is MeetingState.ANALYZED
    for src in (MeetingState.DONE, MeetingState.FAILED, MeetingState.ANALYZING):
        assert transition(src, MeetingState.CAPTURED, rewind=True) is MeetingState.CAPTURED
    # Only stage-input states are rewind targets; a live recording is never rewound.
    with pytest.raises(InvalidTransition):
        transition(MeetingState.DONE, MeetingState.ANALYZING, rewind=True)
    with pytest.raises(InvalidTransition):
        transition(MeetingState.RECORDING, MeetingState.CAPTURED, rewind=True)


def test_rewind_never_goes_forward():
    with pytest.raises(InvalidTransition):
        transition(MeetingState.CAPTURED, MeetingState.ANALYZED, rewind=True)


def test_stage_order_and_next_stage():
    assert STAGE_ORDER == (Stage.TRANSCRIBE, Stage.ANALYZE, Stage.DELIVER, Stage.ARCHIVE)
    assert stage_after(MeetingState.CAPTURED) is Stage.TRANSCRIBE
    assert stage_after(MeetingState.TRANSCRIBED) is Stage.ANALYZE
    assert stage_after(MeetingState.ANALYZED) is Stage.DELIVER
    assert stage_after(MeetingState.DONE) is None
    assert Stage.parse("transcribe") is Stage.TRANSCRIBE
    with pytest.raises(ValueError):
        Stage.parse("nope")
