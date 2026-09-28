"""Private meetings (``meeting_routes`` rules marked ``:private``, DESIGN §19.2) — pure, no discord.py.

A meeting is PRIVATE when a private rule matches it, or when it was already published as private (a
sticky ``KV_PRIVATE`` record: removing or editing the rule later never turns notes that were posted in a
private channel into public ones). Everything that could show a meeting outside its private channel asks
:func:`is_private` first: the Discord notes sink, the Kanban/Linear auto delivery, the agent tools and
the chat commands. Local files (the meeting folder, Obsidian) are not affected.

A private meeting is readable from chat only in its own place: the notes channel (or the forum post /
the thread holding its tasks), see :func:`allowed_places` — the CLI and Desktop always see it; a cron
job or any context without a known local source never does (:class:`Reader`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .domain.models import Meeting

KV_PRIVATE = "privacy.private."  # + meeting id -> {"rule": origin, "channel": notes channel id}
NOTES_POINTER = "mtg:{id}:notes"


def record(repo: Any, meeting_id: str) -> Optional[dict[str, Any]]:
    raw = repo.kv_get(KV_PRIVATE + meeting_id)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return {"rule": "", "channel": ""}  # damaged: still private (fail closed)
    return data if isinstance(data, dict) else {"rule": "", "channel": ""}


def anchor(repo: Any, meeting_id: str, channel: str) -> None:
    """Move a private meeting to ``channel`` (explicit admin action, CLI ``private-move``)."""
    current = record(repo, meeting_id) or {}
    repo.kv_set(KV_PRIVATE + meeting_id, json.dumps({"rule": current.get("rule", ""), "channel": channel}))


def remember(repo: Any, meeting_id: str, rule: str, channel: str) -> None:
    """Sticky: once private, always private. The channel is recorded once, when it becomes known, and
    from then on the meeting is anchored there: only :func:`anchor` (the admin) changes it."""
    current = record(repo, meeting_id) or {}
    wanted = {"rule": rule or current.get("rule", ""), "channel": current.get("channel") or channel or ""}
    if wanted != current:
        repo.kv_set(KV_PRIVATE + meeting_id, json.dumps(wanted))


def rule_for(settings: Any, meeting: Meeting) -> Any:
    from .routes import match_route

    return match_route(meeting, settings.routes())


def marks_private(rule: Any) -> bool:
    """A rule that makes a meeting private FOR GOOD: private and naming its origin. An unreadable
    private entry (``any``) only holds every meeting back until it is fixed."""
    return rule is not None and rule.private and rule.kind != "any"


def is_private(repo: Any, settings: Any, meeting: Meeting) -> bool:
    if record(repo, meeting.id) is not None:
        return True
    rule = rule_for(settings, meeting)
    return bool(rule is not None and rule.private)


def allowed_places(repo: Any, meeting: Meeting, settings: Any) -> set[str]:
    """Chat ids from where a private meeting may be read: its anchored channel (the recorded one) and
    the forum post or thread holding it. Only before the channel is recorded, the rule's channel id —
    editing the rule later never opens the meeting to another channel."""
    from .domain.text import is_ascii_digits

    out: set[str] = set()
    rec = record(repo, meeting.id) or {}
    if rec.get("channel"):
        out.add(str(rec["channel"]))
    else:
        rule = rule_for(settings, meeting)
        if marks_private(rule) and is_ascii_digits(rule.channel):
            out.add(rule.channel)
    row = repo.get_delivery("discord", NOTES_POINTER.format(id=meeting.id))
    if row and row.get("external_id"):
        try:
            ptr = json.loads(row["external_id"])
        except ValueError:
            ptr = {}
        ptr = ptr if isinstance(ptr, dict) else {}
        # the notes' post/thread count only when the notes are in the private channel itself (a copy
        # posted before the meeting became private is not a place to read it from)
        if {str(ptr.get(k)) for k in ("channel", "forum") if ptr.get(k)} & out:
            out |= {str(ptr[k]) for k in ("channel", "forum", "thread") if ptr.get(k)}
    return out


def visible_from(repo: Any, settings: Any, meeting: Meeting, places: Iterable[str]) -> bool:
    """May this meeting be shown in a chat whose ids (channel, thread, parent) are ``places``?"""
    if not is_private(repo, settings, meeting):
        return True
    here = {str(p) for p in places if p}
    return bool(here) and bool(here & allowed_places(repo, meeting, settings))


# The operator's own surfaces (DESIGN §19.2): the Hermes CLI/TUI in a terminal and Desktop. Hermes binds
# ``HERMES_SESSION_SOURCE`` for them; everything else — a cron job (empty platform and source), a
# webhook, the API server, an unknown or future surface — is NOT local and fails closed.
LOCAL_SOURCES = frozenset({"cli", "tui", "desktop"})


@dataclass(frozen=True)
class Reader:
    """Who asks, for the chat surfaces (agent tools, slash commands): the chat platform, the chat ids, the
    session source and whether it is a cron job. The default reader sees no private meeting."""
    platform: str = ""
    places: frozenset[str] = frozenset()
    source: str = ""
    cron: bool = False

    @classmethod
    def operator(cls) -> "Reader":
        """The operator at the terminal (the ``hermes meeting-scribe`` CLI, Desktop's own API)."""
        return cls(source="cli")

    @property
    def local(self) -> bool:
        return (not self.cron and not (self.platform or "").strip()
                and (self.source or "").strip().lower() in LOCAL_SOURCES)

    def may_read(self, repo: Any, settings: Any, meeting: Meeting) -> bool:
        """The operator (CLI, Desktop) sees everything; a Discord chat sees a private meeting only from its
        private channel; any other context (cron, other platforms, unknown) never sees one."""
        if self.local:
            return True
        if self.cron or (self.platform or "").lower() != "discord":
            return not is_private(repo, settings, meeting)
        return visible_from(repo, settings, meeting, self.places)
