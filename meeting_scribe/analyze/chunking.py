"""Transcript rendering and chunking for map-reduce analysis (DESIGN §7).

Chunks break only at utterance boundaries and overlap by a few utterances so an action item
spoken across a boundary ("¿quién lo hace?" / "yo") is seen whole by at least one chunk call.
"""
from __future__ import annotations

from typing import Sequence

from ..domain.models import Utterance
from ..storage.artifacts import fmt_ts


def render_line(u: Utterance) -> str:
    return f"[{fmt_ts(u.t0)}] {u.speaker} (id={u.speaker_id}): {u.text}"


def render_lines(utterances: Sequence[Utterance]) -> list[str]:
    return [render_line(u) for u in utterances]


def rendered_size(utterances: Sequence[Utterance]) -> int:
    return sum(len(line) + 1 for line in render_lines(utterances))


def chunk_utterances(utterances: Sequence[Utterance], max_chars: int, overlap: int = 3) -> list[list[Utterance]]:
    if not utterances:
        return []
    sizes = [len(render_line(u)) + 1 for u in utterances]
    if sum(sizes) <= max_chars:
        return [list(utterances)]
    chunks: list[list[Utterance]] = []
    start = 0
    n = len(utterances)
    while start < n:
        end, total = start, 0
        while end < n and (total + sizes[end] <= max_chars or end == start):
            total += sizes[end]
            end += 1
        chunks.append(list(utterances[start:end]))
        if end >= n:
            break
        # Overlap must shrink when it would not make progress (tiny chunks of huge utterances).
        start = max(start + 1, end - overlap)
    return chunks
