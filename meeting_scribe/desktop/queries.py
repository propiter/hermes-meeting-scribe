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
from typing import Any, Callable, Optional

from ..privacy import DM_UNREACHABLE_KV
from ..domain.models import KV_DM_NOTES, SOURCE_DISCORD, SOURCE_GOOGLE_MEET, MeetingState
from ..llm_config import redact, safe_url
from ..storage.layout import Layout
from ..storage.owner import capturing
from ..storage.repo import Repository, meeting_dict_from_row
from .control import HEARTBEAT_KV

SOURCES = (SOURCE_DISCORD, SOURCE_GOOGLE_MEET)
STATES = tuple(s.value for s in MeetingState)
# Groups the page filters by; every MeetingState belongs to exactly one.
STATE_GROUPS: dict[str, tuple[str, ...]] = {
    "recording": ("recording",),
    "processing": ("captured", "transcribing", "transcribed", "analyzing", "analyzed", "delivering"),
    "done": ("done",),
    "failed": ("failed",),
    "empty": ("empty",),  # nobody was heard: discarded, never "needs attention"
}
WAITING_KV = "pipeline.waiting_destination."
ARTIFACTS = ("notes.json", "recording.ogg", "recording.mka", "playback.ogg", "transcript.json")
# What an operator can do about a failure, by the words found in the (redacted) error. The page shows a
# plain-language sentence for the category; the raw text stays available under «Details».
_PROBLEMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ffmpeg", ("ffmpeg", "ffprobe")),
    ("no_audio", ("no audio", "notracks", "no tracks")),
    ("model", ("faster_whisper", "faster-whisper", "whisper", "ctranslate", "cuda", "out of memory")),
    ("llm", ("llm", "model returned", "json", "openrouter", "anthropic", "openai", "timeout", "timed out", "429",
             "rate limit")),
    ("destination", ("channel", "destination", "discord", "forbidden", "missing access")),
    ("kanban", ("kanban",)),
    ("linear", ("linear",)),
)
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


def problem_kind(error: str) -> str:
    """Category of a pipeline error for the page's plain-language explanation."""
    low = (error or "").lower()
    for kind, words in _PROBLEMS:
        if any(w in low for w in words):
            return kind
    return "unknown"


def _people(data: dict[str, Any]) -> int:
    return sum(1 for s in data.get("speakers") or () if isinstance(s, dict) and not s.get("is_bot"))


class Library:
    """The Desktop library of ONE space (DESIGN §23); ``space=None`` only for single-space tests of
    the pre-spaces surface. A meeting of another space is "not found", never shown."""

    def __init__(self, repo: Repository, root: Path, space: Optional[str] = None,
                 settings: Optional[Callable[[str], Any]] = None) -> None:
        """``settings(space)``: that space's settings, to mark meetings a private rule covers (DESIGN
        §19.2); without it only meetings already published as private are marked."""
        self.repo, self.root, self.space = repo, Path(root), space
        self._settings = settings

    def private(self, meeting: Any) -> bool:
        """Private meeting (Desktop shows it, with everything: the operator's own library)."""
        from ..privacy import is_private, record

        if self._settings is None:
            return record(self.repo, meeting.id) is not None
        return is_private(self.repo, self._settings(meeting.space), meeting)

    def _mine(self, mid: str) -> bool:
        if self.space is None:
            return True
        m = self.repo.get_meeting(mid)
        return m is not None and m.space == self.space

    def require(self, mid: str) -> Any:
        meeting = self.repo.get_meeting(mid)
        if meeting is None or (self.space is not None and meeting.space != self.space):
            raise KeyError(mid)
        return meeting

    def _scope(self) -> tuple[str, tuple[Any, ...]]:
        return ("1=1", ()) if self.space is None else ("space=?", (self.space,))

    # -- library ------------------------------------------------------------------------------
    def meetings(self, *, limit: int = 30, cursor: str = "", q: str = "", source: str = "", state: str = "",
                 since: str = "", until: str = "", channel: str = "", project: str = "") -> dict[str, Any]:
        """Newest first. ``q`` matches the title, the project or any transcript line (FTS5); ``state``
        is a state or a group (``processing``); ``since``/``until`` are inclusive UTC days; ``channel``
        is a channel id; ``project`` a project name (the meeting's or one of its tasks')."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        scope, scope_params = self._scope()
        where, params = [scope], list(scope_params)
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
        if channel:
            where.append("channel_id=?")
            params.append(channel[:200])
        if project:
            where.append("(lower(json_extract(data, '$.project'))=lower(?) OR id IN (SELECT meeting_id FROM "
                         "action_items WHERE lower(json_extract(data, '$.project'))=lower(?)))")
            params.extend([project[:200], project[:200]])
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
            clause = ("(instr(lower(title), lower(?)) > 0 OR instr(lower(coalesce(json_extract(data, '$.project'), "
                      "'')), lower(?)) > 0)")
            params.extend([q, q])
            if words:  # each word quoted: user text can never become FTS5 syntax
                clause = f"({clause} OR id IN (SELECT meeting_id FROM utterances_fts WHERE utterances_fts MATCH ?))"
                params.append(" ".join('"' + w + '"' for w in words))
            where.append(clause)
        after = decode_cursor(cursor, (str, str))
        if after:
            where.append("(started_at, id) < (?, ?)")
            params.extend(after)
        rows = self.repo._x("SELECT data, source, external_id, space, started_at, id FROM meetings WHERE " + " AND ".join(where) +
                            " ORDER BY started_at DESC, id DESC LIMIT ?", (*params, limit + 1)).fetchall()
        page = rows[:limit]
        items = []
        for r in page:
            data = public_meeting(meeting_dict_from_row(r))  # source from the column (authoritative)
            job = self.repo.get_job(r["id"])
            data["job_state"] = job.state if job else None
            data["job_stage"] = job.stage.value if job else None
            data["people"] = _people(data)
            data["task_count"] = int(self.repo._x("SELECT COUNT(*) FROM action_items WHERE meeting_id=? AND "
                                                  "status!='dismissed'", (r["id"],)).fetchone()[0])
            data["waiting_destination"] = self.repo.kv_get(WAITING_KV + r["id"]) is not None
            meeting = self.repo.get_meeting(r["id"])
            data["private"] = meeting is not None and self.private(meeting)
            items.append(data)
        more = len(rows) > limit
        return {"items": items,
                "next_cursor": encode_cursor([page[-1]["started_at"], page[-1]["id"]]) if more else None}

    def facets(self) -> dict[str, Any]:
        """Counts for the filter bar (whole library, not the current page)."""
        scope, sp = self._scope()
        by_state = {r["state"]: r["n"] for r in
                    self.repo._x(f"SELECT state, COUNT(*) AS n FROM meetings WHERE {scope} GROUP BY state", sp).fetchall()}
        by_source = {r["source"]: r["n"] for r in
                     self.repo._x(f"SELECT source, COUNT(*) AS n FROM meetings WHERE {scope} GROUP BY source",
                                  sp).fetchall()}
        channels = [{"id": r["channel_id"], "name": r["name"] or r["channel_id"], "count": r["n"]} for r in self.repo._x(
            "SELECT channel_id, max(json_extract(data, '$.channel_name')) AS name, COUNT(*) AS n FROM meetings "
            f"WHERE channel_id != '' AND {scope} GROUP BY channel_id ORDER BY n DESC, name LIMIT 200", sp).fetchall()]
        projects = {str(r["p"]): int(r["n"]) for r in self.repo._x(
            "SELECT p, COUNT(DISTINCT mid) AS n FROM (SELECT id AS mid, json_extract(data, '$.project') AS p FROM "
            f"meetings WHERE {scope} UNION ALL SELECT meeting_id, json_extract(data, '$.project') FROM action_items "
            f"WHERE meeting_id IN (SELECT id FROM meetings WHERE {scope})) "
            "WHERE p IS NOT NULL AND p != '' GROUP BY lower(p)", (*sp, *sp)).fetchall()}
        return {"total": sum(by_state.values()),
                "states": {g: sum(by_state.get(s, 0) for s in members) for g, members in STATE_GROUPS.items()},
                "sources": {s: by_source.get(s, 0) for s in SOURCES},
                "channels": channels,
                "projects": [{"name": k, "count": v} for k, v in sorted(projects.items(), key=lambda kv: kv[0].lower())][:200]}

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
        tasks = self.tasks(mid)
        projects = sorted({p for p in [meeting.project, *(t.get("project") for t in tasks)] if p}, key=str.lower)
        public = public_meeting(meeting.to_dict())
        public["people"] = _people(public)
        public["missing_audio_names"] = list(meeting.missing_audio_names)
        public["private"] = self.private(meeting)
        public["speaker_tracks"] = self.speaker_tracks(meeting)
        return {"meeting": public, "notes": notes, "tasks": tasks, "projects": projects,
                "transcript_total": self.repo.utterance_count(mid), "job": self.job(mid),
                "history": self.history(mid),
                "waiting_destination": redact(self.repo.kv_get(WAITING_KV + mid) or "") or None,
                "dm_notes": redact(self.repo.kv_get(KV_DM_NOTES + mid) or "") or None,
                "audio": self.audio(mid), "command": self.last_command(mid)}

    def speaker_tracks(self, meeting: Any) -> dict[str, Any]:
        """The "unidentified participant" tracks (interval, lines, owner once assigned) and who they may
        be given to (DESIGN §4.1): each track is one Discord voice connection, one person."""
        from ..pipeline.speakers import candidates, tracks

        folder = Layout(lambda: self.root).meeting_folder(meeting)
        return {"tracks": [tr.to_dict() for tr in tracks(self.repo, folder, meeting)],
                "audit": self.repo.speaker_history(meeting.id),
                "people": [{"id": s.user_id, "name": s.name} for s in candidates(meeting)]}

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
            kanban = self.repo.get_delivery("kanban", f"mtg:{mid}:{item.id}") or {}
            sinks["kanban"]["task_id"] = str(kanban.get("external_id") or "") or None
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
        """What the player can stream. ``multitrack`` alone (an archive made before listening copies
        existed) can be prepared on demand: ``prepare_audio`` command, run by the gateway."""
        for name in ("playback.ogg", "recording.ogg"):
            path = self.artifact(mid, name)
            if path.is_file():
                return {"available": True, "reason": "ready", "path": str(path), "bytes": path.stat().st_size,
                        "original": self.artifact(mid, "recording.mka").is_file()}
        if self.artifact(mid, "recording.mka").is_file():
            return {"available": False, "reason": "multitrack", "can_prepare": True, "original": True}
        meeting = self.require(mid)
        if meeting.source == SOURCE_GOOGLE_MEET:
            return {"available": False, "reason": "imported"}
        if meeting.state.value == "recording":
            return {"available": False, "reason": "recording"}
        if meeting.state.value == "empty":
            return {"available": False, "reason": "empty"}
        return {"available": False, "reason": "not_retained"}

    def history(self, mid: str) -> list[dict[str, Any]]:
        """Timeline for the «Processing» tab, newest last: meeting start/end, the job's last attempt,
        waiting for a destination and every operator command. Built from existing rows (no event log)."""
        meeting = self.require(mid)
        events: list[dict[str, Any]] = [{"kind": "started", "at": meeting.started_at.timestamp()}]
        if meeting.ended_at:
            events.append({"kind": "ended", "at": meeting.ended_at.timestamp(), "partial": meeting.partial})
        row = self.repo._x("SELECT created_at, updated_at FROM jobs WHERE meeting_id=?", (mid,)).fetchone()
        job = self.repo.get_job(mid)
        if row is not None and job is not None:
            events.append({"kind": "queued", "at": row["created_at"]})
            kind = {"done": "processed", "failed": "failed", "running": "running", "queued": "waiting"}.get(job.state,
                                                                                                             job.state)
            item: dict[str, Any] = {"kind": kind, "at": row["updated_at"], "stage": job.stage.value,
                                    "attempts": job.attempts}
            if job.state == "failed" or (job.error and job.state == "queued"):
                item["problem"] = problem_kind(job.error or "")
                item["stage"] = (job.failed_stage or job.stage).value
            events.append(item)
        if self.repo.kv_get(WAITING_KV + mid) is not None:
            events.append({"kind": "waiting_destination", "at": row["updated_at"] if row is not None else None})
        for c in self.repo._x("SELECT id, body, state, error, created_at, updated_at FROM desktop_commands "
                              "WHERE meeting_id=? ORDER BY created_at", (mid,)).fetchall():
            f = _command_fields(c)
            events.append({"kind": "command", "at": f["created_at"], "action": f["action"], "stage": f["stage"],
                           "state": f["state"], "updated_at": f["updated_at"]})
        return sorted(events, key=lambda e: e.get("at") or 0)

    def job(self, mid: str) -> Optional[dict[str, Any]]:
        job = self.repo.get_job(mid)
        if job is None:
            return None
        error = redact(job.error or "")[:2000]
        return {"state": job.state, "stage": job.stage.value, "attempts": job.attempts,
                "failed_stage": job.failed_stage.value if job.failed_stage else None,
                "error": error, "problem": problem_kind(error) if error else None,
                "next_retry_at": job.next_retry_at, "heartbeat": job.heartbeat}

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
                for j in self.repo.list_jobs(("running", "queued", "failed"), space=self.space)]
        scope, sp = self._scope()
        commands = [{**_command_fields(r), "meeting_id": r["meeting_id"], "title": title(r["meeting_id"])}
                    for r in self.repo._x("SELECT id, meeting_id, body, state, error, created_at, updated_at "
                                          f"FROM desktop_commands WHERE meeting_id IN (SELECT id FROM meetings WHERE "
                                          f"{scope}) ORDER BY created_at DESC LIMIT 10", sp).fetchall()]
        waiting = {k[len(WAITING_KV):]: v for k, v in self.repo.kv_prefix(WAITING_KV).items()
                   if self._mine(k[len(WAITING_KV):])}
        dm_notes = {k[len(KV_DM_NOTES):]: v for k, v in self.repo.kv_prefix(KV_DM_NOTES).items()
                    if self._mine(k[len(KV_DM_NOTES):])}
        unreachable = {k[len(DM_UNREACHABLE_KV):]: v for k, v in self.repo.kv_prefix(DM_UNREACHABLE_KV).items()
                       if self._mine(k[len(DM_UNREACHABLE_KV):])}
        recording = [{"meeting_id": m.id, "title": m.title or m.channel_name, "started_at": m.started_at.isoformat(),
                      "live": capturing(owner)} for m, owner in self.repo.recordings(self.space)]
        return {"worker": {"state": worker, "last_seen": seen}, "recording": recording,
                "counts": {s: sum(1 for j in jobs if j["state"] == s) for s in ("running", "queued", "failed")},
                "jobs": jobs[:100],
                "waiting_destination": [{"meeting_id": k, "title": title(k), "detail": redact(v)}
                                        for k, v in waiting.items()],
                "dm_notes": [{"meeting_id": k, "title": title(k), "detail": redact(v)} for k, v in dm_notes.items()],
                "dm_unreachable": [{"meeting_id": k, "title": title(k), "detail": redact(v)}
                                   for k, v in unreachable.items()],
                "commands": commands}


def _command_fields(row: Any) -> dict[str, Any]:
    """A desktop command row as the page shows it: WHAT was asked (action + stage), never the raw body."""
    try:
        body = json.loads(row["body"])
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return {"id": row["id"], "action": body.get("action"), "stage": body.get("stage"), "state": row["state"],
            "error": redact(row["error"] or ""), "created_at": row["created_at"], "updated_at": row["updated_at"]}


def _google_commands(repo: Repository, space: str) -> dict[str, str]:
    flag = f" --space {space}" if len(repo.list_spaces()) > 1 else ""
    return {"connect": f"hermes meeting-scribe google connect{flag} --client-secret <client.json>",
            "status": f"hermes meeting-scribe google status{flag}",
            "enable": f"hermes meeting-scribe config set google_meet_enabled true{flag}"}


def google_summary(status: dict[str, Any]) -> dict[str, Any]:
    """The short form ``GET /v1/spaces`` shows per space (never a token or client field)."""
    failing = status.get("last_poll_ok") == "0"
    return {"enabled": status["enabled"], "connected": status["connected"],
            "last_check": status.get("last_poll_at"), "last_import": status.get("last_import_at"),
            "error": status.get("last_error") if failing or status.get("revoked") else None}


def only_space(repo: Repository) -> Optional[str]:
    """The install's single space; ``None`` with none or several (the Desktop has no space selector
    yet — DESIGN §23: it never shows one team's data as if it were the only one)."""
    rows = repo.list_spaces()
    return rows[0].slug if len(rows) == 1 else None


def google_status(root: Path, repo: Repository, enabled: bool, space: str) -> dict[str, Any]:
    """``space``'s Google connection state from its files, WITHOUT any token or client field."""
    from ..google.importer import status_kv
    from ..google.oauth import GoogleFiles

    KV = status_kv(space)
    files = GoogleFiles(lambda: root, space)
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
                           "commands": _google_commands(repo, space)}
    out.update({k: redact(str(importer[k])) for k in safe_keys if importer.get(k) not in (None, "")})
    return out
