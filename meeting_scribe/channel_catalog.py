"""The bot's channel catalog per Discord server (DESIGN §19.3): what ``config list``, ``doctor``, the
``route`` CLI, the REST API and the Desktop rule editor show instead of bare ids.

The gateway (the process connected to Discord) writes a snapshot of every server it is in at each
connect and, debounced, whenever a channel is created, deleted or updated (:func:`snapshot_guild`,
``Repository.set_guild_channels``). Every other process only READS it: nothing here talks to Discord.

A catalog entry: ``{"id", "name", "type": voice|text|forum|media|category, "parent_id", "parent_name",
"public": @everyone can see it}``. :class:`Catalog` resolves names to ids and checks rules against it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from .routes import MeetingRoute, norm_name

KINDS = ("voice", "text", "forum", "media", "category")
TARGET_KINDS = frozenset({"text", "forum", "media"})
_TYPE_MAP = {"voice": "voice", "stage_voice": "voice", "text": "text", "news": "text", "forum": "forum",
             "media": "media", "category": "category"}


def channel_kind(channel: Any) -> Optional[str]:
    """Catalog kind of a discord.py channel (``None``: not listed — threads, directories)."""
    raw = str(getattr(channel, "type", "") or "").rsplit(".", 1)[-1]
    return _TYPE_MAP.get(raw)


def _public(channel: Any) -> bool:
    role = getattr(getattr(channel, "guild", None), "default_role", None)
    if role is None:
        return True
    try:
        return bool(getattr(channel.permissions_for(role), "view_channel", True))
    except Exception:  # partial object: unknown counts as visible (never hides a warning)
        return True


def snapshot_guild(guild: Any) -> list[dict[str, Any]]:
    """Gateway side: the catalog entries of one server from the discord.py cache."""
    channels = list(getattr(guild, "channels", None) or ())
    names = {str(c.id): str(getattr(c, "name", "") or "") for c in channels if channel_kind(c) == "category"}
    out = []
    for ch in sorted(channels, key=lambda c: (int(getattr(c, "position", 0) or 0), int(c.id))):
        kind = channel_kind(ch)
        if kind is None:
            continue
        parent = getattr(ch, "category_id", None)
        parent = str(parent) if parent and kind != "category" else ""
        out.append({"id": str(ch.id), "name": str(getattr(ch, "name", "") or ""), "type": kind,
                    "parent_id": parent, "parent_name": names.get(parent, ""), "public": _public(ch)})
    return out


@dataclass(frozen=True)
class Check:
    """A rule side resolved against the catalog. ``status``: ``ok`` | ``missing`` | ``ambiguous`` |
    ``wrong_kind`` | ``unknown`` (no catalog for the space yet) | ``no_servers`` (the space has none)."""
    status: str
    id: str = ""
    name: str = ""
    kind: str = ""
    public: Optional[bool] = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "id": self.id, "name": self.name, "kind": self.kind, "public": self.public,
                "detail": self.detail}


NO_SERVERS = ("this space has no Discord server assigned, so it sees no channel: assign a server to this space "
              "first (hermes meeting-scribe space add-guild <space> <server id>)")


class Catalog:
    """The channels of a set of servers (a space's), as last seen by the gateway. ``no_servers``: the
    space has no server at all — it sees nothing and every lookup is refused (never another space's
    channels). ``foreign``: ids of channels catalogued in servers OUTSIDE the space, refused even
    before the space's own servers were catalogued."""

    def __init__(self, by_guild: Mapping[str, tuple[Sequence[Mapping[str, Any]], float]],
                 guild_names: Optional[Mapping[str, str]] = None, *, no_servers: bool = False,
                 foreign: Iterable[str] = ()) -> None:
        self.by_guild = {g: [dict(c, guild_id=g, guild_name=(guild_names or {}).get(g, "")) for c in rows]
                         for g, (rows, _seen) in by_guild.items()}
        self.seen_at = max((seen for _rows, seen in by_guild.values()), default=None)
        self.no_servers = no_servers
        self.foreign = frozenset(str(x) for x in foreign)

    @classmethod
    def load(cls, repo: Any, guild_ids: Optional[Iterable[str]]) -> "Catalog":
        """The catalog of the servers ``guild_ids`` (``None``: every server the bot reported; empty: a
        catalog that refuses every lookup)."""
        names = dict(repo.bot_guilds()[0])
        if guild_ids is None:
            return cls(repo.guild_channels(None), names)
        wanted = [str(g) for g in guild_ids]
        if not wanted:
            return cls({}, no_servers=True)
        everything = repo.guild_channels(None)
        foreign = {str(c.get("id")) for g, (rows, _seen) in everything.items() if g not in wanted for c in rows}
        return cls({g: v for g, v in everything.items() if g in wanted}, names, foreign=foreign)

    @property
    def known(self) -> bool:
        return bool(self.by_guild)

    def channels(self, kinds: Iterable[str] = KINDS) -> list[dict[str, Any]]:
        wanted = set(kinds)
        return [c for rows in self.by_guild.values() for c in rows if c["type"] in wanted]

    def get(self, cid: str) -> Optional[dict[str, Any]]:
        return next((c for c in self.channels() if c["id"] == str(cid)), None)

    def find(self, ref: str, kinds: Iterable[str]) -> Check:
        """An id or a name (``#`` optional, case/accents/decoration ignored) among ``kinds``."""
        kinds = tuple(kinds)
        label = "/".join(kinds)
        if self.no_servers:
            return Check("no_servers", detail=NO_SERVERS)
        ref = str(ref or "").strip().lstrip("#")
        if ref in self.foreign:
            return Check("missing", ref, detail=f"channel {ref} belongs to a server outside this space")
        if not self.known:
            return Check("unknown", detail="the bot has not reported its channels yet (the gateway writes them when "
                                           "it connects to Discord)")
        if ref.isdigit():
            hit = self.get(ref)
            if hit is None:
                return Check("missing", ref, detail=f"no channel with id {ref} in this space's servers")
            if hit["type"] not in kinds:
                return Check("wrong_kind", ref, hit["name"], hit["type"], hit["public"],
                             detail=f"#{hit['name']} ({ref}) is a {hit['type']} channel, expected {label}")
            return Check("ok", ref, hit["name"], hit["type"], hit["public"])
        wanted = norm_name(ref)
        hits = [c for c in self.channels() if norm_name(c["name"]) == wanted]
        good = [c for c in hits if c["type"] in kinds]
        if len(good) == 1:
            c = good[0]
            return Check("ok", c["id"], c["name"], c["type"], c["public"])
        if len(good) > 1:
            ids = ", ".join(c["id"] for c in good)
            return Check("ambiguous", detail=f"{len(good)} {label} channels are named {ref!r} ({ids}); use the id")
        if hits:
            c = hits[0]
            return Check("wrong_kind", c["id"], c["name"], c["type"], c["public"],
                         detail=f"#{c['name']} is a {c['type']} channel, expected {label}")
        return Check("missing", detail=f"no {label} channel named {ref!r} in this space's servers")

    def origin(self, rule: MeetingRoute) -> Check:
        if rule.kind == "voice":
            return self.find(rule.ref, ("voice",))
        if rule.kind == "category":
            return self.find(rule.ref, ("category",))
        return Check("ok", name=rule.ref, kind="meet")  # a Google Meet pattern: nothing to look up

    def target(self, rule: MeetingRoute) -> Check:
        if rule.dm:
            return Check("ok", kind="dm")
        return self.find(rule.channel, TARGET_KINDS)

    def verify(self, rule: MeetingRoute) -> dict[str, Any]:
        """A rule as the editors show it: both sides resolved, one status and plain-words problems."""
        if rule.error:
            return {"status": "invalid", "detail": rule.error, "origin_check": None, "target_check": None}
        o, d = self.origin(rule), self.target(rule)
        problems = [c.detail for c in (o, d) if c.status != "ok" and c.detail]
        status = "not_checked" if "unknown" in (o.status, d.status) else ("ok" if not problems else "problem")
        warning = ""
        if status == "ok" and rule.private and not rule.dm and d.public:
            warning = (f"meeting_routes[{rule.origin}]: #{d.name} is visible to the whole server (@everyone); "
                       "a private rule should point to a private channel")
        return {"status": status, "detail": "; ".join(dict.fromkeys(problems)), "warning": warning,
                "origin_check": o.to_dict(), "target_check": d.to_dict()}


def describe(check: Mapping[str, Any]) -> str:
    """``#orion (501, forum, private)`` — a resolved side, in plain words."""
    if not check or not check.get("id"):
        return ""
    vis = "" if check.get("public") is None else (", visible to everyone" if check["public"] else ", private")
    return f"#{check.get('name')} ({check['id']}, {check.get('kind')}{vis})"
