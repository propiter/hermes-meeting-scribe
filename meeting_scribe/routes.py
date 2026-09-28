"""Per-meeting notes destinations (``meeting_routes``, DESIGN §19.2) — pure, no discord.py.

One rule per entry, ``<origin> = <channel>[:private]`` or ``<origin> = :dm``:

* origin — which meetings the rule covers:
  ``<voice channel id>`` · ``<voice channel name>`` (``#`` optional) · ``category:<id or name>`` (every
  voice channel of that Discord category) · ``meet:<pattern>`` (Google Meet: the meeting code or the
  Meet room id or the meeting title; ``*`` and ``?`` wildcards, case ignored).
* channel — where the notes go: a text/announcement/forum channel id, ``<#id>`` or name.
* ``:private`` (also ``:privado``/``:privada``) — PRIVATE mode: everything stays in that channel and
  tasks leave it only when a member presses a share button.
* ``:dm`` instead of a channel (also ``dm``, ``:directo``/``directo``, ``:mensajes``/``mensajes``) —
  DIRECT-MESSAGES-ONLY mode (DESIGN §19.3): nothing is posted in any channel; every participant gets
  the whole meeting in a DM. It is private in every other respect. A channel literally called ``dm``
  is written ``#dm``.

Precedence among rules: a voice channel rule beats a category rule; within the same kind, the first in
the list wins. A meeting a private rule matched when it was recorded stays private for good (the sticky
record of :mod:`meeting_scribe.privacy`).

Loading is lenient but FAILS CLOSED: an entry whose origin is readable but whose channel or option is
not becomes a *broken* rule that still matches — its meetings are treated as private and their
delivery waits with the reason. An unreadable entry that mentions a private or direct-message mark
anywhere (``private``/``privado``/``privada``, ``dm``/``directo``/``mensajes``) whose origin cannot be
read becomes a broken rule for EVERY meeting of the space (kind ``any``): all of them wait until it is
fixed. An unreadable entry without such a mark is dropped with a warning.
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
DM_WORDS = frozenset({"dm", "directo", "mensajes"})
_PRIVATE_MARK_RE = re.compile(r"(?<![a-z0-9])priv(?:ate|ado|ada)(?![a-z0-9])", re.IGNORECASE)
_DM_MARK_RE = re.compile(r"(?<![a-z0-9#])(?:dm|directo|mensajes)(?![a-z0-9])", re.IGNORECASE)
MODES = ("normal", "private", "dm")
CATEGORY_PREFIXES = ("category:", "categoria:", "categoría:")
MEET_PREFIX = "meet:"
_SEP_RE = re.compile(r"[-_\s]+")
_NAME_MAX = 100
# voice channel rules beat category rules; Meet rules only ever see Meet meetings
# an unreadable private entry (``any``) beats every rule: all meetings wait until it is fixed
_RANK = {"any": -1, "voice": 0, "category": 1, "meet": 0}


def has_private_mark(entry: str) -> bool:
    """``private``/``privado``/``privada`` as a word anywhere in ``entry``."""
    return bool(_PRIVATE_MARK_RE.search(str(entry or "")))


def has_dm_mark(text: str) -> bool:
    """``dm``/``directo``/``mensajes`` as a word (not a ``#dm`` channel name) anywhere in ``text``."""
    return bool(_DM_MARK_RE.search(str(text or "")))


def is_dm_target(dest: str) -> bool:
    """The right side of a rule asks for direct messages: ``:dm``/``dm`` (or ``directo``/``mensajes``)."""
    return str(dest or "").strip().lower().removeprefix(":").strip() in DM_WORDS


def norm_name(name: str) -> str:
    """Comparable form of a channel/server name: decoration removed, folded, separators unified."""
    cleaned = clean_channel_name(str(name or "").lstrip("#")) or str(name or "")
    return _SEP_RE.sub(" ", fold(cleaned)).strip()


@dataclass(frozen=True)
class MeetingRoute:
    kind: str  # voice | category | meet | any (an unreadable private entry: every meeting)
    ref: str  # id, name, (meet) lower-case pattern or (any) the entry as written
    channel: str  # notes channel id or name ("" = broken rule)
    private: bool
    error: str = ""  # why a broken rule cannot be used (its meetings wait, as private)
    dm: bool = False  # direct messages only (no channel; ``private`` is True too), DESIGN §19.3

    @property
    def mode(self) -> str:
        """``normal`` | ``private`` | ``dm``."""
        return "dm" if self.dm else "private" if self.private else "normal"

    @property
    def by_id(self) -> bool:
        return self.kind in ("voice", "category") and is_ascii_digits(self.ref)

    @property
    def origin(self) -> str:
        """Canonical origin text (also the rule's key in reports)."""
        if self.kind == "category":
            return f"category:{self.ref}"
        if self.kind == "meet":
            return f"{MEET_PREFIX}{self.ref}"
        return self.ref  # voice, and ``any`` (the entry as written)

    @property
    def text(self) -> str:
        """Canonical entry: ``origin=channel[:private]`` or ``origin=:dm`` (names get a ``#``)."""
        if self.dm:
            return f"{self.origin}=:dm"
        channel = self.channel if not self.channel or is_ascii_digits(self.channel) else f"#{self.channel}"
        return f"{self.origin}={channel}" + (":private" if self.private else "")

    def matches(self, meeting: Meeting) -> bool:
        if self.kind == "any":
            return True
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
        raise ValueError(f"expected 'origin = #channel' (optionally ':private') or 'origin = :dm', got {entry!r}")
    kind, ref = parse_origin(origin)
    if is_dm_target(dest):
        return MeetingRoute(kind, ref, "", True, dm=True)
    channel, colon, flag = dest.rpartition(":")
    if not colon:
        channel, flag = dest, ""
    flag = flag.strip().lower()
    if flag in DM_WORDS or is_dm_target(channel):
        raise ValueError(f"{origin.strip()}: ':dm' (direct messages only) takes no channel and no other option: "
                         f"write 'origin = :dm' (a channel called dm is written '#dm'), got {entry!r}")
    if flag and flag not in PRIVATE_FLAGS:
        raise ValueError(f"{origin.strip()}: unknown option {flag!r} (the options are ':private' and ':dm')")
    if not flag and has_private_mark(entry):
        raise ValueError(f"{origin.strip()}: 'private' must be written as ':private' right after the channel "
                         f"(origin = #channel:private), got {entry!r}")
    channel = _channel_value(channel)
    if not channel:
        raise ValueError(f"{origin.strip()}: expected the notes channel after '=' (id, <#id> or #name)")
    return MeetingRoute(kind, ref, channel, bool(flag))


def load_routes(entries: Sequence[str]) -> tuple[list[MeetingRoute], list[str]]:
    """Lenient load: ``(rules, warnings)``. A readable origin with an unreadable rest is kept as a
    BROKEN private rule (fails closed). Without a readable origin: an entry with a private mark becomes a
    broken rule for every meeting (``any``); one without it is dropped with a warning."""
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
            if has_private_mark(entry) or has_dm_mark(entry):
                rules.append(MeetingRoute("any", str(entry).strip()[:_NAME_MAX], "", True, error=problem))
                warnings.append(f"meeting_routes: {entry!r} is not valid ({problem}) and looks private; "
                                "EVERY meeting of this space waits until it is fixed")
            else:
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
