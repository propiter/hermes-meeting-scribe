"""Notes → Discord messages (pure; no discord.py import) — DESIGN §8.

Layout:
  1. header: title, partial warning, TL;DR, decisions, open questions (split under 2000 chars);
  2. action items grouped by owner (``<@id>`` mention, "Unassigned" last), at most five items per
     message because each item gets one action row of buttons and Discord allows five rows;
  3. a bulk row: "Approve all → Kanban", "Approve all → Linear", "📁 Project".
Buttons are persistent ``DynamicItem``s keyed by ``custom_id`` = ``mscribe:<action>:<meeting>:<item>``
so they keep working after a gateway restart.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from ..domain.models import ActionItem, ActionStatus, Meeting, Notes
from ..i18n import t
from ..storage.artifacts import fmt_ts

MESSAGE_LIMIT = 2000
EMBED_LIMIT = 4096
ITEMS_PER_MESSAGE = 5
ACTIONS = ("ok", "lin", "no", "prj", "allk", "alll", "psel", "mine", "pg", "tsel")
TEMPLATE = r"mscribe:(?P<action>ok|lin|no|prj|allk|alll|psel|mine|pg|tsel):(?P<meeting>[a-z0-9]{1,16}):(?P<item>[A-Za-z0-9_-]{1,40})"
_TEMPLATE_RE = re.compile(f"^{TEMPLATE}$")
_TOKEN_RE = re.compile(r"<[@#][!&]?\d+>|\S+|\s+")
_STATUS_ICON = {ActionStatus.PENDING: "▫️", ActionStatus.APPROVED: "☑️", ActionStatus.DELIVERED: "✅",
                ActionStatus.DISMISSED: "❌"}


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
def _header(meeting: Meeting, notes: Notes, lang: str) -> str:
    none = t("notes.none", lang)
    title = notes.meeting_title or meeting.title or meeting.channel_name
    out = [f"## 🎙️ {title}", f"-# `{meeting.id}` · {meeting.started_at:%Y-%m-%d %H:%M} · {meeting.channel_name}"]
    if meeting.partial:
        out.append(f"> ⚠️ {t('notes.partial', lang)}")
    out += [f"**{t('notes.tldr', lang)}:** {notes.tldr or none}", "", f"**{t('notes.decisions', lang)}**"]
    out += [f"- {d}" for d in notes.decisions] or [none]
    out += ["", f"**{t('notes.open_questions', lang)}**"]
    out += [f"- {q}" for q in notes.open_questions] or [none]
    return "\n".join(out)


def _item_line(item: ActionItem) -> str:
    title = f"~~{item.title}~~" if item.status is ActionStatus.DISMISSED else f"**{item.title}**"
    extra = []
    if item.due:
        extra.append(f"📅 {item.due}")
    if item.t0 is not None:
        extra.append(f"[{fmt_ts(item.t0)}]")
    tail = f" — {' · '.join(extra)}" if extra else ""
    return f"{_STATUS_ICON[item.status]} {title}{tail}"


def _item_buttons(meeting: Meeting, item: ActionItem, row: int, o: RenderOptions) -> list[ButtonSpec]:
    if item.status in (ActionStatus.DELIVERED, ActionStatus.DISMISSED):
        return []
    out: list[ButtonSpec] = []
    if o.kanban_on and o.is_owner_item(item):
        out.append(ButtonSpec(t("ui.btn_kanban", o.lang), custom_id("ok", meeting.id, item.id), "success", row, "✅"))
    if o.linear_on:
        out.append(ButtonSpec(t("ui.btn_linear", o.lang), custom_id("lin", meeting.id, item.id), "primary", row, "🟣"))
    out.append(ButtonSpec(t("ui.btn_dismiss", o.lang), custom_id("no", meeting.id, item.id), "secondary", row, "❌"))
    return out


def _groups(items: Sequence[ActionItem]) -> list[tuple[Optional[str], Optional[str], list[ActionItem]]]:
    order: dict[Optional[str], tuple[Optional[str], list[ActionItem]]] = {}
    for item in items:
        key = item.owner_speaker_id or None
        order.setdefault(key, (item.owner_name, []))[1].append(item)
    keyed: list[tuple[Optional[str], Optional[str], list[ActionItem]]] = [
        (k, name, lst) for k, (name, lst) in order.items() if k is not None]
    if None in order:
        keyed.append((None, order[None][0], order[None][1]))
    return keyed


def _item_messages(meeting: Meeting, items: Sequence[ActionItem], o: RenderOptions) -> list[MessageSpec]:
    msgs: list[MessageSpec] = []
    for owner_id, owner_name, group in _groups(items):
        who = f"<@{owner_id}>" if owner_id else f"_{t('notes.unassigned', o.lang)}_"
        if owner_id and owner_name:
            who += f" ({owner_name})"
        for start in range(0, len(group), ITEMS_PER_MESSAGE):
            chunk = group[start:start + ITEMS_PER_MESSAGE]
            lines = [f"**{t('notes.action_items', o.lang)}** · {who}"]
            buttons: list[ButtonSpec] = []
            for row, item in enumerate(chunk):
                lines.append(_item_line(item))
                if item.quote:
                    lines.append(f"> {item.quote[:300]}")
                buttons += _item_buttons(meeting, item, row, o)
            content = "\n".join(lines)
            if len(content) > MESSAGE_LIMIT:
                content = content[:MESSAGE_LIMIT - 1] + "…"
            msgs.append(MessageSpec(content, tuple(buttons)))
    return msgs


def _bulk(meeting: Meeting, items: Sequence[ActionItem], o: RenderOptions) -> Optional[MessageSpec]:
    open_items = [i for i in items if i.status not in (ActionStatus.DELIVERED, ActionStatus.DISMISSED)]
    buttons: list[ButtonSpec] = []
    if o.kanban_on and any(o.is_owner_item(i) for i in open_items):
        buttons.append(ButtonSpec(t("ui.btn_all_kanban", o.lang), custom_id("allk", meeting.id, "all"), "success"))
    if o.linear_on and open_items:
        buttons.append(ButtonSpec(t("ui.btn_all_linear", o.lang), custom_id("alll", meeting.id, "all"), "primary"))
    if o.has_candidates:
        buttons.append(ButtonSpec(t("ui.btn_project", o.lang), custom_id("prj", meeting.id, "all"), "secondary",
                                  emoji="📁"))
    if not buttons:
        return None
    project = meeting.project or t("notes.unassigned", o.lang)
    return MessageSpec(t("ui.bulk_line", o.lang, project=project), tuple(buttons))


def render_notes(meeting: Meeting, notes: Notes, items: Sequence[ActionItem], o: RenderOptions) -> list[MessageSpec]:
    msgs = [MessageSpec(part) for part in split_text(_header(meeting, notes, o.lang))]
    msgs += _item_messages(meeting, items, o)
    bulk = _bulk(meeting, items, o)
    if bulk is not None:
        msgs.append(bulk)
    return msgs
