"""Where a meeting's notes go (DESIGN §19) — resolved on the gateway loop from the discord.py cache.

Order of the notes channel:

* Google Meet imports: ``google_meet_discord_channel`` → ``delivery_discord_channel`` → AUTOMATIC.
* Discord voice meetings: ``delivery_discord_channel`` → the voice channel's text chat → AUTOMATIC.
* Nothing usable → PENDING: the delivery waits (without using attempts) until a channel is set or
  appears; ``status``/``doctor`` show the exact command to run.

Channel settings hold an id or a NAME. A name is matched against the server's text channels after
removing decoration (emoji, ``#``, case, ``-``/``_``/spaces); ambiguous or unknown names are never
guessed — they are reported and skipped.

The server ("guild"): the meeting's own for Discord meetings (not cached → PENDING, never another
server); for imported meetings (no guild) the one of a configured channel id, else
``delivery_discord_guild`` (id or name; configured but unresolvable → PENDING), else the bot's only
server. With several servers and nothing configured nothing is guessed. Configured ids of a DM or of
another server are ignored and reported.

AUTOMATIC, in that server: its system channel when the bot can post AND attach files there, else
the first text channel whose clean name is in ``delivery_auto_channel_names`` (list order, then
channel position) where the bot can post. Never NSFW, never hidden from ``@everyone``, and nothing
while the bot's member is not cached (permissions unknown).

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
EXPLICIT_KEYS = ("google_meet_discord_channel", "delivery_discord_channel")
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
    if member is None:
        return None
    try:
        return channel.permissions_for(member)
    except Exception:  # uncached member / partial object
        return None


def can_send(channel: Any, member: Any, *, attach: bool = False) -> bool:
    """Known to be allowed. Unknown permissions (member not cached) are NOT a yes: the automatic
    choice must not guess (review M3); explicit settings do not go through this check."""
    p = _perms(channel, member)
    if p is None:
        return False
    ok = bool(getattr(p, "view_channel", False) and getattr(p, "send_messages", False))
    return ok and (not attach or bool(getattr(p, "attach_files", False)))


def is_public(channel: Any) -> bool:
    """``@everyone`` can see it. Unknown (no default role cached) counts as visible: nothing to warn."""
    role = getattr(getattr(channel, "guild", None), "default_role", None)
    if role is None:
        return True
    try:
        p = channel.permissions_for(role)
    except Exception:
        return True
    return bool(getattr(p, "view_channel", True))


def is_nsfw(channel: Any) -> bool:
    return bool(getattr(channel, "nsfw", False))


def _guild_label(guild: Any) -> str:
    return str(getattr(guild, "name", "") or getattr(guild, "id", ""))


@dataclass(frozen=True)
class Resolved:
    """One setting (or step) of the resolution, for logs, ``doctor`` and ``config list``."""
    key: str
    value: str
    status: str  # ok | missing | ambiguous | no_guild | not_text | not_in_server | other_guild | not_loaded | unset | none
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
    warnings: list[str] = field(default_factory=list)  # usable but worth a look (doctor)

    def report(self) -> dict[str, Any]:
        g = self.guild
        return {"checked_at": time.time(),
                "guild": {"id": str(getattr(g, "id", "")) if g is not None else "", "name": getattr(g, "name", "") or "",
                          "source": self.guild_source},
                "steps": [s.to_dict() for s in self.steps], "targets": list(self.targets),
                "fallback_channel": self.fallback_channel or "", "problem": self.problem,
                "warnings": list(self.warnings)}


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
    """The AUTOMATIC notes channel: never NSFW, never a channel ``@everyone`` cannot see (a private
    channel is used only when configured explicitly), and only where the bot is KNOWN to be able to
    post — with its member not cached yet nothing is chosen (review M2/M3)."""
    if guild is None:
        return Resolved("auto", "", "no_guild")
    me = getattr(guild, "me", None)
    if me is None:
        return Resolved("auto", "", "not_loaded",
                        detail=f"server {_guild_label(guild)} is not loaded yet (the bot's member is not cached)")
    skipped: list[str] = []

    def usable(ch: Any, *, attach: bool = False) -> bool:
        if not is_text(ch) or not can_send(ch, me, attach=attach):
            return False
        if is_nsfw(ch) or not is_public(ch):
            skipped.append(f"#{getattr(ch, 'name', '')}")
            return False
        return True
    system = getattr(guild, "system_channel", None)
    if system is not None and usable(system, attach=True):
        return Resolved("auto", "system_channel", "ok", str(system.id), str(getattr(system, "name", "")))
    wanted = [norm_name(n) for n in names if norm_name(n)]
    best: Optional[tuple[int, int, Any]] = None
    for ch in list(getattr(guild, "channels", None) or ()):
        n = norm_name(getattr(ch, "name", ""))
        if n not in wanted or not usable(ch):
            continue
        rank = (wanted.index(n), int(getattr(ch, "position", 0) or 0))
        if best is None or rank < best[:2]:
            best = (*rank, ch)
    if best is None:
        detail = ("no system channel the bot can post in and no channel named "
                  + ", ".join(f"#{n}" for n in names))
        if skipped:
            detail += (f" (skipped as private or NSFW: {', '.join(dict.fromkeys(skipped))}; "
                       "configure one explicitly to use it)")
        return Resolved("auto", ", ".join(names), "none", detail=detail)
    ch = best[2]
    return Resolved("auto", str(getattr(ch, "name", "")), "ok", str(ch.id), str(getattr(ch, "name", "")))


def _cached_channel(client: Any, cid: str) -> Any:
    get_channel = getattr(client, "get_channel", None)
    try:
        return get_channel(int(cid)) if callable(get_channel) and is_ascii_digits(cid) else None
    except Exception:
        return None


def _guild_of_channel(client: Any, cid: str) -> Any:
    return getattr(_cached_channel(client, cid), "guild", None)


def same_guild(a: Any, b: Any) -> bool:
    return a is not None and b is not None and str(getattr(a, "id", "")) == str(getattr(b, "id", ""))


def _check_id(client: Any, step: Resolved, guild: Any) -> Resolved:
    """A configured channel ID must be a server channel of ``guild`` (never a DM, never another server)."""
    if step.status != "ok" or not step.channel_id:
        return step
    ch = _cached_channel(client, step.channel_id)
    if ch is None:
        return step  # not cached: the publisher checks it again when it opens the channel
    ch_guild = getattr(ch, "guild", None)
    if ch_guild is None:
        return Resolved(step.key, step.value, "not_in_server",
                        detail=f"{step.key}={step.value} is a direct message, not a server channel; ignored")
    if guild is not None and not same_guild(ch_guild, guild):
        return Resolved(step.key, step.value, "other_guild",
                        detail=f"{step.key}={step.value} is in server {_guild_label(ch_guild)}, not in "
                               f"{_guild_label(guild)}; ignored")
    return Resolved(step.key, step.value, "ok", step.channel_id, str(getattr(ch, "name", "") or step.channel_name))


def _explicit_warnings(client: Any, step: Resolved) -> list[str]:
    ch = _cached_channel(client, step.channel_id or "")
    if ch is None:
        return []
    out = []
    if not is_public(ch):
        out.append(f"{step.key}: #{getattr(ch, 'name', '')} is not visible to @everyone; only its members see the notes")
    if is_nsfw(ch):
        out.append(f"{step.key}: #{getattr(ch, 'name', '')} is marked NSFW")
    return out


def resolve(client: Any, meeting: Meeting, s: Settings) -> Destination:
    """Pure over the discord.py cache (runs on the gateway loop)."""
    d = Destination()
    imported = meeting.source == SOURCE_GOOGLE_MEET or not meeting.guild_id
    keys = (("google_meet_discord_channel", s.google_meet_discord_channel),) if meeting.source == SOURCE_GOOGLE_MEET else ()
    keys += (("delivery_discord_channel", s.delivery_discord_channel),)
    guild = None
    search_everywhere = False  # a name may be looked up across servers only when nothing fixes the server
    if not imported:
        getter = getattr(client, "get_guild", None)
        try:
            guild = getter(int(meeting.guild_id)) if callable(getter) and is_ascii_digits(meeting.guild_id) else None
        except (TypeError, ValueError):
            guild = None
        if guild is None:  # never look in other servers for a meeting of this one (review I3)
            d.guild_source = "none"
            d.steps.append(Resolved("guild", meeting.guild_id, "no_guild",
                                    detail=f"the meeting's server {meeting.guild_id} is not available to the bot "
                                           "(not loaded yet, or the bot left it)"))
            d.problem = pending_reason(meeting, d)
            return d
        d.guild_source = "meeting"
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
                if (s.delivery_discord_guild or "").strip():  # configured but wrong: never search globally
                    d.problem = pending_reason(meeting, d)
                    return d
                search_everywhere = True
    d.guild = guild
    for key, value in keys:
        step = resolve_setting(client, key, value, guild if not search_everywhere else None)
        step = _check_id(client, step, guild)
        if step.status != "unset":
            d.steps.append(step)
        if step.channel_id and step.channel_id not in d.targets:
            d.targets.append(step.channel_id)
            d.warnings += _explicit_warnings(client, step)
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
    fb = _check_id(client, resolve_setting(client, "delivery_fallback_channel", s.delivery_fallback_channel, guild),
                   guild)
    if fb.status != "unset":
        d.steps.append(fb)
    d.fallback_channel = fb.channel_id
    if not d.targets:
        d.problem = pending_reason(meeting, d)
    return d


def channel_key(meeting: Meeting) -> str:
    """The setting that names the notes channel of this kind of meeting explicitly."""
    return "google_meet_discord_channel" if meeting.source == SOURCE_GOOGLE_MEET else "delivery_discord_channel"


def explicit_channel(d: Destination) -> Optional[Resolved]:
    """The first notes channel that comes from a CONFIGURED setting (never the automatic one)."""
    return next((st for st in d.steps if st.key in EXPLICIT_KEYS and st.status == "ok" and st.channel_id), None)


def pending_reason(meeting: Meeting, d: Destination) -> str:
    """The exact instruction shown by ``status``/``doctor`` while a delivery waits for a channel."""
    key = channel_key(meeting)
    problems = "; ".join(st.detail for st in d.steps if st.detail and st.status not in ("ok", "unset"))
    hint = f"`hermes meeting-scribe config set {key} \"#channel-name\"` (or a channel id)"
    return f"waiting for a Discord channel: {problems or 'no notes channel could be resolved'}. Set one with {hint}"
