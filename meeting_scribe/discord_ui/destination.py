"""Where a meeting's notes go (DESIGN §19) — resolved on the gateway loop from the discord.py cache.

Order of the notes channel:

* A ``meeting_routes`` rule matching the meeting (DESIGN §19.2) → ONLY that channel: when it cannot
  be used the delivery is PENDING with the reason, never a more public channel.
* Google Meet imports: ``google_meet_discord_channel`` → ``delivery_discord_channel`` → AUTOMATIC.
* Discord voice meetings: ``delivery_discord_channel`` → the voice channel's text chat → AUTOMATIC.
* Nothing usable → PENDING: the delivery waits (without using attempts) until a channel is set or
  appears; ``status``/``doctor`` show the exact command to run.

Channel settings hold an id or a NAME. A name is matched against the server's text, announcement,
forum and media channels after removing decoration (emoji, ``#``, case, ``-``/``_``/spaces);
ambiguous or unknown names are never guessed — they are reported and skipped. In a forum (or media)
channel each meeting is ONE post (DESIGN §19.1).

The server ("guild"): the meeting's own for Discord meetings (not cached → PENDING, never another
server); for imported meetings (no guild) the one of a configured channel id, else
``delivery_discord_guild`` (id or name; configured but unresolvable → PENDING), else the bot's only
server. With several servers and nothing configured nothing is guessed. Configured ids of a DM or of
another server are ignored and reported.

AUTOMATIC, in that server: its system channel when the bot can post AND attach files there, else
the first text or forum channel whose clean name is in ``delivery_auto_channel_names`` (list order,
then channel position) where the bot can post. Never NSFW, never hidden from ``@everyone``, and nothing
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
FORUM_KINDS = frozenset({"forum", "media"})  # Discord channel types 15 and 16: one post (thread) per meeting
POSTABLE_KINDS = TEXT_KINDS | FORUM_KINDS
MAX_FORUM_TAGS = 5  # Discord accepts at most 5 applied tags per post
TAG_REQUIRED_CODE = 40067  # "A tag is required to create a forum post in this channel"
EXPLICIT_KEYS = ("google_meet_discord_channel", "delivery_discord_channel")
ROUTE_KEY = "meeting_routes"
ROUTES_REPORT_KV = "discord.routes_report."  # + space -> every rule resolved (doctor / config list)


def route_key(rule: Any) -> str:
    return f"{ROUTE_KEY}[{rule.origin}]"


def is_explicit(key: str) -> bool:
    return key in EXPLICIT_KEYS or key.startswith(ROUTE_KEY + "[")
_SEP_RE = re.compile(r"[-_\s]+")


def norm_name(name: str) -> str:
    """Comparable form of a channel/server name: decoration removed, folded, separators unified."""
    cleaned = clean_channel_name(str(name or "").lstrip("#")) or str(name or "")
    return _SEP_RE.sub(" ", fold(cleaned)).strip()


def _kind(channel: Any) -> str:
    return str(getattr(channel, "type", "") or "").rsplit(".", 1)[-1]


def is_forum(channel: Any) -> bool:
    """A forum or media channel: nothing is sent to it directly; each meeting is a post (thread)."""
    return _kind(channel) in FORUM_KINDS


def is_postable(channel: Any) -> bool:
    return _kind(channel) in POSTABLE_KINDS


def kind_label(channel: Any) -> str:
    """``text`` | ``forum`` | ``media`` (announcement channels count as text)."""
    kind = _kind(channel)
    return kind if kind in FORUM_KINDS else "text"


def requires_tag(forum: Any) -> bool:
    return bool(getattr(getattr(forum, "flags", None), "require_tag", False))


def tag_names(forum: Any) -> list[str]:
    return [str(getattr(tag, "name", "") or "") for tag in list(getattr(forum, "available_tags", None) or ())]


def pick_tags(forum: Any, wanted: Iterable[str], defaults: Sequence[str] = ()) -> list[Any]:
    """The forum's tags whose name matches (decoration, emoji and case ignored) one of ``wanted``, in the
    forum's order, at most 5. When the forum REQUIRES a tag and none matched, the first of ``defaults``
    that the forum has. Tag names are never invented: an unknown name is simply not applied."""
    tags = list(getattr(forum, "available_tags", None) or ())
    keys = {norm_name(w) for w in wanted if norm_name(w)}
    hits = [tag for tag in tags if norm_name(getattr(tag, "name", "")) in keys][:MAX_FORUM_TAGS]
    if hits or not requires_tag(forum):
        return hits
    for name in defaults:
        tag = next((x for x in tags if norm_name(getattr(x, "name", "")) == norm_name(name)), None)
        if tag is not None:
            return [tag]
    return []


def tag_rejected(exc: BaseException) -> bool:
    """Discord refused a post because the forum requires a tag (HTTP 400, code 40067)."""
    return getattr(exc, "code", None) == TAG_REQUIRED_CODE


class DestinationPending(LookupError):
    """No usable notes channel yet (none resolved, or a forum refused the post); the delivery waits,
    without spending attempts, until the configuration is fixed (DESIGN §19)."""


def _perms(channel: Any, member: Any) -> Any:
    if member is None:
        return None
    try:
        return channel.permissions_for(member)
    except Exception:  # uncached member / partial object
        return None


def needed_permissions(channel: Any, *, attach: bool = False) -> tuple[str, ...]:
    """What the bot needs to post the notes there. A forum/media post is created with *Send Messages*
    and everything after its first message lives inside it (*Send Messages in Threads*)."""
    names = ("view_channel", "send_messages") + (("send_messages_in_threads",) if is_forum(channel) else ())
    return names + (("attach_files",) if attach else ())


def missing_permissions(channel: Any, member: Any, *, attach: bool = False) -> Optional[list[str]]:
    """The missing ones (``[]`` = all granted); ``None`` when the permissions are unknown."""
    p = _perms(channel, member)
    if p is None:
        return None
    return [name for name in needed_permissions(channel, attach=attach) if not bool(getattr(p, name, False))]


PERMISSION_LABELS = {"view_channel": "View Channel", "send_messages": "Send Messages",
                     "send_messages_in_threads": "Send Messages in Threads", "attach_files": "Attach Files"}


def can_send(channel: Any, member: Any, *, attach: bool = False) -> bool:
    """Known to be allowed. Unknown permissions (member not cached) are NOT a yes: the automatic
    choice must not guess (review M3); explicit settings do not go through this check."""
    return missing_permissions(channel, member, attach=attach) == []


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
    kind: str = ""  # text | forum | media (when the channel is known)

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
    rule: str = ""  # the ``meeting_routes`` origin that decided the notes channel ("" = none)
    private: bool = False  # that rule is private: nothing leaves the notes channel by itself
    held: bool = False  # a private meeting anchored to its channel whose rule now says otherwise: DELIVER waits

    def report(self) -> dict[str, Any]:
        g = self.guild
        return {"checked_at": time.time(),
                "guild": {"id": str(getattr(g, "id", "")) if g is not None else "", "name": getattr(g, "name", "") or "",
                          "source": self.guild_source},
                "steps": [s.to_dict() for s in self.steps], "targets": list(self.targets),
                "fallback_channel": self.fallback_channel or "", "problem": self.problem,
                "warnings": list(self.warnings), "rule": self.rule, "private": self.private}


def _guilds(client: Any, allowed: Optional[frozenset[str]] = None) -> list[Any]:
    """The bot's servers; with ``allowed``, only those (the servers of the meeting's space)."""
    return [g for g in (getattr(client, "guilds", None) or ()) if g is not None and _allowed(g, allowed)]


def _allowed(guild: Any, allowed: Optional[frozenset[str]]) -> bool:
    return allowed is None or str(getattr(guild, "id", "")) in allowed


def pick_guild(client: Any, setting: str, allowed: Optional[frozenset[str]] = None) -> tuple[Any, str, str]:
    """``(guild, source, problem)`` for meetings without a guild of their own, among ``allowed``."""
    guilds = _guilds(client, allowed)
    value = (setting or "").strip()
    if value:
        if is_ascii_digits(value):
            getter = getattr(client, "get_guild", None)
            g = getter(int(value)) if callable(getter) else None
            g = g or next((x for x in guilds if str(getattr(x, "id", "")) == value), None)
            if g is not None and not _allowed(g, allowed):
                return None, "none", f"delivery_discord_guild={value}: that server belongs to another space"
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
        return None, "none", ("the bot is not in any Discord server of this space" if allowed is not None
                              else "the bot is not in any Discord server")
    return None, "none", (f"the bot is in {len(guilds)} servers; set delivery_discord_guild "
                          "(`hermes meeting-scribe config set delivery_discord_guild \"<server name or id>\"`)")


def find_channel_by_name(guilds: Iterable[Any], name: str) -> tuple[list[Any], list[Any]]:
    """``(postable matches — text, announcement, forum, media —, other matches)`` of ``name``."""
    wanted = norm_name(name)
    text: list[Any] = []
    other: list[Any] = []
    for g in guilds:
        for ch in list(getattr(g, "channels", None) or ()):
            if norm_name(getattr(ch, "name", "")) == wanted:
                (text if is_postable(ch) else other).append(ch)
    return text, other


def resolve_setting(client: Any, key: str, value: str, guild: Any,
                    allowed: Optional[frozenset[str]] = None) -> Resolved:
    """A channel setting (id or name) → a postable channel id; names are resolved in ``guild`` (or, when no
    server is chosen, across every server of the space — accepted only when exactly one matches)."""
    kind, ref = channel_ref(value)
    if not kind:
        return Resolved(key, "", "unset")
    if kind == "id":
        return Resolved(key, ref, "ok", ref)
    scope = [guild] if guild is not None else _guilds(client, allowed)
    text, other = find_channel_by_name(scope, ref)
    if len(text) == 1:
        ch = text[0]
        return Resolved(key, ref, "ok", str(ch.id), str(getattr(ch, "name", "")), kind=kind_label(ch))
    if len(text) > 1:
        return Resolved(key, ref, "ambiguous", detail=f"{len(text)} channels are named like {ref!r}; "
                                                       f"use the channel id instead")
    if other:
        return Resolved(key, ref, "not_text", detail=f"#{ref} is not a text or forum channel")
    where = f"server {getattr(guild, 'name', '') or getattr(guild, 'id', '')}" if guild is not None else "any server"
    return Resolved(key, ref, "missing", detail=f"no text or forum channel named {ref!r} in {where}")


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
        if not is_postable(ch) or not can_send(ch, me, attach=attach):
            return False
        if is_nsfw(ch) or not is_public(ch):
            skipped.append(f"#{getattr(ch, 'name', '')}")
            return False
        return True
    system = getattr(guild, "system_channel", None)
    if system is not None and usable(system, attach=True):
        return Resolved("auto", "system_channel", "ok", str(system.id), str(getattr(system, "name", "")),
                        kind=kind_label(system))
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
    return Resolved("auto", str(getattr(ch, "name", "")), "ok", str(ch.id), str(getattr(ch, "name", "")),
                    kind=kind_label(ch))


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


def _check_id(client: Any, step: Resolved, guild: Any, allowed: Optional[frozenset[str]] = None) -> Resolved:
    """A configured channel ID must be a server channel of ``guild`` (never a DM, never another server)
    and of a server of the meeting's space (``allowed``). A channel that is not cached cannot be
    checked: with several spaces it is refused rather than risk another team's server."""
    if step.status != "ok" or not step.channel_id:
        return step
    ch = _cached_channel(client, step.channel_id)
    if ch is None:
        if allowed is not None and guild is None:
            return Resolved(step.key, step.value, "not_loaded",
                            detail=f"{step.key}={step.value} is not loaded yet; its server cannot be checked")
        return step  # not cached: the publisher checks it again when it opens the channel
    ch_guild = getattr(ch, "guild", None)
    if ch_guild is None:
        return Resolved(step.key, step.value, "not_in_server",
                        detail=f"{step.key}={step.value} is a direct message, not a server channel; ignored")
    if not _allowed(ch_guild, allowed):
        return Resolved(step.key, step.value, "other_guild",
                        detail=f"{step.key}={step.value} is in a server of another space; ignored")
    if guild is not None and not same_guild(ch_guild, guild):
        return Resolved(step.key, step.value, "other_guild",
                        detail=f"{step.key}={step.value} is in server {_guild_label(ch_guild)}, not in "
                               f"{_guild_label(guild)}; ignored")
    return Resolved(step.key, step.value, "ok", step.channel_id, str(getattr(ch, "name", "") or step.channel_name),
                    kind=kind_label(ch))


def forum_warnings(key: str, forum: Any, s: Settings) -> list[str]:
    """A forum that REQUIRES a tag, and none of ``delivery_forum_default_tag`` exists there: a post whose
    project and ``delivery_forum_tags`` match no tag will be refused (the delivery then waits)."""
    if not is_forum(forum) or not requires_tag(forum) or pick_tags(forum, (), s.delivery_forum_default_tag):
        return []
    names = ", ".join(tag_names(forum)) or "none"
    warning = (f"{key}: forum #{getattr(forum, 'name', '')} requires a tag on every post; posts whose project "
               "matches no tag are refused. Set one with `hermes meeting-scribe config set "
               f"delivery_forum_default_tag \"<tag>\"` (tags there: {names})")
    return [warning]


def permission_warnings(key: str, channel: Any, *, attach: bool) -> list[str]:
    """Missing bot permissions on a configured channel (unknown permissions: nothing to say)."""
    missing = missing_permissions(channel, getattr(getattr(channel, "guild", None), "me", None), attach=attach)
    if not missing:
        return []
    what = "forum" if is_forum(channel) else "channel"
    labels = ", ".join(PERMISSION_LABELS.get(m, m) for m in missing)
    return [f"{key}: the bot is missing {labels} in {what} #{getattr(channel, 'name', '')}"]


def visibility_warning(key: str, channel: Any, private: Optional[bool]) -> str:
    """``private`` rule on a channel @everyone can see, or a channel only its members see (normal rule
    and plain settings: ``private`` False/None)."""
    name = getattr(channel, "name", "")
    if private and is_public(channel):
        return f"{key}: private rule, but #{name} is visible to @everyone; everyone there sees the whole meeting"
    if not private and not is_public(channel):
        return f"{key}: #{name} is not visible to @everyone; only its members see the notes"
    return ""


def _explicit_warnings(client: Any, step: Resolved, s: Settings, private: Optional[bool] = None) -> list[str]:
    ch = _cached_channel(client, step.channel_id or "")
    if ch is None:
        return []
    out = [w for w in (visibility_warning(step.key, ch, private),) if w]
    if is_nsfw(ch):
        out.append(f"{step.key}: #{getattr(ch, 'name', '')} is marked NSFW")
    out += permission_warnings(step.key, ch, attach=s.delivery_discord_transcript and is_explicit(step.key))
    return out + forum_warnings(step.key, ch, s)


def resolve(client: Any, meeting: Meeting, s: Settings, *, allowed_guilds: Optional[frozenset[str]] = None) -> Destination:
    """Pure over the discord.py cache (runs on the gateway loop). ``allowed_guilds``: the servers of the
    meeting's space (``None``: unrestricted, one space); nothing outside them is ever a target."""
    from ..routes import match_route

    allowed = allowed_guilds
    d = Destination()
    imported = meeting.source == SOURCE_GOOGLE_MEET or not meeting.guild_id
    rule = match_route(meeting, s.routes())
    if rule is not None:  # the rule's channel and nothing else (DESIGN §19.2)
        d.rule, d.private = rule.origin, rule.private
        keys: tuple[tuple[str, str], ...] = ((route_key(rule), rule.channel),)
        if rule.error:
            d.guild_source = "none"
            d.steps.append(Resolved(route_key(rule), "", "invalid", detail=f"{route_key(rule)}: {rule.error}"))
            d.problem = pending_reason(meeting, d)
            return d
    else:
        keys = ((("google_meet_discord_channel", s.google_meet_discord_channel),)
                if meeting.source == SOURCE_GOOGLE_MEET else ())
        keys += (("delivery_discord_channel", s.delivery_discord_channel),)
    guild = None
    search_everywhere = False  # a name may be looked up across servers only when nothing fixes the server
    if not imported:
        getter = getattr(client, "get_guild", None)
        try:
            guild = getter(int(meeting.guild_id)) if callable(getter) and is_ascii_digits(meeting.guild_id) else None
        except (TypeError, ValueError):
            guild = None
        if guild is not None and not _allowed(guild, allowed):
            guild = None  # the server moved to another space: never publish there
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
                if guild is not None and not _allowed(guild, allowed):
                    guild = None
                if guild is not None:
                    d.guild_source = "channel"
                    break
        if guild is None:
            guild, d.guild_source, problem = pick_guild(client, s.delivery_discord_guild, allowed)
            if problem:
                d.steps.append(Resolved("delivery_discord_guild", s.delivery_discord_guild, "no_guild", detail=problem))
                if (s.delivery_discord_guild or "").strip():  # configured but wrong: never search globally
                    d.problem = pending_reason(meeting, d)
                    return d
                search_everywhere = True
    d.guild = guild
    for key, value in keys:
        step = resolve_setting(client, key, value, guild if not search_everywhere else None, allowed)
        step = _check_id(client, step, guild, allowed)
        if step.status != "unset":
            d.steps.append(step)
        if step.channel_id and step.channel_id not in d.targets:
            d.targets.append(step.channel_id)
            d.warnings += _explicit_warnings(client, step, s, d.private if rule is not None else None)
            if guild is None:  # a name found in exactly one server: that server
                guild = d.guild = _guild_of_channel(client, step.channel_id)
                d.guild_source = "channel" if guild is not None else d.guild_source
    if rule is not None:
        return _finish_rule(client, d, s, meeting, guild, allowed)
    if not imported:
        for cid in (meeting.text_channel_id, meeting.channel_id):
            if cid and is_ascii_digits(str(cid)) and str(cid) not in d.targets:
                d.targets.append(str(cid))
    auto = auto_channel(guild, s.delivery_auto_channel_names)
    d.steps.append(auto)
    if auto.channel_id and auto.channel_id not in d.targets:
        d.targets.append(auto.channel_id)
    fb = _check_id(client, resolve_setting(client, "delivery_fallback_channel", s.delivery_fallback_channel, guild,
                                           allowed), guild, allowed)
    if fb.status != "unset":
        d.steps.append(fb)
    d.fallback_channel = fb.channel_id
    if fb.channel_id:
        d.warnings += _explicit_warnings(client, fb, s)
    d.warnings += _project_forum_warnings(client, s, guild)
    if not d.targets:
        d.problem = pending_reason(meeting, d)
    return d


def _finish_rule(client: Any, d: Destination, s: Settings, meeting: Meeting, guild: Any,
                 allowed: Optional[frozenset[str]]) -> Destination:
    """A rule decided the notes channel: no voice chat, no automatic channel. A private rule has no
    fallback channel either (its tasks never leave the notes channel by themselves)."""
    if not d.private:
        fb = _check_id(client, resolve_setting(client, "delivery_fallback_channel", s.delivery_fallback_channel, guild,
                                               allowed), guild, allowed)
        if fb.status != "unset":
            d.steps.append(fb)
        d.fallback_channel = fb.channel_id
        if fb.channel_id:
            d.warnings += _explicit_warnings(client, fb, s)
        d.warnings += _project_forum_warnings(client, s, guild)
    if not d.targets:
        d.problem = pending_reason(meeting, d)
    return d


def resolve_routes(client: Any, s: Settings, allowed: Optional[frozenset[str]] = None) -> list[dict[str, Any]]:
    """Every ``meeting_routes`` rule resolved against the discord.py cache (doctor / ``config list``):
    origin, channel, kind, private, visibility and what is wrong."""
    out: list[dict[str, Any]] = []
    for rule in s.routes():
        row: dict[str, Any] = {"origin": rule.origin, "kind": rule.kind, "channel": rule.channel,
                               "private": rule.private, "status": "invalid" if rule.error else "",
                               "detail": rule.error}
        if not rule.error:
            step = _check_id(client, resolve_setting(client, route_key(rule), rule.channel, None, allowed), None,
                             allowed)
            ch = _cached_channel(client, step.channel_id or "")
            row.update(status=step.status, detail=step.detail, channel_id=step.channel_id or "",
                       channel_name=step.channel_name or str(getattr(ch, "name", "") or ""), target_kind=step.kind)
            if ch is not None:
                row.update(target_kind=kind_label(ch), public=is_public(ch),
                           warning=visibility_warning(route_key(rule), ch, rule.private))
        out.append(row)
    return out


def _project_forum_warnings(client: Any, s: Settings, guild: Any) -> list[str]:
    """``project_channels`` entries that are forums of this server: permissions and required tags."""
    out: list[str] = []
    for project, cid in s.project_channel_map().items():
        ch = _cached_channel(client, cid)
        if ch is None or not is_forum(ch) or (guild is not None and not same_guild(getattr(ch, "guild", None), guild)):
            continue
        key = f"project_channels[{project}]"
        out += permission_warnings(key, ch, attach=False) + forum_warnings(key, ch, s)
    return out


def channel_key(meeting: Meeting) -> str:
    """The setting that names the notes channel of this kind of meeting explicitly."""
    return "google_meet_discord_channel" if meeting.source == SOURCE_GOOGLE_MEET else "delivery_discord_channel"


def explicit_channel(d: Destination) -> Optional[Resolved]:
    """The first notes channel that comes from a CONFIGURED setting (never the automatic one)."""
    return next((st for st in d.steps if is_explicit(st.key) and st.status == "ok" and st.channel_id), None)


def pending_reason(meeting: Meeting, d: Destination) -> str:
    """The exact instruction shown by ``status``/``doctor`` while a delivery waits for a channel."""
    problems = "; ".join(st.detail for st in d.steps if st.detail and st.status not in ("ok", "unset"))
    if d.rule:
        return (f"waiting for the Discord channel of rule {d.rule!r} in meeting_routes: "
                f"{problems or 'its channel could not be resolved'}. Fix the rule with `hermes meeting-scribe "
                "config set meeting_routes \"...\"` (the meeting is never published elsewhere)")
    key = channel_key(meeting)
    hint = f"`hermes meeting-scribe config set {key} \"#channel-name\"` (or a channel id)"
    return f"waiting for a Discord channel: {problems or 'no notes channel could be resolved'}. Set one with {hint}"
