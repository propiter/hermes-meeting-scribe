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
from .llm_config import AUX_TASK, TooManyHungCalls, run_with_deadline

log = logging.getLogger(__name__)
_JSON_REMINDER = ("\n\nIMPORTANT: your previous answer was not valid JSON. Reply with ONE complete JSON object "
                  "only — no prose, no Markdown fences — and keep it concise so it is not cut off.")
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


class LlmTimeout(TimeoutError):
    """The LLM call did not return within ``analysis_timeout_seconds``; the attempt fails and the job
    backs off. The worker thread doing the call is abandoned (Python cannot kill it)."""


class HermesStructuredLLM:
    """``StructuredLLM`` over ``ctx.llm.complete_structured`` with our auxiliary task, so users can
    route meeting analysis to a different model via ``auxiliary.meeting_scribe.*``.

    Robustness (DESIGN §18):

    * ``max_tokens`` is always sent (``analysis_max_tokens``): without it some providers reserve the
      whole context window and a low balance turns into ``402 … can only afford N``.
    * A wall-clock deadline (``analysis_timeout_seconds``) of our own: Hermes' ``timeout`` is per
      request and its retries/fallbacks can add up well beyond it (a call was seen hanging ~30 min).
    * A reply that is not JSON (typically truncated) is retried ONCE right away with a stricter
      instruction before the attempt counts as failed. Cost: that chunk is sent (and billed) twice,
      and the attempt can take up to twice ``analysis_timeout_seconds``.
    * A call that misses the deadline keeps running in an abandoned thread; with
      ``llm_config.MAX_ABANDONED`` of them still alive, the next call fails fast
      (``TooManyHungCalls``, logged) instead of piling up threads and provider connections.
    """

    def __init__(self, llm: Callable[[], Any], timeout: "float | Callable[[], float]" = 600.0,
                 max_tokens: "Optional[int] | Callable[[], Optional[int]]" = None) -> None:
        self._llm = llm
        self._timeout = timeout
        self._max_tokens = max_tokens

    def _limits(self) -> tuple[float, Optional[int]]:
        timeout = self._timeout() if callable(self._timeout) else self._timeout
        max_tokens = self._max_tokens() if callable(self._max_tokens) else self._max_tokens
        return float(timeout), (int(max_tokens) if max_tokens else None)

    def _structured(self, base: dict[str, Any], json_schema: Mapping[str, Any]) -> Any:
        try:
            return self._llm().complete_structured(json_schema=dict(json_schema), **base)
        except ValueError as exc:  # Hermes' schema validation; our normaliser copes with the variants
            log.warning("meeting-scribe: structured output rejected (%s); retrying in plain JSON mode", exc)
            return self._llm().complete_structured(json_mode=True, **base)

    def _once(self, base: dict[str, Any], json_schema: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
        try:
            result = run_with_deadline(lambda: self._structured(base, json_schema), timeout,
                                       name="meeting-scribe-llm")
        except TimeoutError as exc:
            raise LlmTimeout(f"LLM call did not return within {timeout:.0f}s (analysis_timeout_seconds); "
                             "abandoned, the attempt will be retried") from exc
        parsed = getattr(result, "parsed", None)
        return parsed if isinstance(parsed, Mapping) else parse_json_text(getattr(result, "text", ""))

    def complete_json(self, *, instructions: str, text: str, json_schema: Mapping[str, Any],
                      schema_name: str) -> Mapping[str, Any]:
        timeout, max_tokens = self._limits()
        base: dict[str, Any] = dict(instructions=instructions, input=[{"type": "text", "text": text}],
                                    schema_name=schema_name, task=AUX_TASK, timeout=timeout,
                                    purpose="meeting analysis")
        if max_tokens:
            base["max_tokens"] = max_tokens
        try:
            return self._once(base, json_schema, timeout)
        except (LlmTimeout, TooManyHungCalls):
            raise
        except ValueError as exc:  # not JSON (usually truncated): one immediate, stricter retry
            log.warning("meeting-scribe: %s; retrying once with a stricter instruction", exc)
            return self._once({**base, "instructions": instructions + _JSON_REMINDER}, json_schema, timeout)


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
