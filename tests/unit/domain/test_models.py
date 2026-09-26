from datetime import datetime, timezone

import pytest

from meeting_scribe.domain.models import (
    ActionItem, ActionStatus, Meeting, MeetingState, Notes, Speaker, Topic, Utterance, Word,
)


def _meeting(**kw):
    base = dict(id="abc12345", guild_id="1", channel_id="2", channel_name="General",
                started_at=datetime(2026, 9, 26, 15, 4, tzinfo=timezone.utc))
    base.update(kw)
    return Meeting(**base)


def test_meeting_defaults_and_immutability():
    m = _meeting()
    assert m.state is MeetingState.RECORDING
    assert m.speakers == () and m.partial is False
    with pytest.raises(Exception):
        m.state = MeetingState.DONE  # type: ignore[misc]


def test_meeting_requires_aware_datetime():
    with pytest.raises(ValueError):
        _meeting(started_at=datetime(2026, 1, 1))


def test_with_state_validates_transition():
    m = _meeting().with_state(MeetingState.CAPTURED)
    assert m.state is MeetingState.CAPTURED
    with pytest.raises(Exception):
        m.with_state(MeetingState.DONE)


def test_meeting_roundtrip_dict():
    m = _meeting(speakers=(Speaker("10", "Ana"), Speaker("11", "Bot", is_bot=True)), title="Sync")
    again = Meeting.from_dict(m.to_dict())
    assert again == m
    assert m.human_speakers == (Speaker("10", "Ana"),)


def test_utterance_roundtrip_and_validation():
    u = Utterance(t0=1.0, t1=2.5, speaker_id="10", speaker="Ana", text="hola",
                  words=(Word(1.0, 1.4, "hola", 0.9),), confidence=-0.2)
    assert Utterance.from_dict(u.to_dict()) == u
    assert "words" not in Utterance(0, 1, "1", "A", "x").to_dict()
    with pytest.raises(ValueError):
        Utterance(t0=2.0, t1=1.0, speaker_id="1", speaker="A", text="x")


def test_action_item_status_and_clamping():
    a = ActionItem(id="a1", title="Send SMTP creds", project_confidence=1.7)
    assert a.status is ActionStatus.PENDING
    assert a.project_confidence == 1.0
    assert a.with_status(ActionStatus.APPROVED).status is ActionStatus.APPROVED
    assert ActionItem.from_dict(a.to_dict()) == a


def test_notes_roundtrip():
    n = Notes(meeting_title="Weekly", tldr="t", summary="s",
              topics=(Topic("SMTP", ("migrate",)),), decisions=("Use SES",),
              open_questions=("Budget?",), action_items=(ActionItem(id="a1", title="Do"),),
              language="es")
    assert Notes.from_dict(n.to_dict()) == n
