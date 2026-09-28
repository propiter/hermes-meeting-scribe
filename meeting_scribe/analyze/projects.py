"""Project candidates and resolution (DESIGN §7).

Candidates come from injected catalogs (Hermes projects, kanban boards, Linear projects, the
learned channel→project map). A failing catalog is reported, never fatal: a locked kanban DB must
not stop notes from being produced. Resolution precedence: learned channel map (a human decided)
→ LLM choice among candidates above ``min_confidence`` → a unique candidate named in the
guild/category/channel names → unassigned.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Protocol, Sequence

from ..domain.text import fold
from ..domain.models import Candidate, Meeting, ProjectResolution


class LearnedMap(Protocol):
    def channel_project(self, space: str, channel_id: str) -> Optional[dict[str, str]]: ...


def _norm(text: str) -> str:
    return fold(text)  # script-preserving: Cyrillic/CJK names and projects must match too


@dataclass
class CallableCatalog:
    """Adapter turning a function into a ``ProjectCatalog`` (keeps Hermes imports out of here)."""

    name: str
    fn: Callable[[Meeting], list[Candidate]]

    def candidates(self, meeting: Meeting) -> list[Candidate]:
        return list(self.fn(meeting))


class LearnedCatalog:
    name = "learned"

    def __init__(self, repo: LearnedMap) -> None:
        self._repo = repo

    def candidates(self, meeting: Meeting) -> list[Candidate]:
        row = self._repo.channel_project(meeting.space, meeting.channel_id)
        return [Candidate(row["project_key"], row["project_name"], "learned")] if row else []


def gather_candidates(catalogs: Iterable[object], meeting: Meeting) -> tuple[list[Candidate], list[str]]:
    seen: dict[str, Candidate] = {}
    errors: list[str] = []
    for catalog in catalogs:
        name = getattr(catalog, "name", type(catalog).__name__)
        try:
            for cand in catalog.candidates(meeting):  # type: ignore[attr-defined]
                prev = seen.get(cand.key)
                # A learned entry only remembers a key: the real catalog's candidate (with its ``ref``:
                # project id, team ids, board) must win over it whatever the catalog order (finding 5).
                if prev is None or (prev.source == "learned" and cand.source != "learned"):
                    seen[cand.key] = cand
        except Exception as exc:  # any backend failure degrades to "fewer candidates"
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    return list(seen.values()), errors


def hints_for(meeting: Meeting) -> list[str]:
    return [h for h in (meeting.guild_name, meeting.category_name, meeting.channel_name) if h]


def find_candidate(value: str, candidates: Sequence[Candidate]) -> Optional[Candidate]:
    wanted = _norm(value)
    return next((c for c in candidates if wanted and wanted in (_norm(c.name), _norm(c.key))), None)


def route_candidate(name: str, key: Optional[str], candidates: Sequence[Candidate],
                    sources: Optional[Sequence[str]] = None) -> Optional[Candidate]:
    """The candidate a SINK should use (review finding 5).

    ``sources`` limits the pool to what the sink understands (Linear needs a ``linear`` candidate
    for projectId/teamId; Kanban takes ``hermes``/``kanban``). Within it, the resolved ``key`` wins,
    then a name match — so a Hermes and a Linear project with the same name each route correctly.
    """
    pool = [c for c in candidates if sources is None or c.source in sources]
    if key:
        exact = next((c for c in pool if c.key == key), None)
        if exact is not None:
            return exact
    return find_candidate(name, pool)


class ProjectResolver:
    def __init__(self, min_confidence: float, learned: LearnedMap) -> None:
        self._min = min_confidence
        self._learned = learned

    def learned_first(self, meeting: Meeting, candidates: Sequence[Candidate]) -> Optional[Candidate]:
        row = self._learned.channel_project(meeting.space, meeting.channel_id)
        if not row:
            return None
        return next((c for c in candidates if c.key == row["project_key"]),
                    Candidate(row["project_key"], row["project_name"], "learned"))

    def _hint(self, meeting: Meeting, candidates: Sequence[Candidate]) -> Optional[Candidate]:
        haystack = f" {_norm(' '.join(hints_for(meeting)))} "
        hits = [c for c in candidates if _norm(c.name) and f" {_norm(c.name)} " in haystack]
        return hits[0] if len(hits) == 1 else None

    def resolve(self, meeting: Meeting, candidates: Sequence[Candidate], llm_choice: Optional[str],
                llm_confidence: float) -> ProjectResolution:
        learned = self.learned_first(meeting, candidates)
        if learned:
            return ProjectResolution(learned, 1.0, "learned")
        if llm_choice and llm_confidence >= self._min:
            chosen = find_candidate(llm_choice, candidates)
            if chosen:
                return ProjectResolution(chosen, llm_confidence, "llm")
        hinted = self._hint(meeting, candidates)
        if hinted:
            return ProjectResolution(hinted, self._min, "hint")
        return ProjectResolution(None, 0.0, "none")
