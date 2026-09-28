"""LLM analysis (implements the ``Analyzer`` port): single call or map-reduce, then strict
normalisation so downstream sinks can trust the shape regardless of the provider.

Normalisation rules (DESIGN §7): owners are matched to the meeting's participants (id, then name);
``due`` survives only as a valid ISO date (the prompt forbids guessing, we enforce it); the
project must be one of the offered candidates and above ``projects.min_confidence``; action-item
ids are content-derived so re-analysis keeps the same idempotency keys.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date
from typing import Any, Callable, Mapping, Optional, Sequence

from ..config import Settings
from ..domain.text import fold
from ..domain.ids import action_item_id
from ..domain.names import match_person
from ..domain.models import ActionItem, Candidate, Meeting, Notes, Speaker, Topic, Utterance
from ..domain.ports import StructuredLLM
from . import prompts
from .chunking import chunk_utterances, render_lines
from .projects import hints_for
from .schemas import CHUNK_SCHEMA, NOTES_SCHEMA

_SOURCE_SUFFIX_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _norm(text: str) -> str:
    return fold(text)  # script-preserving: Cyrillic/CJK names and projects must match too


def match_owner(speaker_id: Optional[str], name: Optional[str], speakers: Sequence[Speaker]) -> Optional[Speaker]:
    """The participant owning a task: the id the LLM chose from the participant list, else the ONE
    participant the spoken name designates (:func:`match_person`: accents, emoji, given name,
    nicknames and phonetic spellings tolerated; ambiguity leaves it unassigned)."""
    by_id = {s.user_id: s for s in speakers}
    if speaker_id and str(speaker_id) in by_id:
        return by_id[str(speaker_id)]
    key = match_person(name or "", [(s.user_id, (s.name, *s.aliases)) for s in speakers if not s.is_bot])
    return by_id.get(key) if key is not None else None


def _iso_date(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat() if re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", value.strip()[:10]) else None
    except ValueError:
        return None


def _seconds(value: Any) -> Optional[float]:
    """``3.5``, ``"3.5"``, ``"12:30"`` (mm:ss) or ``"1:02:03"`` → seconds; anything else → None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return float(raw)
    m = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d+)?)", raw)
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _conf(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _strs(value: Any) -> tuple[str, ...]:
    return tuple(str(v).strip() for v in value or () if str(v).strip()) if isinstance(value, list) else ()


class LlmAnalyzer:
    def __init__(self, llm: StructuredLLM, settings: Callable[[], Settings]) -> None:
        self._llm = llm
        self._settings = settings

    # -- normalisation --------------------------------------------------------------------------
    @staticmethod
    def _spoken(value: Any) -> Optional[str]:
        # Models echo the candidate line format ("Website (hermes)"); the E2E run hit exactly that.
        text = _SOURCE_SUFFIX_RE.sub("", value).strip() if isinstance(value, str) else ""
        return text or None

    def _pick(self, value: Any, confidence: float, candidates: Sequence[Candidate],
              min_conf: float) -> Optional[Candidate]:
        if not isinstance(value, str) or confidence < min_conf:
            return None
        wanted = {_norm(value), _norm(_SOURCE_SUFFIX_RE.sub("", value))}
        return next((c for c in candidates if wanted & {_norm(c.name), _norm(c.key)}), None)

    def _candidate(self, value: Any, confidence: float, candidates: Sequence[Candidate],
                   min_conf: float) -> Optional[str]:
        chosen = self._pick(value, confidence, candidates, min_conf)
        return chosen.name if chosen else None

    def _items(self, raw: Any, speakers: Sequence[Speaker], candidates: Sequence[Candidate],
               min_conf: float) -> tuple[ActionItem, ...]:
        out: dict[str, ActionItem] = {}
        for it in raw if isinstance(raw, list) else ():
            if not isinstance(it, Mapping) or not str(it.get("title") or "").strip():
                continue
            owner = match_owner(it.get("owner_speaker_id"), it.get("owner_name"), speakers)
            conf = _conf(it.get("project_confidence"))
            title = " ".join(str(it["title"]).split())
            chosen = self._pick(it.get("project"), conf, candidates, min_conf)
            hint = None if chosen else (self._spoken(it.get("project_hint")) or self._spoken(it.get("project")))
            item = ActionItem(
                id=action_item_id(title, owner.user_id if owner else None), title=title,
                description=str(it.get("description") or "").strip(),
                owner_speaker_id=owner.user_id if owner else None,
                owner_name=owner.name if owner else (str(it.get("owner_name")).strip() or None
                                                     if it.get("owner_name") else None),
                due=_iso_date(it.get("due")),
                project=chosen.name if chosen else None, project_key=chosen.key if chosen else None,
                project_hint=hint, project_confidence=conf, quote=str(it.get("quote") or "").strip(),
                t0=_seconds(it.get("t0")))
            out.setdefault(item.id, item)
        return tuple(out.values())

    @staticmethod
    def _topics(raw: Any) -> tuple[Topic, ...]:
        return tuple(Topic(str(t.get("title")).strip(), _strs(t.get("points")))
                     for t in raw if isinstance(t, Mapping) and t.get("title")) if isinstance(raw, list) else ()

    # -- LLM calls ------------------------------------------------------------------------------
    def _call(self, instructions: str, text: str, schema: Mapping[str, Any], name: str,
              required: Sequence[str]) -> Mapping[str, Any]:
        data = self._llm.complete_json(instructions=instructions, text=text, json_schema=schema, schema_name=name)
        if not isinstance(data, Mapping) or any(k not in data for k in required):
            raise ValueError(f"LLM returned malformed {name}: missing {[k for k in required if k not in (data or {})]}")
        return data

    def analyze(self, meeting: Meeting, utterances: Sequence[Utterance], candidates: Sequence[Candidate]) -> Notes:
        settings = self._settings(meeting.space)
        lang_setting = settings.analysis_language
        if not utterances:
            return Notes(meeting_title=meeting.title or meeting.channel_name, tldr="", summary="",
                         language=lang_setting if lang_setting != "auto" else (meeting.language or "en"))
        context = (prompts.participants_block(meeting.speakers) + "\n" +
                   prompts.candidates_block(candidates, hints_for(meeting), meeting.started_at.date()))
        chunks = chunk_utterances(utterances, settings.analysis_chunk_chars)
        required = ("meeting_title", "tldr", "summary", "action_items")
        if len(chunks) == 1:
            text = f"{context}\n<transcript>\n" + "\n".join(render_lines(chunks[0])) + "\n</transcript>"
            data = self._call(prompts.single_instructions(lang_setting), text, NOTES_SCHEMA, "meeting_notes",
                              required)
        else:
            partials = [self._call(prompts.chunk_instructions(lang_setting),
                                   f"{context}\n<transcript>\n" + "\n".join(render_lines(c)) + "\n</transcript>",
                                   CHUNK_SCHEMA, "meeting_chunk", ("summary", "action_items"))
                        for c in chunks]
            text = f"{context}\n<chunk_notes>\n{json.dumps(list(partials), ensure_ascii=False)}\n</chunk_notes>"
            data = self._call(prompts.reduce_instructions(lang_setting), text, NOTES_SCHEMA, "meeting_notes",
                              required)
        return self._notes(meeting, data, candidates, settings)

    def _notes(self, meeting: Meeting, data: Mapping[str, Any], candidates: Sequence[Candidate],
               settings: Settings) -> Notes:
        min_conf = settings.projects_min_confidence
        conf = _conf(data.get("project_confidence"))
        language = settings.analysis_language if settings.analysis_language != "auto" else (
            str(data.get("language") or meeting.language or "en").lower()[:5])
        notes = Notes(
            meeting_title=" ".join(str(data.get("meeting_title") or meeting.title or meeting.channel_name).split()),
            tldr=str(data.get("tldr") or "").strip(), summary=str(data.get("summary") or "").strip(),
            topics=self._topics(data.get("topics")), decisions=_strs(data.get("decisions")),
            open_questions=_strs(data.get("open_questions")),
            action_items=self._items(data.get("action_items"), meeting.speakers, candidates, min_conf),
            language=language, project=self._candidate(data.get("project"), conf, candidates, min_conf),
            project_confidence=conf)
        return replace(notes, project_confidence=conf if notes.project else 0.0)
