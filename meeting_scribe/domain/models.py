"""Domain models and the meeting state machine (DESIGN §9).

Models are frozen so pipeline stages cannot mutate shared state behind the repository's back;
every change goes through ``dataclasses.replace`` and is persisted explicitly.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional

from .text import is_ascii_digits


class InvalidTransition(ValueError):
    """Raised when a state change is not allowed by the meeting state machine."""


class MeetingState(str, Enum):
    RECORDING = "recording"
    CAPTURED = "captured"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    ANALYZING = "analyzing"
    ANALYZED = "analyzed"
    DELIVERING = "delivering"
    DONE = "done"
    FAILED = "failed"
    # Nobody's voice was captured (a person joined and left without speaking, or every track was
    # silence the transcriber drops): nothing to write notes about. Terminal, not an error.
    EMPTY = "empty"

    @property
    def terminal(self) -> bool:
        return self in (MeetingState.DONE, MeetingState.FAILED, MeetingState.EMPTY)


class Stage(str, Enum):
    """Pipeline stage names used by ``reprocess from=<stage>`` and ``failed_stage``."""

    TRANSCRIBE = "transcribe"
    ANALYZE = "analyze"
    DELIVER = "deliver"
    ARCHIVE = "archive"

    @classmethod
    def parse(cls, value: str) -> "Stage":
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(f"unknown stage {value!r}; expected one of {[s.value for s in cls]}") from exc


STAGE_ORDER: tuple[Stage, ...] = (Stage.TRANSCRIBE, Stage.ANALYZE, Stage.DELIVER, Stage.ARCHIVE)

_S = MeetingState
_FORWARD: dict[MeetingState, frozenset[MeetingState]] = {
    _S.RECORDING: frozenset({_S.CAPTURED}),
    _S.CAPTURED: frozenset({_S.TRANSCRIBING}),
    _S.TRANSCRIBING: frozenset({_S.TRANSCRIBED}),
    _S.TRANSCRIBED: frozenset({_S.ANALYZING}),
    _S.ANALYZING: frozenset({_S.ANALYZED}),
    _S.ANALYZED: frozenset({_S.DELIVERING}),
    # DELIVERING -> DONE happens after deliver+archive; archive has no state of its own because
    # it is a local, idempotent file operation re-run safely on resume.
    _S.DELIVERING: frozenset({_S.DONE}),
    _S.DONE: frozenset(),
    _S.FAILED: frozenset(),
    _S.EMPTY: frozenset(),
}
# Where a recording may turn out to hold no voice: before any notes exist (never once analyzed).
_CAN_BE_EMPTY = frozenset({_S.RECORDING, _S.CAPTURED, _S.TRANSCRIBING, _S.TRANSCRIBED, _S.ANALYZING})
_ORDER = [_S.RECORDING, _S.CAPTURED, _S.TRANSCRIBING, _S.TRANSCRIBED, _S.ANALYZING, _S.ANALYZED,
          _S.DELIVERING, _S.DONE]
_REWIND_TARGETS = frozenset({_S.CAPTURED, _S.TRANSCRIBED, _S.ANALYZED})
_STAGE_INPUT: dict[Stage, MeetingState] = {
    Stage.TRANSCRIBE: _S.CAPTURED, Stage.ANALYZE: _S.TRANSCRIBED,
    Stage.DELIVER: _S.ANALYZED, Stage.ARCHIVE: _S.ANALYZED,
}
_RUNNING: dict[Stage, MeetingState] = {
    Stage.TRANSCRIBE: _S.TRANSCRIBING, Stage.ANALYZE: _S.ANALYZING, Stage.DELIVER: _S.DELIVERING,
}


def transition(current: MeetingState, target: MeetingState, *, rewind: bool = False) -> MeetingState:
    """Validate ``current -> target``.

    ``rewind=True`` is the reprocess/resume path: jump back to a stage *input* state from any
    later (or failed) state. It never moves forward and never touches a live recording.
    """
    if target is _S.FAILED and not current.terminal:
        return target
    if target is _S.EMPTY and current in _CAN_BE_EMPTY:
        return target
    if rewind:
        if target not in _REWIND_TARGETS or current in (_S.RECORDING, _S.EMPTY):
            raise InvalidTransition(f"cannot rewind {current.value} -> {target.value}")
        if current is _S.FAILED or _ORDER.index(current) >= _ORDER.index(target):
            return target
        raise InvalidTransition(f"rewind must go backwards: {current.value} -> {target.value}")
    if target in _FORWARD[current]:
        return target
    raise InvalidTransition(f"invalid transition {current.value} -> {target.value}")


def rewind_target(stage: Stage) -> MeetingState:
    """State a meeting must be in before ``stage`` runs."""
    return _STAGE_INPUT[stage]


def running_state(stage: Stage) -> Optional[MeetingState]:
    """In-progress state for ``stage`` (archive has none)."""
    return _RUNNING.get(stage)


def stage_after(state: MeetingState) -> Optional[Stage]:
    """Next stage to run for a meeting at rest in ``state`` (None when nothing is pending)."""
    return {_S.CAPTURED: Stage.TRANSCRIBE, _S.TRANSCRIBED: Stage.ANALYZE,
            _S.ANALYZED: Stage.DELIVER}.get(state)


# --------------------------------------------------------------------------------------------
SOURCE_DISCORD = "discord"
SOURCE_GOOGLE_MEET = "google_meet"
# Notes an older version posted in a DM (DESIGN §19). ``KV_MOVE_FROM_DM + id``: set by
# ``reprocess <id> --from deliver`` — the only thing that may move them to a server channel.
# ``KV_DM_NOTES + id``: why they are still in a DM and the exact commands to move them.
KV_MOVE_FROM_DM = "discord.move_from_dm."
KV_DM_NOTES = "discord.dm_notes."


UNIDENTIFIED_PREFIX = "unidentified-"  # the speaker id of an "unidentified participant" track (DESIGN §4.1)


def is_unidentified(user_id: Optional[str]) -> bool:
    """A track whose owner was never proven: one SSRC, i.e. one Discord connection, i.e. one person."""
    rest = str(user_id or "")[len(UNIDENTIFIED_PREFIX):]
    return str(user_id or "").startswith(UNIDENTIFIED_PREFIX) and is_ascii_digits(rest)


def is_discord_user_id(user_id: Optional[str]) -> bool:
    """Discord snowflakes are digits; imported speakers (``gmeet:<id>``) are not mentionable/DMable."""
    return bool(user_id) and is_ascii_digits(str(user_id))


@dataclass(frozen=True)
class Speaker:
    user_id: str
    name: str
    is_bot: bool = False
    # A Google Meet attendee signed in to Google: their Google user id as the Meet API gives it
    # (``signedinUser.user``, ``users/<id>``). The only identity of an imported speaker that can be
    # linked to a Discord member (DESIGN §19.3); the display name is typed by the attendee.
    google_user: str = ""
    # Other names the person goes by on the platform (Discord username, global name, server nickname
    # — whichever differ from ``name``): a task owner said as "Sebas" still finds "Sebastián Ortega".
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Word:
    t0: float
    t1: float
    text: str
    p: float = 1.0


@dataclass(frozen=True)
class Utterance:
    """One transcript line; times are seconds since meeting t0 (DESIGN §6)."""

    t0: float
    t1: float
    speaker_id: str
    speaker: str
    text: str
    words: tuple[Word, ...] = ()
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if self.t1 < self.t0:
            raise ValueError(f"utterance ends before it starts ({self.t0} > {self.t1})")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"t0": round(self.t0, 3), "t1": round(self.t1, 3), "speaker_id": self.speaker_id,
                             "speaker": self.speaker, "text": self.text,
                             "confidence": round(self.confidence, 4)}
        if self.words:
            d["words"] = [asdict(w) for w in self.words]
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Utterance":
        return cls(t0=float(d["t0"]), t1=float(d["t1"]), speaker_id=str(d["speaker_id"]),
                   speaker=str(d.get("speaker") or d["speaker_id"]), text=str(d["text"]),
                   words=tuple(Word(float(w["t0"]), float(w["t1"]), str(w["text"]), float(w.get("p", 1.0)))
                               for w in d.get("words") or ()),
                   confidence=float(d.get("confidence", 0.0)))


class ActionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DISMISSED = "dismissed"
    DELIVERED = "delivered"


@dataclass(frozen=True)
class ActionItem:
    id: str
    title: str
    description: str = ""
    owner_speaker_id: Optional[str] = None
    owner_name: Optional[str] = None
    due: Optional[str] = None
    project: Optional[str] = None
    project_confidence: float = 0.0
    quote: str = ""
    t0: Optional[float] = None
    status: ActionStatus = ActionStatus.PENDING
    project_key: Optional[str] = None   # resolved candidate key (DESIGN §16: per-task routing)
    project_hint: Optional[str] = None  # the name as spoken when it is not a known candidate

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_confidence", min(1.0, max(0.0, float(self.project_confidence))))

    def with_status(self, status: ActionStatus) -> "ActionItem":
        return replace(self, status=status)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ActionItem":
        kw = {k: d.get(k) for k in ("owner_speaker_id", "owner_name", "due", "project", "t0", "project_key",
                                    "project_hint")}
        return cls(id=str(d["id"]), title=str(d["title"]), description=str(d.get("description") or ""),
                   project_confidence=float(d.get("project_confidence") or 0.0),
                   quote=str(d.get("quote") or ""),
                   status=ActionStatus(d.get("status") or ActionStatus.PENDING.value), **kw)


@dataclass(frozen=True)
class Topic:
    title: str
    points: tuple[str, ...] = ()


@dataclass(frozen=True)
class Notes:
    meeting_title: str
    tldr: str
    summary: str
    topics: tuple[Topic, ...] = ()
    decisions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    action_items: tuple[ActionItem, ...] = ()
    language: str = "en"
    project: Optional[str] = None
    project_confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"meeting_title": self.meeting_title, "tldr": self.tldr, "summary": self.summary,
                "topics": [{"title": t.title, "points": list(t.points)} for t in self.topics],
                "decisions": list(self.decisions), "open_questions": list(self.open_questions),
                "action_items": [a.to_dict() for a in self.action_items], "language": self.language,
                "project": self.project, "project_confidence": self.project_confidence}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Notes":
        return cls(meeting_title=str(d.get("meeting_title") or ""), tldr=str(d.get("tldr") or ""),
                   summary=str(d.get("summary") or ""),
                   topics=tuple(Topic(str(t["title"]), tuple(str(p) for p in t.get("points") or ()))
                                for t in d.get("topics") or ()),
                   decisions=tuple(str(x) for x in d.get("decisions") or ()),
                   open_questions=tuple(str(x) for x in d.get("open_questions") or ()),
                   action_items=tuple(ActionItem.from_dict(a) for a in d.get("action_items") or ()),
                   language=str(d.get("language") or "en"), project=d.get("project"),
                   project_confidence=float(d.get("project_confidence") or 0.0))


@dataclass(frozen=True)
class Meeting:
    id: str
    guild_id: str
    channel_id: str
    channel_name: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    state: MeetingState = MeetingState.RECORDING
    title: str = ""
    speakers: tuple[Speaker, ...] = ()
    guild_name: str = ""
    category_name: str = ""
    text_channel_id: Optional[str] = None
    category_id: Optional[str] = None  # the voice channel's Discord category (``meeting_routes``, §19.2)
    folder: str = ""
    partial: bool = False
    language: Optional[str] = None
    project: Optional[str] = None
    started_by: Optional[str] = None
    project_key: Optional[str] = None  # the resolved candidate's key (sink routing, finding 5)
    # Where the meeting came from (DESIGN §17): ``discord`` (live capture) or ``google_meet`` (an
    # imported Meet transcript). ``external_id`` is the source's own id (the Meet conference record
    # name); ``(source, external_id)`` is unique so an import can never be processed twice.
    source: str = SOURCE_DISCORD
    external_id: Optional[str] = None
    # The space (team/client) the meeting belongs to (DESIGN §23): fixed at creation, never crosses.
    space: str = ""
    # User ids of people who were in the call (unmuted, > 1 min) but whose voice was never captured
    # (DESIGN §4.1): every surface that shows the notes says so.
    missing_audio: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("started_at", "ended_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")

    @property
    def human_speakers(self) -> tuple[Speaker, ...]:
        return tuple(s for s in self.speakers if not s.is_bot)

    @property
    def missing_audio_names(self) -> tuple[str, ...]:
        """Display names of :attr:`missing_audio` (the id when the person is not a speaker)."""
        names = {s.user_id: s.name for s in self.speakers}
        return tuple(names.get(uid) or uid for uid in self.missing_audio)

    @property
    def duration_seconds(self) -> Optional[float]:
        return (self.ended_at - self.started_at).total_seconds() if self.ended_at else None

    def with_state(self, target: MeetingState, *, rewind: bool = False) -> "Meeting":
        return replace(self, state=transition(self.state, target, rewind=rewind))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        d["started_at"] = self.started_at.isoformat()
        d["ended_at"] = self.ended_at.isoformat() if self.ended_at else None
        d["speakers"] = [asdict(s) for s in self.speakers]
        d["missing_audio"] = list(self.missing_audio)
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Meeting":
        data = dict(d)
        data["state"] = MeetingState(data.get("state") or MeetingState.RECORDING.value)
        data["started_at"] = datetime.fromisoformat(data["started_at"])
        data["ended_at"] = datetime.fromisoformat(data["ended_at"]) if data.get("ended_at") else None
        data["speakers"] = tuple(Speaker(str(s["user_id"]), str(s["name"]), bool(s.get("is_bot")),
                                         str(s.get("google_user") or ""),
                                         tuple(str(a) for a in s.get("aliases") or ()))
                                 for s in data.get("speakers") or ())
        data["missing_audio"] = tuple(str(u) for u in data.get("missing_audio") or ())
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(frozen=True)
class Candidate:
    """A project a meeting/action item may belong to (DESIGN §7 project resolution)."""

    key: str
    name: str
    source: str  # hermes | kanban | linear | learned
    ref: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectResolution:
    candidate: Optional[Candidate]
    confidence: float
    reason: str = ""


@dataclass(frozen=True)
class SinkResult:
    sink: str
    ok: bool
    delivered: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    detail: str = ""
    deferred: bool = False  # not delivered only because the target is not ready yet (Discord connecting)
    # not delivered because no destination could be resolved (no channel configured/found): waits
    # without a deadline until one is configured (DESIGN §19); implies ``deferred``
    waiting: bool = False
