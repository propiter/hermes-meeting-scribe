"""Editing ``meeting_routes`` one rule at a time (DESIGN §19.3): the ``route`` CLI, the REST API and the
Desktop rule editor share this, so every surface validates and writes rules the same way.

A rule is built from three plain choices — the ORIGIN (a voice channel, a category or a Google Meet
pattern), the DESTINATION (a text/forum channel; none for direct messages) and the MODE (``normal``,
``private``, ``dm``) — and stored in the canonical text of :mod:`routes`. When the channel catalog of
the space is known, names are resolved to ids (a rename then never breaks the rule) and a wrong name
is refused with the reason; without a catalog the rule is stored as written and checked later.

Where the list lives: a space's own override when it has one or when the install has several spaces;
otherwise the global value (one team, the simple case). :func:`scope_for` decides; the callers write.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .channel_catalog import Catalog, Check
from .routes import MEET_PREFIX, parse_route, validate_entries

ORIGIN_KINDS = ("voice", "category", "meet")
MODES = ("normal", "private", "dm")


class RuleError(ValueError):
    """A rule that cannot be written; the message is for the person editing it."""


@dataclass(frozen=True)
class Built:
    entry: str  # canonical text, ready for meeting_routes
    warning: str = ""  # usable but risky (a private rule on a channel everyone sees)


def _resolved(check: Check, ref: str, what: str) -> str:
    if check.status == "ok" and check.id:
        return check.id
    if check.status == "unknown":
        return ref  # no catalog yet: kept as written, checked once the gateway reports the channels
    raise RuleError(f"{what}: {check.detail}")


def build_entry(origin_kind: str, origin: str, target: str, mode: str, catalog: Optional[Catalog] = None) -> Built:
    """One rule from the editor's three choices; ``RuleError`` explains what to fix."""
    origin_kind, mode = (origin_kind or "").strip().lower(), (mode or "normal").strip().lower()
    origin, target = str(origin or "").strip(), str(target or "").strip()
    if origin_kind not in ORIGIN_KINDS:
        raise RuleError(f"origin: choose one of {', '.join(ORIGIN_KINDS)}")
    if mode not in MODES:
        raise RuleError(f"mode: choose one of {', '.join(MODES)}")
    if not origin:
        raise RuleError("origin: say which voice channel, category or Google Meet pattern")
    catalog = catalog or Catalog({})
    if origin_kind == "voice":
        left = _resolved(catalog.find(origin, ("voice",)), origin.lstrip("#"), "origin")
    elif origin_kind == "category":
        left = "category:" + _resolved(catalog.find(origin, ("category",)), origin.lstrip("#"), "origin")
    else:
        left = MEET_PREFIX + origin.lower().removeprefix(MEET_PREFIX)
    warning = ""
    if mode == "dm":
        if target:
            raise RuleError("destination: 'direct messages only' sends each participant a copy; leave the channel empty")
        right = ":dm"
    else:
        if not target:
            raise RuleError("destination: choose the text or forum channel the meetings go to")
        check = catalog.find(target, ("text", "forum", "media"))
        cid = _resolved(check, target.lstrip("#"), "destination")
        right = (cid if cid.isdigit() else f"#{cid}") + (":private" if mode == "private" else "")
        if mode == "private" and check.status == "ok" and check.public:
            warning = (f"#{check.name} is visible to the whole server (@everyone): a private rule keeps the meeting "
                       "in that channel, so everyone who can see it reads it. Choose a private channel")
    try:
        return Built(parse_route(f"{left} = {right}").text, warning)
    except ValueError as exc:
        raise RuleError(str(exc)) from exc


def _checked(entries: Sequence[str]) -> list[str]:
    try:
        return validate_entries(list(entries))
    except ValueError as exc:
        raise RuleError(str(exc)) from exc


def index_of(entries: Sequence[str], which: Any) -> int:
    """A rule by its position (1-based, as listed) or by its origin text."""
    text = str(which).strip()
    if text.isdigit() and 1 <= int(text) <= len(entries):
        return int(text) - 1
    for i, entry in enumerate(entries):
        try:
            if parse_route(entry).origin.lower() == text.lower():
                return i
        except ValueError:
            continue
    raise RuleError(f"there is no rule {text!r} (use its number from the list, or its origin)")


def add(entries: Sequence[str], entry: str, position: Optional[int] = None) -> list[str]:
    out = list(entries)
    out.insert(len(out) if position is None else max(0, min(len(out), position - 1)), entry)
    return _checked(out)


def replace_at(entries: Sequence[str], which: Any, entry: str) -> list[str]:
    out = list(entries)
    out[index_of(out, which)] = entry
    return _checked(out)


def remove(entries: Sequence[str], which: Any) -> list[str]:
    """Removing never needs the other rules to be valid (the way out of a broken list)."""
    out = list(entries)
    del out[index_of(out, which)]
    return out


def move(entries: Sequence[str], which: Any, to: int) -> list[str]:
    out = list(entries)
    entry = out.pop(index_of(out, which))
    out.insert(max(0, min(len(out), int(to) - 1)), entry)
    return out


def scope_for(spaces: Any, space: str) -> str:
    """``space`` (write the space's override) or ``global`` (one space without its own list)."""
    rows = spaces.all()
    row = next((s for s in rows if s.slug == space), None)
    if len(rows) > 1 or (row is not None and "meeting_routes" in row.overrides):
        return "space"
    return "global"


def sentence(row: dict[str, Any], lang: str = "en") -> str:
    """``Voice channel «Leadership» → forum «leadership-notes» · Private`` for lists."""
    from .i18n import t

    o = row.get("origin_check") or {}
    kind = row.get("kind", "")
    name = o.get("name") or row.get("origin", "").split(":", 1)[-1]
    left = t(f"routes.origin_{kind}", lang, name=name) if kind in ORIGIN_KINDS else str(row.get("origin", ""))
    mode = row.get("mode") or "normal"
    if mode == "dm":
        right = t("routes.to_dm", lang)
    else:
        d = row.get("target_check") or {}
        target_name = d.get("name") or row.get("channel_name") or row.get("channel") or "?"
        right = t("routes.to_forum" if d.get("kind") in ("forum", "media") else "routes.to_channel", lang,
                  name=target_name)
    return f"{left} → {right} · {t(f'routes.mode_{mode}', lang)}"
