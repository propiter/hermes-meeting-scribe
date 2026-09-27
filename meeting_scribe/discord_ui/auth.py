"""Who may press which task button (DESIGN §16) — pure.

Buttons on a public message are visible to everyone, so every click is checked against the TASK:

* the task's assignee and the owners may act on it; anyone else is told whose task it is;
* an unassigned task: owners only;
* Kanban (``ok``) is the owners' personal board: owners only, and only on tasks assigned to an owner;
* meeting-wide legacy buttons from 0.1 messages (``allk`` owners; ``alll``/``psel``/``prj:all``
  owners or users Hermes authorizes) keep their 0.1 rule;
* ``mine``/``pg`` (the personal panel) are open to everyone: it only ever shows the clicker's tasks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..domain.models import ActionItem, is_discord_user_id
from ..i18n import t

TASK_ACTIONS = frozenset({"ok", "lin", "no", "prj", "tsel"})
OPEN_ACTIONS = frozenset({"mine", "pg"})
MEETING_OWNER_ONLY = frozenset({"allk"})
MEETING_ACTIONS = frozenset({"allk", "alll", "psel"})


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    message: str = ""


def check_task(action: str, item: Optional[ActionItem], user_id: str, owners: frozenset[str], lang: str,
               item_id: str = "") -> Verdict:
    if item is None:  # unknown / stale id: fail closed, nothing to act on
        return Verdict(False, t("tasks.unknown", lang, item=item_id or "?"))
    assignee = item.owner_speaker_id or None
    is_owner = user_id in owners
    if action == "ok":
        if not is_owner:
            return Verdict(False, t("ui.owner_only", lang))
        if assignee not in owners:
            return Verdict(False, t("tasks.kanban_owner_tasks", lang))
        return Verdict(True)
    if is_owner or (assignee is not None and assignee == user_id):
        return Verdict(True)
    if assignee is None:
        return Verdict(False, t("tasks.owners_only_unassigned", lang))
    who = f"<@{assignee}>" if is_discord_user_id(assignee) else (item.owner_name or assignee)
    return Verdict(False, t("tasks.belongs_to", lang, user=who))

