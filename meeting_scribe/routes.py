"""Per-meeting notes destinations (``meeting_routes``, DESIGN §19.2) — pure, no discord.py.

One rule per entry, ``<origin> = <channel>[:private]``:

* origin — which meetings the rule covers:
  ``<voice channel id>`` · ``<voice channel name>`` (``#`` optional) · ``category:<id or name>`` (every
  voice channel of that Discord category) · ``meet:<pattern>`` (Google Meet: the meeting code or the
  Meet room id or the meeting title; ``*`` and ``?`` wildcards, case ignored).
* channel — where the notes go: a text/announcement/forum channel id, ``<#id>`` or name.
* ``:private`` (also ``:privado``/``:privada``) — PRIVATE mode: everything stays in that channel and
  tasks leave it only when a member presses a share button.

Precedence among rules: a voice channel rule beats a category rule; within the same kind, the first in
the list wins. A meeting a private rule matched when it was recorded stays private for good (the sticky
record of :mod:`meeting_scribe.privacy`).

Loading is lenient but FAILS CLOSED: an entry whose origin is readable but whose channel or option is
not becomes a *broken* rule that still matches — its meetings are treated as private and their
delivery waits with the reason — so a typo can never publish a private meeting in a public channel.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Optional, Sequence

from .domain.models import SOURCE_GOOGLE_MEET, Meeting
from .domain.names import clean_channel_name
from .domain.text import fold, is_ascii_digits

PRIVATE_FLAGS = frozenset({"private", "privado", "privada"})
CATEGORY_PREFIXES = ("category:", "categoria:", "categoría:")
MEET_PREFIX = "meet:"
_SEP_RE = re.compile(r"[-_\s]+")
_NAME_MAX = 100
# voice channel rules beat category rules; Meet rules only ever see Meet meetings
_RANK = {"voice": 0, "category": 1, "meet": 0}


def norm_name(name: str) -> str:
    """Comparable form of a channel/server name: decoration removed, folded, separators unified."""
    cleaned = clean_channel_name(str(name or "").lstrip("#")) or str(name or "")
    return _SEP_RE.sub(" ", fold(cleaned)).strip()


@dataclass(frozen=True)
class MeetingRoute:
    kind: str  # voice | category | meet
    ref: str  # id, name or (meet) lower-case pattern
    channel: str  # notes channel id or name ("" = broken rule)
    private: bool
    error: str = ""  # why a broken rule cannot be used (its meetings wait, as private)

    @property
    def by_id(self) -> bool:
        return self.kind != "meet" and is_ascii_digits(self.ref)

    @property
    def origin(self) -> str:
        """Canonical origin text (also the rule's key in reports)."""
        if self.kind == "category":
            return f"category:{self.ref}"
        if self.kind == "meet":
            return f"{MEET_PREFIX}{self.ref}"
        return self.ref

    @property
    def text(self) -> str:
        """Canonical entry: ``origin=channel[:private]`` (names get a ``#``)."""
        channel = self.channel if not self.channel or is_ascii_digits(self.channel) else f"#{self.channel}"
        return f"{self.origin}={channel}" + (":private" if self.private else "")

    def matches(self, meeting: Meeting) -> bool:
        if self.kind == "meet":
            if meeting.source != SOURCE_GOOGLE_MEET:
                return False
            room = str(meeting.channel_id or "").split(":", 1)[-1]
            return any(value and fnmatch.fnmatchcase(value.lower(), self.ref)
                       for value in (str(meeting.channel_name or ""), room, str(meeting.title or "")))
        if meeting.source == SOURCE_GOOGLE_MEET:
            return False
        if self.kind == "voice":
            if self.by_id:
                return str(meeting.channel_id or "") == self.ref
            return bool(norm_name(self.ref)) and norm_name(meeting.channel_name) == norm_name(self.ref)
        if self.by_id:
            return str(meeting.category_id or "") == self.ref
        return bool(norm_name(self.ref)) and norm_name(meeting.category_name) == norm_name(self.ref)


def _line(value: str, what: str) -> str:
    value = value.strip()
    if not value or len(value) > _NAME_MAX or any(c in value for c in "\r\n\t"):
        raise ValueError(f"expected {what} (max {_NAME_MAX} characters, one line)")
    return value


def parse_origin(raw: str) -> tuple[str, str]:
    """``(kind, ref)`` of a rule's origin; ``ValueError`` explains what is wrong."""
    value = raw.strip()
    low = value.lower()
    for prefix in CATEGORY_PREFIXES:
        if low.startswith(prefix):
            return "category", _line(value[len(prefix):].lstrip("#"), "a Discord category id or name after 'category:'")
    if low.startswith(MEET_PREFIX):
        return "meet", _line(value[len(MEET_PREFIX):], "a Google Meet code or pattern after 'meet:'").lower()
    if value.startswith("<"):
        raise ValueError("the origin is a voice channel id or name, category:<name> or meet:<pattern>, not a mention")
    return "voice", _line(value.lstrip("#"), "a voice channel id or name")


def parse_route(entry: str) -> MeetingRoute:
    """Strict parse of one entry (``config set``, Desktop); ``ValueError`` names the problem."""
    from .config import _channel_value

    origin, sep, dest = str(entry).rpartition("=")
    if not sep or not origin.strip():
        raise ValueError(f"expected 'origin = #channel' (optionally ':private'), got {entry!r}")
    kind, ref = parse_origin(origin)
    channel, colon, flag = dest.rpartition(":")
    if not colon:
        channel, flag = dest, ""
    flag = flag.strip().lower()
    if flag and flag not in PRIVATE_FLAGS:
        raise ValueError(f"{origin.strip()}: unknown option {flag!r} (the only option is ':private')")
    channel = _channel_value(channel)
    if not channel:
        raise ValueError(f"{origin.strip()}: expected the notes channel after '=' (id, <#id> or #name)")
    return MeetingRoute(kind, ref, channel, bool(flag))


def load_routes(entries: Sequence[str]) -> tuple[list[MeetingRoute], list[str]]:
    """Lenient load: ``(rules, warnings)``. A readable origin with an unreadable rest is kept as a
    BROKEN private rule (fails closed); an entry without a readable origin is dropped with a warning."""
    rules: list[MeetingRoute] = []
    warnings: list[str] = []
    for entry in entries:
        try:
            rules.append(parse_route(entry))
            continue
        except ValueError as exc:
            problem = str(exc)
        origin, sep, _ = str(entry).rpartition("=")
        try:
            kind, ref = parse_origin(origin if sep else "")
        except ValueError:
            warnings.append(f"meeting_routes: {entry!r} ignored: {problem}")
            continue
        rules.append(MeetingRoute(kind, ref, "", True, error=problem))
        warnings.append(f"meeting_routes: {entry!r} is not valid ({problem}); its meetings are kept private and wait")
    return rules, warnings


def match_route(meeting: Meeting, rules: Sequence[MeetingRoute]) -> Optional[MeetingRoute]:
    """The rule for ``meeting``: the most specific kind first, then list order."""
    hits = [(i, r) for i, r in enumerate(rules) if r.matches(meeting)]
    if not hits:
        return None
    return min(hits, key=lambda hit: (_RANK[hit[1].kind], hit[0]))[1]


def validate_entries(entries: Sequence[str]) -> list[str]:
    """Canonical entries for storage; ``ValueError`` on the first bad one or a repeated origin."""
    out: list[str] = []
    seen: dict[tuple[str, str], str] = {}
    for entry in entries:
        rule = parse_route(entry)
        key = (rule.kind, rule.ref if rule.by_id or rule.kind == "meet" else norm_name(rule.ref))
        if key in seen:
            raise ValueError(f"{rule.origin}: the same origin appears twice ({seen[key]!r} and {entry!r})")
        seen[key] = entry
        out.append(rule.text)
    return out
