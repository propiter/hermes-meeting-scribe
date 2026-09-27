"""Thin adapters from Hermes APIs to our ports. Hermes imports stay lazy so unit tests (and the
plugin validator's sandbox) never need a running gateway."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .domain.models import Candidate, Meeting

log = logging.getLogger(__name__)
AUX_TASK = "meeting_scribe"
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def is_gateway_process() -> bool:
    """True inside ``hermes gateway`` (``gateway/run.py`` sets ``_HERMES_GATEWAY=1`` at import).

    Only the gateway runs the pipeline worker and recovery; CLI/TUI processes must not
    (review finding 2). Children the gateway spawns inherit the marker, but they do not load
    plugins with a Discord adapter, and job leases keep even that case safe.
    """
    return os.environ.get("_HERMES_GATEWAY") == "1"


def parse_json_text(text: str) -> Any:
    """Providers without ``response_format`` sometimes wrap JSON in fences or prose."""
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text or "")] + [text or ""]
    for chunk in candidates:
        start, end = chunk.find("{"), chunk.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(chunk[start:end + 1])
            except ValueError:
                continue
    raise ValueError("LLM response is not JSON")


class HermesStructuredLLM:
    """``StructuredLLM`` over ``ctx.llm.complete_structured`` with our auxiliary task, so users can
    route meeting analysis to a different model via ``auxiliary.meeting_scribe.*``."""

    def __init__(self, llm: Callable[[], Any], timeout: float = 600.0) -> None:
        self._llm = llm
        self._timeout = timeout

    def complete_json(self, *, instructions: str, text: str, json_schema: Mapping[str, Any],
                      schema_name: str) -> Mapping[str, Any]:
        base = dict(instructions=instructions, input=[{"type": "text", "text": text}], schema_name=schema_name,
                    task=AUX_TASK, timeout=self._timeout, purpose="meeting analysis")
        try:
            result = self._llm().complete_structured(json_schema=dict(json_schema), **base)
        except ValueError as exc:  # Hermes' schema validation; our normaliser copes with the variants
            log.warning("meeting-scribe: structured output rejected (%s); retrying in plain JSON mode", exc)
            result = self._llm().complete_structured(json_mode=True, **base)
        parsed = getattr(result, "parsed", None)
        return parsed if isinstance(parsed, Mapping) else parse_json_text(getattr(result, "text", ""))


def hermes_projects(_meeting: Meeting) -> list[Candidate]:
    """Hermes projects of the active profile (``projects_db``)."""
    from hermes_cli import projects_db as pdb

    with pdb.connect_closing() as conn:
        return [Candidate(f"hermes:{p.id}", p.name, "hermes", {"project_id": p.id, "board_slug": p.board_slug})
                for p in pdb.list_projects(conn)]


def kanban_boards(_meeting: Optional[Meeting] = None) -> list[Candidate]:
    from hermes_cli import kanban_db as kb

    return [Candidate(f"kanban:{b['slug']}", str(b.get("name") or b["slug"]), "kanban", {"board": b["slug"]})
            for b in kb.list_boards(include_archived=False)]


def linear_projects(backend: Callable[[], Any]) -> Callable[[Meeting], list[Candidate]]:
    def fetch(_meeting: Meeting) -> list[Candidate]:
        b = backend()
        if b is None:
            return []
        return [Candidate(f"linear:{p['id']}", p["name"], "linear",
                          {"project_id": p["id"], "team_ids": list(p.get("team_ids") or ())}) for p in b.projects()]

    return fetch


def secret(name: str) -> Optional[str]:
    """Profile-scoped secret lookup (DESIGN §1.4)."""
    from agent.secret_scope import get_secret

    return get_secret(name)


def data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir("meeting-scribe")


def context_spawner(target: Callable[[], None], *, name: str, daemon: bool = True) -> threading.Thread:
    """Thread factory that carries the profile contextvars into the pipeline worker."""
    from agent.memory_provider import spawn_context_thread

    return spawn_context_thread(target, name=name, daemon=daemon)
