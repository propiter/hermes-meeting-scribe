"""Keep action-item ids stable across re-analysis (E2E finding).

Ids are content-derived (``action_item_id(title, owner)``) so that re-running analysis yields the
same idempotency keys. LLMs rephrase, though: in the real end-to-end run a ``reprocess`` turned
"Preparar el plan de pruebas de carga" into "Preparar plan de pruebas de carga", which produced a
new id and a DUPLICATE Kanban task. Before the new notes are stored, each new item is matched to
an item of the previous analysis with the same owner and a near-identical title, and inherits its
id. Matching is a greedy best-first assignment, each previous id used at most once. Quotes are not
used: one sentence often carries two commitments.
"""
from __future__ import annotations

import difflib
from dataclasses import replace
from typing import Sequence

from ..domain.models import ActionItem
from ..domain.text import fold

TITLE_RATIO = 0.8


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, fold(a), fold(b)).ratio()


def reconcile_ids(previous: Sequence[ActionItem], new: Sequence[ActionItem],
                  threshold: float = TITLE_RATIO) -> list[ActionItem]:
    if not previous:
        return list(new)
    pairs = sorted(((_ratio(n.title, p.title), i, j) for i, n in enumerate(new) for j, p in enumerate(previous)
                    if (n.owner_speaker_id or None) == (p.owner_speaker_id or None)),
                   key=lambda x: (-x[0], x[1], x[2]))
    assigned: dict[int, str] = {}
    used: set[int] = set()
    for score, i, j in pairs:
        if score < threshold:
            break
        if i in assigned or j in used:
            continue
        assigned[i] = previous[j].id
        used.add(j)
    out: list[ActionItem] = []
    seen: set[str] = set()
    taken = set(assigned.values())
    for i, item in enumerate(new):
        item_id = assigned.get(i, item.id)
        if i not in assigned and item_id in taken:
            continue  # its content id now names another (rephrased) item: it is a duplicate
        if item_id in seen:
            continue
        seen.add(item_id)
        out.append(replace(item, id=item_id) if item_id != item.id else item)
    return out
