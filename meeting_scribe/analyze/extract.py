"""LLM analysis (implements the ``Analyzer`` port): single call or map-reduce, then strict
normalisation so downstream sinks can trust the shape regardless of the provider.

Normalisation rules (DESIGN §7): owners are matched to real speakers (id, then name/fuzzy);
``due`` survives only as a valid ISO date (the prompt forbids guessing, we enforce it); the
project must be one of the offered candidates and above ``projects.min_confidence``; action-item
ids are content-derived so re-analysis keeps the same idempotency keys.
"""
from __future__ import annotations

import difflib
import json
import re
import unicodedata
from dataclasses import replace
from datetime import date
from typing import Any, Callable, Mapping, Optional, Sequence

from ..config import Settings
from ..domain.ids import action_item_id
from ..domain.models import ActionItem, Candidate, Meeting, Notes, Speaker, Topic, Utterance
from ..domain.ports import StructuredLLM
from . import prompts
from .chunking import chunk_utterances, render_lines
from .projects import hints_for
from .schemas import CHUNK_SCHEMA, NOTES_SCHEMA

FUZZY_RATIO = 0.85


def _norm(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s).split())


def match_owner(speaker_id: Optional[str], name: Optional[str], speakers: Sequence[Speaker]) -> Optional[Speaker]:
    by_id = {s.user_id: s for s in speakers}
    if speaker_id and str(speaker_id) in by_id:
        return by_id[str(speaker_id)]
    wanted = _norm(name or "")
    if not wanted:
        return None
    exact = [s for s in speakers if _norm(s.name) == wanted]
    if len(exact) == 1:
        return exact[0]
    first = [s for s in speakers if _norm(s.name).split()[:1] == wanted.split()[:1]]
    if len(first) == 1 and len(wanted.split()) == 1:
        return first[0]
    scored = sorted(((difflib.SequenceMatcher(None, wanted, _norm(s.name)).ratio(), s) for s in speakers),
                    key=lambda x: -x[0])
    if scored and scored[0][0] >= FUZZY_RATIO and (len(scored) == 1 or scored[1][0] < scored[0][0]):
        return scored[0][1]
    return None


def _iso_date(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat() if re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", value.strip()[:10]) else None
    except ValueError:
        return None


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
    def _candidate(self, value: Any, confidence: float, candidates: Sequence[Candidate],
                   min_conf: float) -> Optional[str]:
        if not isinstance(value, str) or confidence < min_conf:
            return None
        wanted = _norm(value)
        for c in candidates:
            if wanted in (_norm(c.name), _norm(c.key)):
                return c.name
        return None

    def _items(self, raw: Any, speakers: Sequence[Speaker], candidates: Sequence[Candidate],
               min_conf: float) -> tuple[ActionItem, ...]:
        out: dict[str, ActionItem] = {}
        for it in raw if isinstance(raw, list) else ():
            if not isinstance(it, Mapping) or not str(it.get("title") or "").strip():
                continue
            owner = match_owner(it.get("owner_speaker_id"), it.get("owner_name"), speakers)
            conf = _conf(it.get("project_confidence"))
            t0 = it.get("t0")
            title = " ".join(str(it["title"]).split())
            item = ActionItem(
                id=action_item_id(title, owner.user_id if owner else None), title=title,
                description=str(it.get("description") or "").strip(),
                owner_speaker_id=owner.user_id if owner else None,
                owner_name=owner.name if owner else (str(it.get("owner_name")).strip() or None
                                                     if it.get("owner_name") else None),
                due=_iso_date(it.get("due")),
                project=self._candidate(it.get("project"), conf, candidates, min_conf),
                project_confidence=conf, quote=str(it.get("quote") or "").strip(),
                t0=float(t0) if isinstance(t0, (int, float)) and not isinstance(t0, bool) else None)
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
        settings = self._settings()
        lang_setting = settings.analysis_language
        if not utterances:
            return Notes(meeting_title=meeting.title or meeting.channel_name, tldr="", summary="",
                         language=lang_setting if lang_setting != "auto" else (meeting.language or "en"))
        context = prompts.candidates_block(candidates, hints_for(meeting))
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
