from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.domain.models import Candidate, Notes, SinkResult, Utterance, ActionItem
from meeting_scribe.storage.layout import Layout
from meeting_scribe.storage.repo import Repository


class FakeClock:
    def __init__(self):
        self.t = datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc)

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class FakeTranscriber:
    def __init__(self, fail_times=0):
        self.calls = 0
        self.fail_times = fail_times

    def transcribe(self, meeting, folder, progress=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("worker crashed")
        return [Utterance(0.0, 2.0, "10", "Ana", "Hola equipo"),
                Utterance(2.5, 4.0, "11", "Luis", "Yo envío el informe")]


class FakeAnalyzer:
    def __init__(self):
        self.calls = 0
        self.seen_candidates = None

    def analyze(self, meeting, utterances, candidates):
        self.calls += 1
        self.seen_candidates = list(candidates)
        return Notes(meeting_title="Informe semanal", tldr="t", summary="s", language="es",
                     project="Website", project_confidence=0.9,
                     action_items=(ActionItem(id="a1", title="Enviar informe", owner_speaker_id="11",
                                              owner_name="Luis", project="Website", project_confidence=0.9),))


@dataclass
class RecordingSink:
    name: str = "rec"
    fail_times: int = 0
    calls: list = field(default_factory=list)
    on: bool = True

    def enabled(self):
        return self.on

    def deliver(self, meeting, notes, folder):
        self.calls.append(meeting.id)
        if len(self.calls) <= self.fail_times:
            return SinkResult(self.name, False, errors=("503 upstream",))
        return SinkResult(self.name, True, ("x",))


class Catalog:
    name = "hermes"

    def candidates(self, meeting):
        return [Candidate("hermes:p1", "Website", "hermes", {"project_id": "p1"})]


class SyncSpawner:
    """Returns an unstarted thread like spawn_context_thread; records the name."""

    def __init__(self):
        self.names = []

    def __call__(self, target, *, name, daemon=True):
        self.names.append(name)
        return threading.Thread(target=target, name=name, daemon=daemon)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def layout(tmp_path):
    return Layout(lambda: tmp_path / "data")


@pytest.fixture
def prepo(layout):
    r = Repository(layout.db_path())
    yield r
    r.close()


@pytest.fixture
def settings():
    s = settings_from_mapping({"audio_retention": "none"})
    return lambda: s
