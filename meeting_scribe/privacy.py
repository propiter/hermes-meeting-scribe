"""Private meetings (``meeting_routes`` rules marked ``:private``, DESIGN §19.2) — pure, no discord.py.

A meeting is PRIVATE when a private rule matches it, or when it was already published as private (a
sticky ``KV_PRIVATE`` record: removing or editing the rule later never turns notes that were posted in a
private channel into public ones). Everything that could show a meeting outside its private channel asks
:func:`is_private` first: the Discord notes sink, the Kanban/Linear auto delivery, the agent tools and
the chat commands. Local files (the meeting folder, Obsidian) are not affected.

A private meeting is readable from chat only in its own place: the notes channel (or the forum post /
the thread holding its tasks), see :func:`allowed_places` — the CLI and Desktop always see it; a cron
job or any context without a known local source never does (:class:`Reader`).

A DIRECT-MESSAGES meeting (a ``:dm`` rule, DESIGN §19.3) is private too, with no channel at all: its
record says ``mode: dm`` and, once delivered, lists its ``recipients`` (the participants it was sent
to, fixed from then on: the anchor). Its only places are those participants' DM channels.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .domain.models import Meeting

KV_PRIVATE = "privacy.private."  # + meeting id -> {"rule", "channel"} or {"rule", "mode": "dm", "recipients"}
NOTES_POINTER = "mtg:{id}:notes"
DM_COPY_PREFIX = "pdm:"  # + user id + ":" + suffix: a participant's copy of a direct-messages meeting


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


def remember(repo: Any, meeting_id: str, rule: str, channel: str, *, dm: bool = False) -> None:
    """Sticky: once private, always private. The channel is recorded once, when it becomes known, and
    from then on the meeting is anchored there: only :func:`anchor` (the admin) changes it. ``dm``: a
    ``:dm`` rule matched a meeting not recorded yet — it is a direct-messages meeting from now on, and
    such a meeting never gets a channel this way."""
    current = record(repo, meeting_id) or {}
    wanted = {**current, "rule": rule or current.get("rule", ""), "channel": current.get("channel") or channel or ""}
    if dm and not current:
        wanted.update(mode="dm", recipients=[])
    if is_dm_record(wanted):
        wanted["channel"] = ""
    if wanted != current:
        repo.kv_set(KV_PRIVATE + meeting_id, json.dumps(wanted))


def is_dm_record(rec: Optional[dict[str, Any]]) -> bool:
    """The record says direct messages only (its ``recipients`` are fixed once it is delivered)."""
    return bool(rec) and rec.get("mode") == "dm"


def anchor_dm(repo: Any, meeting_id: str, rule: str, recipients: Iterable[str]) -> list[str]:
    """Anchor a meeting to direct messages with ``recipients`` (its first delivery, DESIGN §19.3) and
    return the recipients it is anchored to. Sticky like :func:`remember`: an existing DM anchor keeps
    its list, and a meeting already anchored to a channel is never turned into a DM meeting here."""
    current = record(repo, meeting_id) or {}
    if is_dm_record(current) and current.get("recipients"):
        return [str(u) for u in current["recipients"]]
    if current.get("channel"):
        raise ValueError(f"meeting {meeting_id} is anchored to channel {current['channel']}")
    people = [str(u) for u in dict.fromkeys(recipients)]
    repo.kv_set(KV_PRIVATE + meeting_id, json.dumps({"rule": rule or current.get("rule", ""), "channel": "",
                                                     "mode": "dm", "recipients": people}))
    return people


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


def is_dm(repo: Any, settings: Any, meeting: Meeting) -> bool:
    """Delivered by direct message only: anchored so, or — before any anchor — a ``:dm`` rule matches.
    A meeting anchored to a private channel stays a channel meeting whatever the rule says now."""
    rec = record(repo, meeting.id)
    if is_dm_record(rec):
        return True
    if rec is not None and rec.get("channel"):
        return False
    rule = rule_for(settings, meeting)
    return bool(rule is not None and getattr(rule, "dm", False))


DM_UNREACHABLE_KV = "privacy.dm_unreachable."  # + meeting id -> who did not get the copy, and how to fix it


def participants(repo: Any, meeting: Meeting) -> tuple[list[str], list[str]]:
    """``(discord user ids, names that could not be mapped)`` of the humans of a meeting (see
    :func:`people`)."""
    found: list[str] = []
    unmapped: list[str] = []
    for uid, name in people(repo, meeting):
        (found if uid else unmapped).append(uid or name)
    return list(dict.fromkeys(found)), unmapped


def people(repo: Any, meeting: Meeting) -> list[tuple[str, str]]:
    """``[(discord user id or "", display name)]`` of the humans of a meeting, one per person: who spoke
    or was in the call (``Meeting.human_speakers``). An imported speaker (Google Meet, ``gmeet:…``) gets
    an id when exactly one person link of the meeting's space (``/meeting link``) matches its name or
    email; otherwise only its name."""
    from .domain.models import is_discord_user_id
    from .domain.text import fold

    links = repo.list_links(meeting.space) if hasattr(repo, "list_links") else []
    by_name: dict[str, set[str]] = {}
    for link in links:
        for value in (link.get("name"), link.get("email")):
            if value and str(value).strip():
                by_name.setdefault(fold(" ".join(str(value).split())), set()).add(str(link["discord_user_id"]))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for sp in meeting.human_speakers:
        name = str(sp.name or sp.user_id)
        if is_discord_user_id(sp.user_id):
            uid = str(sp.user_id)
        else:
            hits = by_name.get(fold(" ".join(str(sp.name or "").split())), set())
            uid = next(iter(hits)) if len(hits) == 1 else ""
        if uid and uid in seen:
            continue
        seen.add(uid)
        out.append((uid, name))
    return out


def dm_copies(repo: Any, meeting_id: str) -> dict[str, str]:
    """``{recipient: DM channel}`` of the anchored recipients' delivered copies (nobody else's)."""
    people = {str(u) for u in (record(repo, meeting_id) or {}).get("recipients") or ()}
    return {uid: cid for uid, cid in dm_channels(repo, meeting_id).items() if uid in people}


def dm_channels(repo: Any, meeting_id: str) -> dict[str, str]:
    """``{user id: DM channel id}`` of the copies of a direct-messages meeting delivered so far."""
    prefix = f"mtg:{meeting_id}:{DM_COPY_PREFIX}"
    out: dict[str, str] = {}
    for row in repo.list_deliveries(meeting_id, sink="discord", prefix=prefix):
        uid, _, suffix = str(row["key"])[len(prefix):].partition(":")
        if suffix != "notes" or not row.get("external_id"):
            continue
        try:
            ptr = json.loads(row["external_id"])
        except ValueError:
            continue
        if isinstance(ptr, dict) and ptr.get("channel"):
            out[uid] = str(ptr["channel"])
    return out


def allowed_places(repo: Any, meeting: Meeting, settings: Any) -> set[str]:
    """Chat ids from where a private meeting may be read: its anchored channel (the recorded one) and
    the forum post or thread holding it. Only before the channel is recorded, the rule's channel id —
    editing the rule later never opens the meeting to another channel."""
    from .domain.text import is_ascii_digits

    out: set[str] = set()
    rec = record(repo, meeting.id) or {}
    if is_dm_record(rec):  # only the DMs of the participants it was sent to (DESIGN §19.3)
        return set(dm_copies(repo, meeting.id).values())
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
