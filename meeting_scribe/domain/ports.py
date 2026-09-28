"""Ports (hexagonal boundaries). Adapters live outside ``domain``; tests substitute fakes.

Each Protocol is the *narrowest* surface the core needs, so Hermes/Discord/Linear specifics
never leak into pipeline logic.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence, runtime_checkable

from .models import Candidate, Meeting, MeetingState, Notes, SinkResult, Speaker, Utterance


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


@runtime_checkable
class Transcriber(Protocol):
    """Turns per-speaker tracks of a meeting folder into ordered utterances."""

    def transcribe(self, meeting: Meeting, folder: Path,
                   progress: Optional[Callable[[str, float], None]] = None) -> list[Utterance]: ...


@runtime_checkable
class StructuredLLM(Protocol):
    """Subset of ``ctx.llm`` the analyzer uses; returns the parsed JSON object."""

    def complete_json(self, *, instructions: str, text: str, json_schema: Mapping[str, Any],
                      schema_name: str) -> Mapping[str, Any]: ...


@runtime_checkable
class Analyzer(Protocol):
    def analyze(self, meeting: Meeting, utterances: Sequence[Utterance],
                candidates: Sequence[Candidate]) -> Notes: ...


@runtime_checkable
class ProjectCatalog(Protocol):
    """One source of project candidates (Hermes projects, kanban boards, Linear, learned map)."""

    name: str

    def candidates(self, meeting: Meeting) -> list[Candidate]: ...


@runtime_checkable
class Sink(Protocol):
    """Delivery target. MUST be idempotent: keys are ``mtg:<meeting_id>:<item_id>``."""

    name: str

    def enabled(self, meeting: Meeting) -> bool: ...

    def deliver(self, meeting: Meeting, notes: Notes, folder: Path) -> SinkResult: ...


@runtime_checkable
class MeetingRepository(Protocol):
    def save_meeting(self, meeting: Meeting) -> None: ...

    def get_meeting(self, meeting_id: str) -> Optional[Meeting]: ...

    def list_meetings(self, limit: int = 20, states: Optional[Iterable[MeetingState]] = None
                      ) -> list[Meeting]: ...

    def upsert_speakers(self, meeting_id: str, speakers: Sequence[Speaker]) -> None: ...

    def replace_utterances(self, meeting_id: str, utterances: Sequence[Utterance]) -> None: ...

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]: ...
