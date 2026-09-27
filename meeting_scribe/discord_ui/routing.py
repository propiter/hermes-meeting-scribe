"""Which channel a task is posted in (DESIGN §16) — pure, no discord.py.

Precedence for the task's project name (``item.project`` when the LLM picked a candidate, else the
spoken ``project_hint``):

1. the LLM picked a ``discord:<id>`` candidate → that channel;
2. explicit ``project_channels`` config entry for the name;
3. a mapping learned from a 📁 correction;
4. best fuzzy match among the guild's text channels and categories (a category resolves to its
   first text channel the bot may post in). A close runner-up, or a best match only above the
   weak floor, still posts there but flags the task as ``uncertain`` (⚠️ confirm with 📁);
5. nothing for the task → the meeting's project, always flagged;
6. nothing at all → ``channel_id=None``: the task stays in the meeting chat.

A matched channel the bot cannot post in (View / Send / Create Public Threads) also returns
``channel_id=None`` with ``reason="no_permission"`` and ``wanted_channel_id`` so the index says so.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

from ..domain.models import ActionItem, Meeting
from ..domain.names import clean_channel_name, match_name, rank_names
from ..domain.text import fold

WEAK_FLOOR = 0.2  # below min_score by this much a best guess is still posted, flagged


@dataclass(frozen=True)
class ChannelInfo:
    id: str
    name: str
    kind: str = "text"  # text | category
    category_id: Optional[str] = None
    position: int = 0
    can_post: bool = True  # View Channel + Send Messages + Create Public Threads


@dataclass(frozen=True)
class RouteContext:
    channel_map: Mapping[str, str]  # folded project name -> channel id (config)
    min_score: float
    ignore_prefixes: Sequence[str]
    learned: Callable[[str], Optional[str]]  # folded project name -> channel id


@dataclass(frozen=True)
class Route:
    channel_id: Optional[str]
    project: Optional[str] = None
    uncertain: bool = False
    reason: str = "none"  # candidate | config | learned | fuzzy | meeting | none | no_permission
    wanted_channel_id: Optional[str] = None


def _text_channel(target: ChannelInfo, channels: Sequence[ChannelInfo]) -> Optional[ChannelInfo]:
    if target.kind != "category":
        return target
    children = sorted((c for c in channels if c.category_id == target.id and c.kind != "category"),
                      key=lambda c: c.position)
    return next((c for c in children if c.can_post), children[0] if children else None)


def _finish(target: ChannelInfo, channels: Sequence[ChannelInfo], project: str, uncertain: bool,
            reason: str) -> Route:
    chan = _text_channel(target, channels)
    if chan is None or not chan.can_post:
        return Route(None, project, uncertain, "no_permission", chan.id if chan else target.id)
    return Route(chan.id, project, uncertain, reason)


def _fuzzy(name: str, channels: Sequence[ChannelInfo], ctx: RouteContext) -> Optional[tuple[ChannelInfo, bool]]:
    by_name: dict[str, ChannelInfo] = {}
    for c in channels:
        by_name.setdefault(c.name, c)
    hit = match_name(name, by_name, ctx.min_score, ignore_prefixes=ctx.ignore_prefixes)
    if hit is not None:
        return by_name[hit.name], hit.uncertain
    weak = rank_names(name, by_name, max(0.0, ctx.min_score - WEAK_FLOOR), ignore_prefixes=ctx.ignore_prefixes)
    return (by_name[weak[0][0]], True) if weak else None


def _by_name(name: str, channels: Sequence[ChannelInfo], ctx: RouteContext, *, guess: bool) -> Optional[Route]:
    by_id = {c.id: c for c in channels}
    key = fold(name)
    for reason, cid in (("config", ctx.channel_map.get(key)), ("learned", ctx.learned(key))):
        if cid and cid in by_id:
            return _finish(by_id[cid], channels, name, guess, "meeting" if guess else reason)
    found = _fuzzy(name, channels, ctx)
    if found is None:
        return None
    target, uncertain = found
    return _finish(target, channels, name if guess else clean_channel_name(target.name) or name,
                   uncertain or guess, "meeting" if guess else "fuzzy")


def route_item(item: ActionItem, meeting: Meeting, channels: Sequence[ChannelInfo], ctx: RouteContext) -> Route:
    by_id = {c.id: c for c in channels}
    if item.project_key and item.project_key.startswith("discord:"):
        target = by_id.get(item.project_key.split(":", 1)[1])
        if target is not None:
            return _finish(target, channels, item.project or clean_channel_name(target.name), False, "candidate")
    for name in (item.project, item.project_hint):
        if name:
            route = _by_name(name, channels, ctx, guess=False)
            if route is not None:
                return route
    if meeting.project:
        route = _by_name(meeting.project, channels, ctx, guess=True)
        if route is not None:
            return route
    return Route(None)
