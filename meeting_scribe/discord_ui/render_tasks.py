"""Task-centred Discord rendering (DESIGN §16) — pure, no discord.py import.

* :func:`render_task` — ONE message per task: the task text with its own button row directly under
  it (✅ Kanban only on owner tasks · 🟣 Linear · ❌ Dismiss · 📁 Move). A finished task shows the
  result (``✅ Kanban `t_42```, ``🟣 Linear ENG-7``, ``❌ Dismissed``) and has no buttons, so
  approving one task can never shift the buttons of another.
* :func:`render_index` — the compact block for the meeting chat: counts per project (with a link
  to the thread holding them) and per person, delivery problems, and ONE "📋 My tasks" button.
* :func:`render_panel` — the per-user panel (ephemeral reply or DM): the user's tasks, each as a
  text block followed by its button row, paginated so a page never exceeds Discord's limits
  (5 rows: 4 tasks + navigation; 40 components; 4000 display characters in a components-v2 view).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from ..domain.models import ActionItem, ActionStatus, Meeting, is_discord_user_id
from ..i18n import t
from ..storage.artifacts import fmt_ts
from .render import MESSAGE_LIMIT, ButtonSpec, MessageSpec, RenderOptions, custom_id
from .routing import Route

TASKS_PER_PAGE = 4
PANEL_TEXT_LIMIT = 3800
QUOTE_LIMIT = 240
_ICON = {ActionStatus.PENDING: "▫️", ActionStatus.APPROVED: "☑️", ActionStatus.DELIVERED: "✅",
         ActionStatus.DISMISSED: "❌"}
_FINISHED = (ActionStatus.DELIVERED, ActionStatus.DISMISSED)


@dataclass(frozen=True)
class Sharing:
    """Where a task of a PRIVATE meeting was shared (DESIGN §19.2); ``None`` on a normal meeting."""
    dm: bool = False  # sent to its assignee
    channel: str = ""  # project channel it was published in ("" = not published)
    target: str = ""  # the project channel it would be published in ("" = none: stays private or 📁)
    target_name: str = ""
    can_dm: bool = False  # a Discord assignee and ``delivery_dm_assignees`` on


@dataclass(frozen=True)
class TaskView:
    item: ActionItem
    route: Route
    refs: Mapping[str, str] = field(default_factory=dict)  # sink -> "t_42" / "ENG-7"
    sharing: Optional[Sharing] = None


@dataclass(frozen=True)
class PanelBlock:
    item_id: str
    content: str
    buttons: tuple[ButtonSpec, ...] = ()


@dataclass(frozen=True)
class TaskPanel:
    header: str
    blocks: tuple[PanelBlock, ...]
    nav: tuple[ButtonSpec, ...]
    page: int
    pages: int


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


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:max(0, limit - 1)].rstrip() + "…"


def _who(item: ActionItem, lang: str) -> str:
    if not item.owner_speaker_id:
        return f"_{t('notes.unassigned', lang)}_"
    if not is_discord_user_id(item.owner_speaker_id):  # imported speaker (Google Meet): no mention
        return f"**{safe_name(item.owner_name or item.owner_speaker_id)}**"
    return f"<@{item.owner_speaker_id}>" + (f" ({safe_name(item.owner_name)})" if item.owner_name else "")


def _result(view: TaskView, lang: str) -> list[str]:
    out = []
    if view.refs.get("kanban"):
        out.append(f"✅ Kanban `{view.refs['kanban']}`")
    if view.refs.get("linear"):
        out.append(f"🟣 Linear {view.refs['linear']}")
    if view.item.status is ActionStatus.DISMISSED:
        out.append(f"❌ {t('tasks.dismissed', lang)}")
    return out


def task_text(meeting: Meeting, view: TaskView, lang: str, *, meeting_ref: bool = True) -> str:
    item = view.item
    title = f"~~{item.title}~~" if item.status is ActionStatus.DISMISSED else f"**{item.title}**"
    extra = ([f"📅 {item.due}"] if item.due else []) + ([f"[{fmt_ts(item.t0)}]"] if item.t0 is not None else [])
    lines = [f"{_ICON[item.status]} {title}" + (f" — {' · '.join(extra)}" if extra else "")]
    project = view.route.project or t("tasks.no_project", lang)
    lines.append(f"👤 {_who(item, lang)} · 📁 {project}")
    if view.route.uncertain and item.status not in _FINISHED:
        lines.append(f"⚠️ {t('tasks.uncertain', lang)}")
    if item.description and item.description != item.title:
        lines.append(_clip(item.description, QUOTE_LIMIT))
    if item.quote:
        lines.append(f"> {_clip(item.quote, QUOTE_LIMIT)}")
    lines += _result(view, lang)
    if meeting_ref:
        lines.append(f"-# 🎙️ {meeting.title or meeting.channel_name} · {meeting.started_at:%Y-%m-%d} · `{meeting.id}`")
    return "\n".join(lines)


def task_buttons(meeting: Meeting, view: TaskView, o: RenderOptions) -> tuple[ButtonSpec, ...]:
    item = view.item
    if item.status in _FINISHED:
        return ()
    out: list[ButtonSpec] = []
    if o.kanban_on and o.is_owner_item(item):
        out.append(ButtonSpec(t("ui.btn_kanban", o.lang), custom_id("ok", meeting.id, item.id), "success", 0, "✅"))
    if o.linear_on:
        out.append(ButtonSpec(t("ui.btn_linear", o.lang), custom_id("lin", meeting.id, item.id), "primary", 0, "🟣"))
    out.append(ButtonSpec(t("ui.btn_dismiss", o.lang), custom_id("no", meeting.id, item.id), "secondary", 0, "❌"))
    if o.has_candidates:
        out.append(ButtonSpec(t("ui.btn_move", o.lang), custom_id("prj", meeting.id, item.id), "secondary", 0, "📁"))
    return tuple(out)


def render_task(meeting: Meeting, view: TaskView, o: RenderOptions) -> MessageSpec:
    if view.sharing is not None:
        return render_private_task(meeting, view, o)
    return MessageSpec(_clip(task_text(meeting, view, o.lang), MESSAGE_LIMIT), task_buttons(meeting, view, o))


# -- private meetings (DESIGN §19.2) ---------------------------------------------------------------
def share_lines(view: TaskView, lang: str) -> list[str]:
    s = view.sharing
    if s is None:
        return []
    done = ([f"✉️ {t('share.sent_to', lang, user=f'<@{view.item.owner_speaker_id}>')}"] if s.dm else [])
    done += [f"📣 {t('share.published_in', lang, channel=f'<#{s.channel}>')}"] if s.channel else []
    return done or [f"🔒 {t('share.not_shared', lang)}"]


def share_buttons(meeting: Meeting, view: TaskView, lang: str) -> tuple[ButtonSpec, ...]:
    s, item = view.sharing, view.item
    if s is None or item.status is ActionStatus.DISMISSED:
        return ()
    out: list[ButtonSpec] = []
    if s.can_dm and not s.dm:
        who = _clip(" ".join(str(item.owner_name or "").split()) or t("share.assignee", lang), 40)
        out.append(ButtonSpec(t("ui.btn_share_dm", lang, name=who), custom_id("shd", meeting.id, item.id),
                              "primary", 1, "✉️"))
    if s.target and s.channel != s.target:
        name = _clip(s.target_name or s.target, 40)
        out.append(ButtonSpec(t("ui.btn_share_project", lang, channel=name), custom_id("shp", meeting.id, item.id),
                              "primary", 1, "📣"))
    return tuple(out)


def render_private_task(meeting: Meeting, view: TaskView, o: RenderOptions) -> MessageSpec:
    """The task in the private channel: its full text, where it was shared, and its buttons (the usual
    ones on the first row, the share ones on the second)."""
    text = "\n".join([task_text(meeting, view, o.lang), *share_lines(view, o.lang)])
    return MessageSpec(_clip(text, MESSAGE_LIMIT), task_buttons(meeting, view, o) + share_buttons(meeting, view, o.lang))


def shared_task_text(meeting: Meeting, view: TaskView, lang: str) -> str:
    """What leaves a private meeting: the task only — title, description, assignee, due date, project.
    Never the summary, the quote, the meeting title or a link to the private channel."""
    item = view.item
    title = f"~~{item.title}~~" if item.status is ActionStatus.DISMISSED else f"**{item.title}**"
    lines = [f"{_ICON[item.status]} {title}" + (f" — 📅 {item.due}" if item.due else "")]
    lines.append(f"👤 {_who(item, lang)}" + (f" · 📁 {view.route.project}" if view.route.project else ""))
    if item.description and item.description != item.title:
        lines.append(_clip(item.description, QUOTE_LIMIT))
    if item.status is ActionStatus.DISMISSED:
        lines.append(f"❌ {t('tasks.dismissed', lang)}")
    lines.append(f"-# {t('share.footer', lang, date=f'{meeting.started_at:%Y-%m-%d}')}")
    return "\n".join(lines)


def render_shared_task(meeting: Meeting, view: TaskView, lang: str) -> MessageSpec:
    """A shared copy (project channel): text only, no buttons — decisions stay in the private channel."""
    return MessageSpec(_clip(shared_task_text(meeting, view, lang), MESSAGE_LIMIT))


def render_shared_dm(meeting: Meeting, view: TaskView, lang: str) -> MessageSpec:
    """One task of a private meeting sent to its assignee: a header and the task only."""
    header = f"### 📋 {t('share.dm_title', lang)}"
    return MessageSpec(_clip("\n".join([header, shared_task_text(meeting, view, lang)]), MESSAGE_LIMIT))


# -- index -----------------------------------------------------------------------------------------
def _project_lines(views: Sequence[TaskView], threads: Mapping[Optional[str], str], lang: str) -> list[str]:
    groups: dict[tuple[Optional[str], str, str], list[TaskView]] = {}
    for v in views:
        r = v.route
        label = r.project if r.channel_id or r.reason == "no_permission" else t("tasks.no_project", lang)
        groups.setdefault((r.channel_id, r.reason if r.reason == "no_permission" else "", label or ""), []).append(v)
    lines = []
    for (cid, problem, label), group in groups.items():
        done = sum(v.item.status in _FINISHED for v in group)
        line = f"- **{label}** — {len(group)}" + (f" ({t('tasks.done_count', lang, count=done)})" if done else "")
        if any(v.route.uncertain for v in group):
            line += " ⚠️"
        target = threads.get(cid) or (threads.get(None) if cid is None else None)
        if target:
            line += f" · <#{target}>"
        if problem:
            line += f" · ⛔ {t('tasks.no_permission', lang, channel=f'<#{group[0].route.wanted_channel_id}>')}"
        lines.append(line)
    return lines


def _person_lines(views: Sequence[TaskView], lang: str) -> list[str]:
    people: dict[Optional[str], int] = {}
    names: dict[str, str] = {}
    for v in views:
        people[v.item.owner_speaker_id or None] = people.get(v.item.owner_speaker_id or None, 0) + 1
        if v.item.owner_speaker_id and v.item.owner_name:
            names.setdefault(v.item.owner_speaker_id, v.item.owner_name)
    lines = [f"- <@{uid}> — {n}" if is_discord_user_id(uid) else f"- **{safe_name(names.get(uid, uid))}** — {n}"
             for uid, n in people.items() if uid]
    if None in people:
        lines.append(f"- {t('notes.unassigned', lang)} — {people[None]}")
    return lines


def _private_lines(views: Sequence[TaskView], lang: str) -> list[str]:
    shared = sum(1 for v in views if v.sharing is not None and (v.sharing.dm or v.sharing.channel))
    return [f"🔒 {t('share.index_private', lang)}", f"-# {t('share.index_count', lang, shared=shared, total=len(views))}"]


def _shareable(view: TaskView) -> bool:
    s = view.sharing
    if s is None or view.item.status is ActionStatus.DISMISSED:
        return False
    return (s.can_dm and not s.dm) or bool(s.target and s.channel != s.target)


def render_index(meeting: Meeting, views: Sequence[TaskView], threads: Mapping[Optional[str], str],
                 dm_failed: Sequence[str], o: RenderOptions, *, private: bool = False) -> MessageSpec:
    lang = o.lang
    lines = [f"## 📋 {t('tasks.index_title', lang)} · {len(views)}"]
    if private:
        lines += _private_lines(views, lang)
    if views:
        lines += [f"**{t('tasks.by_project', lang)}**", *_project_lines(views, threads, lang),
                  f"**{t('tasks.by_person', lang)}**", *_person_lines(views, lang)]
    else:
        lines.append(t("notes.none", lang))
    if dm_failed:
        lines.append(f"✉️ {t('tasks.dm_failed', lang, users=', '.join(f'<@{u}>' for u in dm_failed))}")
    buttons = (ButtonSpec(t("ui.btn_my_tasks", lang), custom_id("mine", meeting.id, "all"), "primary", 0, "📋"),)
    if private and any(_shareable(v) for v in views):
        buttons += (ButtonSpec(t("ui.btn_share_all", lang), custom_id("sha", meeting.id, "all"), "secondary", 0, "📤"),)
    return MessageSpec(_clip("\n".join(lines), MESSAGE_LIMIT), buttons if views else ())


# -- panel -----------------------------------------------------------------------------------------
def render_panel(meeting: Meeting, views: Sequence[TaskView], *, user_id: str, scope: str, page: int,
                 o: RenderOptions, is_owner: bool, link: str = "") -> TaskPanel:
    """``link``: where the meeting's notes are (the DM panel points to the notes message or forum post)."""
    lang = o.lang
    scope = "a" if scope == "a" and is_owner else "m"
    mine = [v for v in views if scope == "a" or (v.item.owner_speaker_id or "") == str(user_id)]
    pages = max(1, math.ceil(len(mine) / TASKS_PER_PAGE))
    page = min(max(0, page), pages - 1)
    title = meeting.title or meeting.channel_name
    key = "tasks.panel_all" if scope == "a" else "tasks.panel_mine"
    header = f"### 📋 {t(key, lang, title=title)}"
    if link:
        header += f"\n-# 🔗 {t('tasks.panel_link', lang, url=link)}"
    if not mine:
        header += f"\n{t('tasks.panel_empty', lang)}"
    elif pages > 1:
        header += f"\n-# {t('tasks.page', lang, page=page + 1, pages=pages)}"
    budget = (PANEL_TEXT_LIMIT - len(header)) // TASKS_PER_PAGE
    blocks = tuple(PanelBlock(v.item.id, _clip(task_text(meeting, v, lang, meeting_ref=False), budget),
                              task_buttons(meeting, v, o))
                   for v in mine[page * TASKS_PER_PAGE:(page + 1) * TASKS_PER_PAGE])
    nav: list[ButtonSpec] = []
    if page > 0:
        nav.append(ButtonSpec(t("ui.btn_prev", lang), custom_id("pg", meeting.id, f"{scope}{page - 1}"), "secondary",
                              0, "◀️"))
    if page < pages - 1:
        nav.append(ButtonSpec(t("ui.btn_next", lang), custom_id("pg", meeting.id, f"{scope}{page + 1}"), "secondary",
                              0, "▶️"))
    if is_owner:
        other, label = ("m", "ui.btn_mine_only") if scope == "a" else ("a", "ui.btn_all_tasks")
        nav.append(ButtonSpec(t(label, lang), custom_id("pg", meeting.id, f"{other}0"), "secondary", 0, "👥"))
    return TaskPanel(header, blocks, tuple(nav), page, pages)
