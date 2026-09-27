"""Read side of the Desktop page: bounded, path-safe DTOs straight from the profile's SQLite + files.

Runs in the dashboard process. Nothing here builds a ``Runtime``, a pipeline, a worker or a capture
controller, and nothing returns a secret (Google tokens / client secret, API keys) or an internal
path other than the one audio file the host media player needs.
"""
from __future__ import annotations

import math

import base64
import binascii
import json
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from ..domain.models import KV_DM_NOTES, SOURCE_DISCORD, SOURCE_GOOGLE_MEET, MeetingState
from ..llm_config import redact, safe_url
from ..storage.layout import Layout
from ..storage.repo import Repository
from .control import HEARTBEAT_KV

SOURCES = (SOURCE_DISCORD, SOURCE_GOOGLE_MEET)
STATES = tuple(s.value for s in MeetingState)
# Groups the page filters by; every MeetingState belongs to exactly one.
STATE_GROUPS: dict[str, tuple[str, ...]] = {
    "recording": ("recording",),
    "processing": ("captured", "transcribing", "transcribed", "analyzing", "analyzed", "delivering"),
    "done": ("done",),
    "failed": ("failed",),
}
WAITING_KV = "pipeline.waiting_destination."
ARTIFACTS = ("notes.json", "recording.ogg", "recording.mka", "transcript.json")
WORKER_RECENT_SECONDS = 180
_HIDDEN_MEETING_KEYS = ("folder", "started_by", "external_id")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# -- cursors --------------------------------------------------------------------------------------
def encode_cursor(values: list[Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode()


def decode_cursor(value: str, kinds: tuple[type, ...]) -> Optional[list[Any]]:
    """Opaque keyset cursor; anything malformed is a ``ValueError`` (HTTP 400), never a crash."""
    if not value:
        return None
    try:
        data = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise ValueError("invalid cursor") from exc
    if not isinstance(data, list) or len(data) != len(kinds) or \
            not all(isinstance(v, k) and not isinstance(v, bool) for v, k in zip(data, kinds)):
        raise ValueError("invalid cursor")
    for v, kind in zip(data, kinds):
        if (kind is int and not -(2**63) <= v < 2**63) or (kind is float and not math.isfinite(v)):
            raise ValueError("invalid cursor")
    return data


def _day(value: str, name: str) -> Optional[date]:
    if not value:
        return None
    if not _DATE_RE.match(value):
        raise ValueError(f"{name} must be YYYY-MM-DD")
    return date.fromisoformat(value)


def public_meeting(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k not in _HIDDEN_MEETING_KEYS}


class Library:
    def __init__(self, repo: Repository, root: Path) -> None:
        self.repo, self.root = repo, Path(root)

    def require(self, mid: str) -> Any:
        meeting = self.repo.get_meeting(mid)
        if meeting is None:
            raise KeyError(mid)
        return meeting

    # -- library ------------------------------------------------------------------------------
    def meetings(self, *, limit: int = 30, cursor: str = "", q: str = "", source: str = "", state: str = "",
                 since: str = "", until: str = "") -> dict[str, Any]:
        """Newest first. ``q`` matches the title or any transcript line (FTS5); ``state`` is a state or
        a group (``processing``); ``since``/``until`` are inclusive UTC days."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        where, params = ["1=1"], []
        if source:
            if source not in SOURCES:
                raise ValueError("unknown source")
            where.append("source=?")
            params.append(source)
        if state:
            states = STATE_GROUPS.get(state) or ((state,) if state in STATES else None)
            if states is None:
                raise ValueError("unknown state")
            where.append(f"state IN ({','.join('?' * len(states))})")
            params.extend(states)
        start, end = _day(since, "since"), _day(until, "until")
        if start and end and end < start:
            raise ValueError("until is before since")
        if start:
            where.append("started_at >= ?")
            params.append(start.isoformat())
        if end:
            where.append("started_at < ?")
            params.append((end + timedelta(days=1)).isoformat())
        q = q.strip()
        if q:
            words = re.findall(r"\w+", q, re.UNICODE)
            clause = "instr(lower(title), lower(?)) > 0"
            params.append(q)
            if words:  # each word quoted: user text can never become FTS5 syntax
                clause = f"({clause} OR id IN (SELECT meeting_id FROM utterances_fts WHERE utterances_fts MATCH ?))"
                params.append(" ".join('"' + w + '"' for w in words))
            where.append(clause)
        after = decode_cursor(cursor, (str, str))
        if after:
            where.append("(started_at, id) < (?, ?)")
            params.extend(after)
        rows = self.repo._x("SELECT data, started_at, id FROM meetings WHERE " + " AND ".join(where) +
                            " ORDER BY started_at DESC, id DESC LIMIT ?", (*params, limit + 1)).fetchall()
        page = rows[:limit]
        items = []
        for r in page:
            data = public_meeting(json.loads(r["data"]))
            job = self.repo.get_job(r["id"])
            data["job_state"] = job.state if job else None
            items.append(data)
        more = len(rows) > limit
        return {"items": items,
                "next_cursor": encode_cursor([page[-1]["started_at"], page[-1]["id"]]) if more else None}

    def facets(self) -> dict[str, Any]:
        """Counts for the filter bar (whole library, not the current page)."""
        by_state = {r["state"]: r["n"] for r in
                    self.repo._x("SELECT state, COUNT(*) AS n FROM meetings GROUP BY state").fetchall()}
        by_source = {r["source"]: r["n"] for r in
                     self.repo._x("SELECT source, COUNT(*) AS n FROM meetings GROUP BY source").fetchall()}
        return {"total": sum(by_state.values()),
                "states": {g: sum(by_state.get(s, 0) for s in members) for g, members in STATE_GROUPS.items()},
                "sources": {s: by_source.get(s, 0) for s in SOURCES}}

    # -- one meeting --------------------------------------------------------------------------
    def artifact(self, mid: str, name: str) -> Path:
        """A known file of the meeting's folder, refusing a poisoned folder or a symlink."""
        if name not in ARTIFACTS:
            raise ValueError("unknown artifact")
        meeting = self.require(mid)
        layout = Layout(lambda: self.root)
        folder = layout.meeting_folder(meeting).resolve()  # Layout.resolve refuses escapes
        meetings_root = layout.meetings_dir().resolve()
        if meetings_root not in folder.parents:
            raise ValueError("unsafe artifact")
        path = folder / name
        if path.is_symlink() or (path.exists() and path.resolve().parent != folder):
            raise ValueError("unsafe artifact")
        return path

    def detail(self, mid: str) -> dict[str, Any]:
        from ..domain.models import Notes

        meeting = self.require(mid)
        notes_path = self.artifact(mid, "notes.json")
        notes = None
        if notes_path.is_file():
            try:
                notes = Notes.from_dict(json.loads(notes_path.read_text(encoding="utf-8"))).to_dict()
            except (ValueError, KeyError, TypeError):
                notes = None  # a damaged file must not break the page; the transcript still shows
        return {"meeting": public_meeting(meeting.to_dict()), "notes": notes, "tasks": self.tasks(mid),
                "transcript_total": self.repo.utterance_count(mid), "job": self.job(mid),
                "waiting_destination": redact(self.repo.kv_get(WAITING_KV + mid) or "") or None,
                "dm_notes": redact(self.repo.kv_get(KV_DM_NOTES + mid) or "") or None,
                "audio": self.audio(mid), "command": self.last_command(mid)}

    def tasks(self, mid: str) -> list[dict[str, Any]]:
        out = []
        for item in self.repo.list_action_items(mid):
            sinks = {}
            for sink in ("kanban", "linear"):
                delivery = self.repo.get_delivery(sink, f"mtg:{mid}:{item.id}") or {}
                url = str(delivery.get("url") or "")
                sinks[sink] = {"status": self.repo.item_sink_status(mid, item.id, sink) or "pending",
                               # only https links reach the page (never javascript:/file:/data:)
                               "url": safe_url(url) if url.startswith("https://") else ""}
            out.append({**item.to_dict(), "sinks": sinks, "discord": self._discord_task(mid, item.id)})
        return out

    def _discord_task(self, mid: str, item_id: str) -> Optional[dict[str, Any]]:
        """Where the gateway posted the task in Discord (pointer written by the notes publisher)."""
        row = self.repo.get_delivery("discord", f"mtg:{mid}:task:{item_id}") or {}
        try:
            ptr = json.loads(row.get("external_id") or "null")
        except ValueError:
            ptr = None
        if not isinstance(ptr, dict):
            return None
        url = str(row.get("url") or ptr.get("url") or "")
        return {"channel_id": str(ptr.get("target") or ptr.get("channel") or "") or None,
                "url": url if url.startswith("https://") else ""}

    def audio(self, mid: str) -> dict[str, Any]:
        mixed = self.artifact(mid, "recording.ogg")
        if mixed.is_file():
            return {"available": True, "reason": "mixed", "path": str(mixed), "bytes": mixed.stat().st_size}
        if self.artifact(mid, "recording.mka").is_file():
            return {"available": False, "reason": "multitrack"}
        source = self.require(mid).source
        return {"available": False, "reason": "imported" if source == SOURCE_GOOGLE_MEET else "not_retained"}

    def job(self, mid: str) -> Optional[dict[str, Any]]:
        job = self.repo.get_job(mid)
        if job is None:
            return None
        return {"state": job.state, "stage": job.stage.value, "attempts": job.attempts,
                "failed_stage": job.failed_stage.value if job.failed_stage else None,
                "error": redact(job.error or "")[:2000], "next_retry_at": job.next_retry_at,
                "heartbeat": job.heartbeat}

    def last_command(self, mid: str) -> Optional[dict[str, Any]]:
        row = self.repo._x("SELECT id FROM desktop_commands WHERE meeting_id=? ORDER BY created_at DESC LIMIT 1",
                           (mid,)).fetchone()
        if row is None:
            return None
        from .control import Commands

        return Commands(self.repo).get(row["id"])

    def transcript(self, mid: str, *, limit: int = 200, cursor: str = "") -> dict[str, Any]:
        self.require(mid)
        if not 1 <= limit <= 500:
            raise ValueError("limit must be 1..500")
        after = decode_cursor(cursor, (float, int))
        where, params = "", [mid]
        if after:
            where = " AND (t0, id) > (?, ?)"
            params.extend(after)
        rows = self.repo._x("SELECT id, t0, t1, speaker_id, speaker, text FROM utterances WHERE meeting_id=?" +
                            where + " ORDER BY t0, id LIMIT ?", (*params, limit + 1)).fetchall()
        page = rows[:limit]
        more = len(rows) > limit
        return {"items": [dict(r) for r in page], "total": self.repo.utterance_count(mid),
                "next_cursor": encode_cursor([float(page[-1]["t0"]), int(page[-1]["id"])]) if more else None}

    # -- processing status --------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        raw = self.repo.kv_get(HEARTBEAT_KV)
        try:
            seen: Optional[float] = float(raw) if raw else None
        except ValueError:
            seen = None
        age = None if seen is None else time.time() - seen
        worker = "unknown" if age is None else "recent" if 0 <= age < WORKER_RECENT_SECONDS else "stale"
        titles = {}

        def title(mid: str) -> str:
            if mid not in titles:
                m = self.repo.get_meeting(mid)
                titles[mid] = (m.title or m.channel_name) if m else mid
            return titles[mid]
        jobs = [{"meeting_id": j.meeting_id, "title": title(j.meeting_id), **(self.job(j.meeting_id) or {})}
                for j in self.repo.list_jobs(("running", "queued", "failed"))]
        commands = [dict(r) for r in self.repo._x(
            "SELECT id, meeting_id, state, error, created_at, updated_at FROM desktop_commands "
            "ORDER BY created_at DESC LIMIT 10").fetchall()]
        return {"worker": {"state": worker, "last_seen": seen},
                "counts": {s: sum(1 for j in jobs if j["state"] == s) for s in ("running", "queued", "failed")},
                "jobs": jobs[:100],
                "waiting_destination": [{"meeting_id": k[len(WAITING_KV):], "title": title(k[len(WAITING_KV):]),
                                         "detail": redact(v)} for k, v in self.repo.kv_prefix(WAITING_KV).items()],
                "dm_notes": [{"meeting_id": k[len(KV_DM_NOTES):], "title": title(k[len(KV_DM_NOTES):]),
                              "detail": redact(v)} for k, v in self.repo.kv_prefix(KV_DM_NOTES).items()],
                "commands": [{**c, "error": redact(c["error"] or "")} for c in commands]}


def google_status(root: Path, repo: Repository, enabled: bool) -> dict[str, Any]:
    """Connection state from the per-profile files, WITHOUT any token or client field."""
    from ..google.importer import KV
    from ..google.oauth import GoogleFiles

    files = GoogleFiles(lambda: root)
    token = files.read_token() or {}
    client = files.client_path.exists()
    connected = bool(token.get("refresh_token")) and not token.get("disconnected") and client
    safe_keys = ("last_poll_at", "last_poll_ok", "last_error", "last_import_at", "last_import_meeting",
                 "records_given_up", "records_given_up_last", "retry_after_until")
    importer = {k[len(KV):]: v for k, v in repo.kv_prefix(KV).items()}
    out: dict[str, Any] = {"enabled": enabled, "client_stored": client, "connected": connected,
                           "revoked": bool(token.get("disconnected")),
                           "connected_at": token.get("connected_at") if isinstance(token.get("connected_at"),
                                                                                  (int, float)) else None,
                           "commands": {"connect": "hermes meeting-scribe google connect --client-secret <client.json>",
                                        "status": "hermes meeting-scribe google status",
                                        "enable": "hermes meeting-scribe config set google_meet_enabled true"}}
    out.update({k: redact(str(importer[k])) for k in safe_keys if importer.get(k) not in (None, "")})
    return out
