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

from ..domain.models import ActionItem, Meeting, Notes, is_unidentified
from ..i18n import t

MESSAGE_LIMIT = 2000
EMBED_LIMIT = 4096
ACTIONS = ("ok", "lin", "no", "prj", "allk", "alll", "psel", "mine", "pg", "tsel", "shp", "shd", "sha", "shc",
           "tak", "tas", "trl", "tun", "asel", "ausr", "pok", "pno")
TEMPLATE = r"mscribe:(?P<action>ok|lin|no|prj|allk|alll|psel|mine|pg|tsel|shp|shd|sha|shc|spk|ssel|scfm|sme|tak|tas|trl|tun|asel|ausr|pok|pno):(?P<meeting>[a-z0-9]{1,16}):(?P<item>[A-Za-z0-9_-]{1,40})"
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
    # The user ids this message may notify, and nobody else (``()``: nobody): the notes' participants
    # line, a task's assignee. Any other ``<@id>`` in the text (model output, typed names) stays inert
    # (DESIGN §19.4).
    mentions: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderOptions:
    lang: str
    kanban_on: bool
    linear_on: bool
    is_owner_item: Callable[[ActionItem], bool]
    has_candidates: bool = True
    can_assign: bool = True  # 🙋/👤 on the task card (DESIGN §16.2); never in a direct-messages copy


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


_MD_SPECIAL = re.compile(r"([\\*_`~|>\[\]()#:-])")


def safe_name(name: str) -> str:
    """A display name as inert Discord text: no mentions, no Markdown, one line.

    Google Meet names are typed by the participants themselves (anonymous guests included), so a
    name like ``<@123>`` or ``**x**`` must never ping or format anything. Every ``@`` gets a
    zero-width space (stricter than ``discord.utils.escape_mentions``, which only catches 17-20
    digit ids) and Markdown characters are backslash-escaped (``escape_markdown``).
    """
    one_line = " ".join(str(name or "").split())
    return _MD_SPECIAL.sub(r"\\\1", one_line).replace("@", "@\u200b")


# -- rendering -----------------------------------------------------------------------------------
def _header(meeting: Meeting, notes: Notes, lang: str, participants: str = "") -> str:
    none = t("notes.none", lang)
    title = notes.meeting_title or meeting.title or meeting.channel_name
    out = [f"## 🎙️ {title}", f"-# `{meeting.id}` · {meeting.started_at:%Y-%m-%d %H:%M} · {meeting.channel_name}"]
    if participants:
        out.append(participants)
    if meeting.partial:
        out.append(f"> ⚠️ {t('notes.partial', lang)}")
    if meeting.missing_audio:
        names = ", ".join(safe_name(n) for n in meeting.missing_audio_names)
        out.append(f"> ⚠️ {t('notes.missing_audio', lang, names=names)}")
    out += [f"**{t('notes.tldr', lang)}:** {notes.tldr or none}", "", f"**{t('notes.decisions', lang)}**"]
    out += [f"- {d}" for d in notes.decisions] or [none]
    out += ["", f"**{t('notes.open_questions', lang)}**"]
    out += [f"- {q}" for q in notes.open_questions] or [none]
    return "\n".join(out)


def render_header(meeting: Meeting, notes: Notes, lang: str, participants: str = "",
                  pings: Optional[tuple[str, ...]] = None) -> list[MessageSpec]:
    """The meeting-chat summary (title, TL;DR, decisions, open questions), split under 2000 chars.
    ``participants`` (a line under the title) and ``pings`` (who that line may ping) only concern the
    FIRST part; every other part, and the model's text anywhere, pings nobody."""
    parts = split_text(_header(meeting, notes, lang, participants))
    return [MessageSpec(part, buttons=speaker_buttons(meeting, lang) if i == 0 else (),
                        mentions=(tuple(pings or ()) if i == 0 and participants else ()))
            for i, part in enumerate(parts)]


def speaker_buttons(meeting: Meeting, lang: str) -> tuple[ButtonSpec, ...]:
    """"Who is <unidentified participant>?" — one per track still unassigned (DESIGN §4.1), at most 5
    (one row); who may press it is decided on click (``auth.check_speaker``)."""
    names = {s.user_id: s.name for s in meeting.speakers}
    assigned = tuple(ButtonSpec(t("speakers.correct", lang, name=names.get(uid, uid))[:80],
                                 custom_id("spk", meeting.id, label), "secondary", 1, "👤")
                     for label, uid in getattr(meeting, "speaker_assignments", {}).items())
    return (assigned + tuple(ButtonSpec(t("speakers.suggestion" if s.suggested_user else "ui.btn_assign", lang,
                              name=names.get(s.suggested_user, s.name))[:80],
                            custom_id("scfm" if s.suggested_user else "spk", meeting.id, s.user_id),
                            "secondary", 1, "👤")
                 for s in meeting.speakers if is_unidentified(s.user_id)))[:5]
