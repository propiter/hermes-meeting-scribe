from __future__ import annotations

from datetime import datetime, timezone

import pytest

from meeting_scribe.domain.models import (
    ActionItem, Meeting, MeetingState, Notes, Speaker, Topic, Utterance,
)


@pytest.fixture
def meeting() -> Meeting:
    return Meeting(id="k3v7q2ab", guild_id="100", channel_id="200", channel_name="Daily Sync",
                   started_at=datetime(2026, 9, 26, 15, 4, tzinfo=timezone.utc),
                   ended_at=datetime(2026, 9, 26, 15, 34, tzinfo=timezone.utc),
                   state=MeetingState.CAPTURED, title="Daily Sync", guild_name="Acme",
                   category_name="Engineering",
                   speakers=(Speaker("10", "Ana"), Speaker("11", "Luis")))


@pytest.fixture
def utterances() -> list[Utterance]:
    return [
        Utterance(0.0, 3.0, "10", "Ana", "Hola, revisemos la migración SMTP."),
        Utterance(3.5, 7.0, "11", "Luis", "Yo envío las credenciales el viernes."),
        Utterance(8.0, 9.0, "10", "Ana", "Perfecto, decidimos usar SES."),
    ]


@pytest.fixture
def notes() -> Notes:
    return Notes(meeting_title="Migración SMTP", tldr="Migrar a SES.", summary="Se revisó la migración.",
                 topics=(Topic("SMTP", ("Migrar a SES",)),), decisions=("Usar SES",),
                 open_questions=("¿Presupuesto?",),
                 action_items=(ActionItem(id="a0000000001", title="Enviar credenciales", owner_speaker_id="11",
                                          owner_name="Luis", due="2026-10-02", quote="Yo envío las credenciales",
                                          t0=3.5),
                               ActionItem(id="a0000000002", title="Revisar costos")),
                 language="es")
