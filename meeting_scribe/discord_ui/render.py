"""Notes → Discord messages (pure; no discord.py import) — DESIGN §8, §16.

This module renders the meeting-chat summary and owns the button ``custom_id`` scheme. Tasks are
rendered one message each by :mod:`render_tasks` (the 0.1 layout — all tasks, then all button rows
— lost the task/button alignment as soon as one row disappeared). Buttons are persistent
``DynamicItem``s keyed by ``custom_id`` = ``mscribe:<action>:<meeting>:<item>`` (< 100 chars) so they
keep working after a gateway restart.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from ..domain.models import ActionItem, Meeting, Notes
from ..i18n import t

MESSAGE_LIMIT = 2000
EMBED_LIMIT = 4096
ACTIONS = ("ok", "lin", "no", "prj", "allk", "alll", "psel", "mine", "pg", "tsel", "shp", "shd", "sha", "shc")
TEMPLATE = r"mscribe:(?P<action>ok|lin|no|prj|allk|alll|psel|mine|pg|tsel|shp|shd|sha|shc):(?P<meeting>[a-z0-9]{1,16}):(?P<item>[A-Za-z0-9_-]{1,40})"
_TEMPLATE_RE = re.compile(f"^{TEMPLATE}$")
_TOKEN_RE = re.compile(r"<[@#][!&]?\d+>|\S+|\s+")


@dataclass(frozen=True)
class ButtonSpec:
    label: str
    custom_id: str
    style: str  # success | primary | danger | secondary
    row: int = 0
    emoji: Optional[str] = None


@dataclass(frozen=True)
class MessageSpec:
    content: str
    buttons: tuple[ButtonSpec, ...] = ()
    # ``None``: the default mention policy. A tuple: ping exactly these user ids and nothing else
    # (``()`` pings nobody) — the notes' participants line (DESIGN §19.4).
    mentions: Optional[tuple[str, ...]] = None


@dataclass(frozen=True)
class RenderOptions:
    lang: str
    kanban_on: bool
    linear_on: bool
    is_owner_item: Callable[[ActionItem], bool]
    has_candidates: bool = True


def custom_id(action: str, meeting_id: str, item_id: str) -> str:
    cid = f"mscribe:{action}:{meeting_id}:{item_id}"
    if not _TEMPLATE_RE.match(cid):
        raise ValueError(f"invalid custom_id {cid!r}")
    return cid


def parse_custom_id(value: str) -> Optional[tuple[str, str, str]]:
    m = _TEMPLATE_RE.match(value or "")
    return (m["action"], m["meeting"], m["item"]) if m else None


# -- text splitting ------------------------------------------------------------------------------
def _hard_split(token: str, limit: int) -> list[str]:
    return [token[i:i + limit] for i in range(0, len(token), limit)]


def split_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split on line breaks, then on words; mentions (``<@id>``) are never cut in half."""
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        if len(line) <= limit:
            current = line
            continue
        for token in _TOKEN_RE.findall(line):
            for piece in (_hard_split(token, limit) if len(token) > limit else [token]):
                if len(current) + len(piece) > limit:
                    parts.append(current.rstrip())
                    current = piece.lstrip()
                else:
                    current += piece
    if current.strip():
        parts.append(current)
    return [p for p in parts if p.strip()] or [""]


# -- rendering -----------------------------------------------------------------------------------
def _header(meeting: Meeting, notes: Notes, lang: str, participants: str = "") -> str:
    none = t("notes.none", lang)
    title = notes.meeting_title or meeting.title or meeting.channel_name
    out = [f"## 🎙️ {title}", f"-# `{meeting.id}` · {meeting.started_at:%Y-%m-%d %H:%M} · {meeting.channel_name}"]
    if participants:
        out.append(participants)
    if meeting.partial:
        out.append(f"> ⚠️ {t('notes.partial', lang)}")
    out += [f"**{t('notes.tldr', lang)}:** {notes.tldr or none}", "", f"**{t('notes.decisions', lang)}**"]
    out += [f"- {d}" for d in notes.decisions] or [none]
    out += ["", f"**{t('notes.open_questions', lang)}**"]
    out += [f"- {q}" for q in notes.open_questions] or [none]
    return "\n".join(out)


def render_header(meeting: Meeting, notes: Notes, lang: str, participants: str = "",
                  pings: Optional[tuple[str, ...]] = None) -> list[MessageSpec]:
    """The meeting-chat summary (title, TL;DR, decisions, open questions), split under 2000 chars.
    ``participants`` (a line under the title) and ``pings`` (who that line may ping) only concern the
    FIRST part; every other part pings nobody when a participants line is present."""
    parts = split_text(_header(meeting, notes, lang, participants))
    if not participants:
        return [MessageSpec(part) for part in parts]
    return [MessageSpec(part, mentions=(tuple(pings or ()) if i == 0 else ())) for i, part in enumerate(parts)]
