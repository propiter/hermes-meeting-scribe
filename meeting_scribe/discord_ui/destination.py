"""Where a meeting's notes go (DESIGN §19) — resolved on the gateway loop from the discord.py cache.

Order of the notes channel:

* Google Meet imports: ``google_meet_discord_channel`` → ``delivery_discord_channel`` → AUTOMATIC.
* Discord voice meetings: ``delivery_discord_channel`` → the voice channel's text chat → AUTOMATIC.
* Nothing usable → PENDING: the delivery waits (without using attempts) until a channel is set or
  appears; ``status``/``doctor`` show the exact command to run.

Channel settings hold an id or a NAME. A name is matched against the server's text channels after
removing decoration (emoji, ``#``, case, ``-``/``_``/spaces); ambiguous or unknown names are never
guessed — they are reported and skipped.

The server ("guild"): the meeting's own for Discord meetings; for imported meetings (no guild) the
one of a configured channel id, else ``delivery_discord_guild`` (id or name), else the bot's only
server. With several servers and nothing configured nothing is guessed.

AUTOMATIC, in that server: its system channel when the bot can post AND attach files there, else
the first text channel whose clean name is in ``delivery_auto_channel_names`` (list order, then
channel position) where the bot can post.

Never a DM: Hermes' home channel is no longer a fallback (it is often a DM with the owner, where
nobody else sees the notes and tasks cannot be routed); every candidate must belong to a server.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from ..config import Settings, channel_ref
from ..domain.models import SOURCE_GOOGLE_MEET, Meeting
from ..domain.names import clean_channel_name
from ..domain.text import fold, is_ascii_digits

REPORT_KV = "discord.destination_report"
TEXT_KINDS = frozenset({"text", "news"})
_SEP_RE = re.compile(r"[-_\s]+")


def norm_name(name: str) -> str:
    """Comparable form of a channel/server name: decoration removed, folded, separators unified."""
    cleaned = clean_channel_name(str(name or "").lstrip("#")) or str(name or "")
    return _SEP_RE.sub(" ", fold(cleaned)).strip()


def _kind(channel: Any) -> str:
    return str(getattr(channel, "type", "") or "").rsplit(".", 1)[-1]


def is_text(channel: Any) -> bool:
    return _kind(channel) in TEXT_KINDS


def _perms(channel: Any, member: Any) -> Any:
    try:
        return channel.permissions_for(member)
    except Exception:  # uncached member / partial object: let Discord decide
        return None


def can_send(channel: Any, member: Any, *, attach: bool = False) -> bool:
    p = _perms(channel, member)
    if p is None:
        return True
    ok = bool(getattr(p, "view_channel", False) and getattr(p, "send_messages", False))
    return ok and (not attach or bool(getattr(p, "attach_files", False)))


@dataclass(frozen=True)
class Resolved:
    """One setting (or step) of the resolution, for logs, ``doctor`` and ``config list``."""
    key: str
    value: str
    status: str  # ok | missing | ambiguous | no_guild | not_text | unset | none
    channel_id: Optional[str] = None
    channel_name: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}


@dataclass
class Destination:
    targets: list[str] = field(default_factory=list)  # notes channel candidates, in order
    guild: Any = None
    guild_source: str = ""  # meeting | channel | setting | only | none
    steps: list[Resolved] = field(default_factory=list)
    fallback_channel: Optional[str] = None  # tasks without project (None = the notes channel)
    problem: str = ""  # why nothing is usable (pending)

    def report(self) -> dict[str, Any]:
        g = self.guild
        return {"checked_at": time.time(),
                "guild": {"id": str(getattr(g, "id", "")) if g is not None else "", "name": getattr(g, "name", "") or "",
                          "source": self.guild_source},
                "steps": [s.to_dict() for s in self.steps], "targets": list(self.targets),
                "fallback_channel": self.fallback_channel or "", "problem": self.problem}


def _guilds(client: Any) -> list[Any]:
    return [g for g in (getattr(client, "guilds", None) or ()) if g is not None]


def pick_guild(client: Any, setting: str) -> tuple[Any, str, str]:
    """``(guild, source, problem)`` for meetings without a guild of their own."""
    guilds = _guilds(client)
    value = (setting or "").strip()
    if value:
        if is_ascii_digits(value):
            getter = getattr(client, "get_guild", None)
            g = getter(int(value)) if callable(getter) else None
            g = g or next((x for x in guilds if str(getattr(x, "id", "")) == value), None)
            return (g, "setting", "") if g is not None else (
                None, "none", f"delivery_discord_guild={value}: the bot is not in that server")
        hits = [g for g in guilds if norm_name(getattr(g, "name", "")) == norm_name(value)]
        if len(hits) == 1:
            return hits[0], "setting", ""
        return None, "none", (f"delivery_discord_guild={value!r}: several servers have that name; use its id"
                              if hits else f"delivery_discord_guild={value!r}: no server with that name")
    if len(guilds) == 1:
        return guilds[0], "only", ""
    if not guilds:
        return None, "none", "the bot is not in any Discord server"
    return None, "none", (f"the bot is in {len(guilds)} servers; set delivery_discord_guild "
                          "(`hermes meeting-scribe config set delivery_discord_guild \"<server name or id>\"`)")


def find_channel_by_name(guilds: Iterable[Any], name: str) -> tuple[list[Any], list[Any]]:
    """``(text matches, non-text matches)`` of ``name`` across ``guilds``."""
    wanted = norm_name(name)
    text: list[Any] = []
    other: list[Any] = []
    for g in guilds:
        for ch in list(getattr(g, "channels", None) or ()):
            if norm_name(getattr(ch, "name", "")) == wanted:
                (text if is_text(ch) else other).append(ch)
    return text, other


def resolve_setting(client: Any, key: str, value: str, guild: Any) -> Resolved:
    """A channel setting (id or name) → a text channel id; names are resolved in ``guild`` (or, when no
    server is chosen, across every server — accepted only when exactly one channel matches)."""
    kind, ref = channel_ref(value)
    if not kind:
        return Resolved(key, "", "unset")
    if kind == "id":
        return Resolved(key, ref, "ok", ref)
    scope = [guild] if guild is not None else _guilds(client)
    text, other = find_channel_by_name(scope, ref)
    if len(text) == 1:
        ch = text[0]
        return Resolved(key, ref, "ok", str(ch.id), str(getattr(ch, "name", "")))
    if len(text) > 1:
        return Resolved(key, ref, "ambiguous", detail=f"{len(text)} text channels are named like {ref!r}; "
                                                       f"use the channel id instead")
    if other:
        return Resolved(key, ref, "not_text", detail=f"#{ref} is not a text channel")
    where = f"server {getattr(guild, 'name', '') or getattr(guild, 'id', '')}" if guild is not None else "any server"
    return Resolved(key, ref, "missing", detail=f"no text channel named {ref!r} in {where}")


def auto_channel(guild: Any, names: Sequence[str]) -> Resolved:
    if guild is None:
        return Resolved("auto", "", "no_guild")
    me = getattr(guild, "me", None)
    system = getattr(guild, "system_channel", None)
    if system is not None and is_text(system) and can_send(system, me, attach=True):
        return Resolved("auto", "system_channel", "ok", str(system.id), str(getattr(system, "name", "")))
    wanted = [norm_name(n) for n in names if norm_name(n)]
    best: Optional[tuple[int, int, Any]] = None
    for ch in list(getattr(guild, "channels", None) or ()):
        n = norm_name(getattr(ch, "name", ""))
        if n not in wanted or not is_text(ch) or not can_send(ch, me):
            continue
        rank = (wanted.index(n), int(getattr(ch, "position", 0) or 0))
        if best is None or rank < best[:2]:
            best = (*rank, ch)
    if best is None:
        return Resolved("auto", ", ".join(names), "none",
                        detail="no system channel the bot can post in and no channel named "
                               + ", ".join(f"#{n}" for n in names))
    ch = best[2]
    return Resolved("auto", str(getattr(ch, "name", "")), "ok", str(ch.id), str(getattr(ch, "name", "")))


def _guild_of_channel(client: Any, cid: str) -> Any:
    get_channel = getattr(client, "get_channel", None)
    ch = get_channel(int(cid)) if callable(get_channel) and is_ascii_digits(cid) else None
    return getattr(ch, "guild", None)


def resolve(client: Any, meeting: Meeting, s: Settings) -> Destination:
    """Pure over the discord.py cache (runs on the gateway loop)."""
    d = Destination()
    imported = meeting.source == SOURCE_GOOGLE_MEET or not meeting.guild_id
    keys = (("google_meet_discord_channel", s.google_meet_discord_channel),) if meeting.source == SOURCE_GOOGLE_MEET else ()
    keys += (("delivery_discord_channel", s.delivery_discord_channel),)
    guild = None
    if not imported:
        getter = getattr(client, "get_guild", None)
        try:
            guild = getter(int(meeting.guild_id)) if callable(getter) else None
        except (TypeError, ValueError):
            guild = None
        d.guild_source = "meeting" if guild is not None else "none"
    else:
        for _key, value in keys:  # a configured channel id fixes the server
            kind, ref = channel_ref(value)
            if kind == "id":
                guild = _guild_of_channel(client, ref)
                if guild is not None:
                    d.guild_source = "channel"
                    break
        if guild is None:
            guild, d.guild_source, problem = pick_guild(client, s.delivery_discord_guild)
            if problem:
                d.steps.append(Resolved("delivery_discord_guild", s.delivery_discord_guild, "no_guild", detail=problem))
    d.guild = guild
    for key, value in keys:
        step = resolve_setting(client, key, value, guild)
        if step.status != "unset":
            d.steps.append(step)
        if step.channel_id and step.channel_id not in d.targets:
            d.targets.append(step.channel_id)
            if guild is None:  # a name found in exactly one server: that server
                guild = d.guild = _guild_of_channel(client, step.channel_id)
                d.guild_source = "channel" if guild is not None else d.guild_source
    if not imported:
        for cid in (meeting.text_channel_id, meeting.channel_id):
            if cid and is_ascii_digits(str(cid)) and str(cid) not in d.targets:
                d.targets.append(str(cid))
    auto = auto_channel(guild, s.delivery_auto_channel_names)
    d.steps.append(auto)
    if auto.channel_id and auto.channel_id not in d.targets:
        d.targets.append(auto.channel_id)
    fb = resolve_setting(client, "delivery_fallback_channel", s.delivery_fallback_channel, guild)
    if fb.status != "unset":
        d.steps.append(fb)
    d.fallback_channel = fb.channel_id
    if not d.targets:
        d.problem = pending_reason(meeting, d)
    return d


def pending_reason(meeting: Meeting, d: Destination) -> str:
    """The exact instruction shown by ``status``/``doctor`` while a delivery waits for a channel."""
    key = "google_meet_discord_channel" if meeting.source == SOURCE_GOOGLE_MEET else "delivery_discord_channel"
    problems = "; ".join(st.detail for st in d.steps if st.detail and st.status not in ("ok", "unset"))
    hint = f"`hermes meeting-scribe config set {key} \"#channel-name\"` (or a channel id)"
    return f"waiting for a Discord channel: {problems or 'no notes channel could be resolved'}. Set one with {hint}"
