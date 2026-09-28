"""Who may press which task button (DESIGN §16) — pure.

Buttons on a public message are visible to everyone, so every click is checked against the TASK:

* the task's assignee and the owners may act on it; anyone else is told whose task it is;
* an unassigned task: owners only;
* Kanban (``ok``) is the owners' personal board: owners only, and only on tasks assigned to an owner;
* meeting-wide legacy buttons from 0.1 messages (``allk`` owners; ``alll``/``psel``/``prj:all``
  owners or users Hermes authorizes) keep their 0.1 rule;
* ``mine``/``pg`` (the personal panel) are open to everyone: it only ever shows the clicker's tasks.

A DIRECT-MESSAGES meeting (DESIGN §19.3) is stricter: a button works only in the direct message the
bot sent to the clicker for that meeting, and only on the clicker's own tasks — nobody acts on (or
shares) someone else's task, owners included; everyone already has the whole meeting. ``shp`` (share
MY task in its project channel) is the only share button; Kanban keeps its owners-only rule; 📁 Move
is not offered nor accepted (it would pick among the bot's channels, private ones included).

A PRIVATE meeting (DESIGN §19.2) adds one gate to every button: the click must come from the meeting's
private channel (or its thread / forum post) and the clicker must be able to see that channel. The share
buttons (``shd`` to the assignee, ``shp`` to the project channel, ``sha``/``shc`` share everything) are
open to every such member: deciding what leaves the room is the room's decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..domain.models import ActionItem, is_discord_user_id
from ..i18n import t
from .render_tasks import safe_name

TASK_ACTIONS = frozenset({"ok", "lin", "no", "prj", "tsel"})
# A direct-messages meeting never lives in a channel: no 📁 Move (``prj``/``tsel``) there (DESIGN §19.3).
DM_TASK_ACTIONS = frozenset({"ok", "lin", "no", "shp"})
SHARE_ACTIONS = frozenset({"shd", "shp", "sha", "shc"})
OPEN_ACTIONS = frozenset({"mine", "pg"})
MEETING_OWNER_ONLY = frozenset({"allk"})
MEETING_ACTIONS = frozenset({"allk", "alll", "psel"})


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    message: str = ""


def _ids(channel: Any) -> set[str]:
    if channel is None:
        return set()
    parent = getattr(channel, "parent_id", None) or getattr(getattr(channel, "parent", None), "id", None)
    return {str(x) for x in (getattr(channel, "id", None), parent) if x is not None}


def can_view(interaction: Any) -> bool:
    """The clicker can see the channel the button is in (unknown permissions: no)."""
    perms = getattr(interaction, "permissions", None)
    if perms is None:
        channel, user = getattr(interaction, "channel", None), getattr(interaction, "user", None)
        try:
            perms = channel.permissions_for(user) if channel is not None and user is not None else None
        except Exception:  # partial objects: fail closed
            perms = None
    return bool(getattr(perms, "view_channel", False))


def check_private(interaction: Any, place: set[str], lang: str) -> Verdict:
    """A button of a private meeting: pressed inside its private channel by someone who can see it."""
    if getattr(interaction, "guild", None) is None or not (_ids(getattr(interaction, "channel", None)) & place):
        return Verdict(False, t("share.only_in_private", lang))
    if not can_view(interaction):
        return Verdict(False, t("share.not_member", lang))
    return Verdict(True)


def check_dm(interaction: Any, recipients: dict[str, str], action: str, item: Optional[ActionItem],
             lang: str) -> Verdict:
    """A button of a direct-messages meeting: pressed in the clicker's own copy (``recipients``: user id ->
    their DM channel), on a task assigned to the clicker. Meeting-wide buttons are refused."""
    uid = str(getattr(getattr(interaction, "user", None), "id", "") or "")
    here = _ids(getattr(interaction, "channel", None)) | ({str(interaction.channel_id)}
                                                         if getattr(interaction, "channel_id", None) else set())
    if getattr(interaction, "guild", None) is not None or not uid or recipients.get(uid) not in here:
        return Verdict(False, t("dm.only_own_copy", lang))
    if action in OPEN_ACTIONS:
        return Verdict(True)
    if action not in DM_TASK_ACTIONS or item is None:
        return Verdict(False, t("dm.only_own_tasks", lang))
    if str(item.owner_speaker_id or "") != uid:
        return Verdict(False, t("dm.only_own_tasks", lang))
    return Verdict(True)


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
    who = f"<@{assignee}>" if is_discord_user_id(assignee) else safe_name(item.owner_name or assignee)
    return Verdict(False, t("tasks.belongs_to", lang, user=who))

