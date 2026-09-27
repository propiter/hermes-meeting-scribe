"""Meet transcript entries → our transcript model (DESIGN §17). Pure functions, no I/O.

* Times: seconds relative to the conference ``startTime`` (entries carry absolute RFC 3339 times).
* Speaker id: ``gmeet:<participant id>`` — stable per conference, derived from the participant
  resource name; it is never a Discord id, so nothing tries to mention or DM it.
* Speaker name: ``signedinUser|anonymousUser|phoneUser.displayName``; unknown participant → a
  numbered neutral label.
* Entries are NOT merged: Meet already emits sentence-level entries, and the only merge utility in
  the pipeline (``transcribe.merge``) works on whisper segments, not on finished utterances.
* Confidence: Meet exposes none; 1.0 means "provided by the source" (whisper uses avg_logprob).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..domain.models import Speaker, Utterance

SPEAKER_PREFIX = "gmeet:"


def parse_time(value: Any) -> Optional[datetime]:
    """RFC 3339 with ``Z`` and up to nanosecond fractions (``2026-09-27T15:04:05.123456789Z``)."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("z", "Z")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, _, rest = text.partition(".")
        digits = "".join(ch for ch in rest if "0" <= ch <= "9")
        tz = rest[len(digits):]
        text = f"{head}.{(digits + '000000')[:6]}{tz}"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def participant_id(name: str) -> str:
    """``conferenceRecords/abc/participants/123`` → ``gmeet:123``."""
    return SPEAKER_PREFIX + (str(name).rstrip("/").rsplit("/", 1)[-1] or "unknown")


def participant_name(p: Mapping[str, Any]) -> str:
    for kind in ("signedinUser", "anonymousUser", "phoneUser"):
        info = p.get(kind)
        if isinstance(info, Mapping) and str(info.get("displayName") or "").strip():
            return " ".join(str(info["displayName"]).split())
    return ""


def speakers_from(participants: Iterable[Mapping[str, Any]], entries: Sequence[Mapping[str, Any]]) -> list[Speaker]:
    """One speaker per participant that spoke (plus labels for speakers missing from the list)."""
    names = {participant_id(p["name"]): participant_name(p) for p in participants if p.get("name")}
    out: dict[str, Speaker] = {}
    unknown = 0
    for e in entries:
        pid = participant_id(e.get("participant") or "")
        if pid in out:
            continue
        name = names.get(pid) or ""
        if not name:
            unknown += 1
            name = f"Participant {unknown}"
        out[pid] = Speaker(pid, name)
    return list(out.values())


def to_utterances(entries: Sequence[Mapping[str, Any]], speakers: Sequence[Speaker],
                  conference_start: datetime) -> list[Utterance]:
    by_id = {s.user_id: s for s in speakers}
    out: list[Utterance] = []
    for e in entries:
        text = " ".join(str(e.get("text") or "").split())
        start = parse_time(e.get("startTime"))
        if not text or start is None:
            continue
        end = parse_time(e.get("endTime")) or start
        t0 = max(0.0, (start - conference_start).total_seconds())
        t1 = max(t0, (end - conference_start).total_seconds())
        pid = participant_id(e.get("participant") or "")
        speaker = by_id.get(pid)
        out.append(Utterance(round(t0, 3), round(t1, 3), pid, speaker.name if speaker else pid, text,
                             confidence=1.0))
    out.sort(key=lambda u: (u.t0, u.speaker_id))
    return out


def majority_language(entries: Iterable[Mapping[str, Any]]) -> Optional[str]:
    """Most frequent ``languageCode`` shortened to the ISO 639-1 form the rest of the plugin uses."""
    counts = Counter(str(e.get("languageCode") or "").split("-")[0].split("_")[0].lower()
                     for e in entries if e.get("languageCode"))
    counts.pop("", None)
    return counts.most_common(1)[0][0] if counts else None


def default_title(start: datetime, meeting_code: str = "") -> str:
    label = f"Google Meet · {start:%Y-%m-%d %H:%M}"
    return f"{label} · {meeting_code}" if meeting_code else label
