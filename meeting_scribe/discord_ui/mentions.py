"""The participants line of a meeting's notes (DESIGN §19.4): the first message of the notes mentions
the humans of the meeting so they know the notes are there.

Who: :func:`privacy.people` — Discord speakers by id; Google Meet attendees mapped through the space's
person links; anyone else by name (inert text, never a ping). Never the bot. In a channel not everyone
sees (a private rule's channel, or any channel @everyone cannot view), only members who can view it
are mentioned: a member the bot cannot check is named instead (fail closed). The mention pings only
at the first publication: the message is sent with ``allowed_mentions`` listing exactly those users
(no @everyone/@here, no roles); every later edit or re-post carries the same text with no pings.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..domain.models import Meeting
from ..i18n import t
from ..privacy import people
from .render_tasks import safe_name

MAX_MENTIONS = 40  # a line stays well under a message; the rest is counted


@dataclass(frozen=True)
class Participants:
    line: str  # the text added under the notes title ("" = nothing to add)
    users: tuple[str, ...]  # the ids that line may ping


def _visible(channel: Any, uid: str) -> Optional[bool]:
    """Can member ``uid`` view ``channel``? ``None`` when the bot cannot tell (member not cached)."""
    guild = getattr(channel, "guild", None)
    getter = getattr(guild, "get_member", None)
    member = getter(int(uid)) if callable(getter) else None
    if member is None:
        return None
    perms = channel.permissions_for(member)
    return bool(getattr(perms, "view_channel", False))


def _everyone_sees(channel: Any) -> bool:
    target = getattr(channel, "parent", None) or channel  # a thread / forum post: its channel decides
    role = getattr(getattr(target, "guild", None), "default_role", None)
    if role is None:
        return False
    return bool(getattr(target.permissions_for(role), "view_channel", False))


def _bot_ids(adapter: Any, channel: Any) -> set[str]:
    client = getattr(adapter, "_client", None)
    ids = {getattr(getattr(client, "user", None), "id", None), getattr(getattr(getattr(channel, "guild", None), "me", None), "id", None)}
    return {str(i) for i in ids if i is not None}


def participants_line(repo: Any, meeting: Meeting, channel: Any, adapter: Any, *, private: bool,
                      lang: str) -> Participants:
    """The line for a NEW notes message in ``channel`` and the users it may ping."""
    bots = _bot_ids(adapter, channel)
    open_to_all = not private and _everyone_sees(channel)
    mentioned: list[str] = []
    named: list[str] = []
    for uid, name in people(repo, meeting):
        if uid in bots:
            continue
        if uid and len(mentioned) < MAX_MENTIONS:
            seen = _visible(channel, uid)
            if seen is True or (seen is None and open_to_all):
                mentioned.append(uid)
                continue
        named.append(safe_name(name))
    shown = [f"<@{u}>" for u in mentioned] + [n for n in named[:MAX_MENTIONS] if n]
    if not shown:
        return Participants("", ())
    extra = len(named) - len(named[:MAX_MENTIONS])
    text = ", ".join(shown) + (f" +{extra}" if extra > 0 else "")
    return Participants(f"-# 👥 {t('notes.participants', lang)}: {text}", tuple(mentioned))
