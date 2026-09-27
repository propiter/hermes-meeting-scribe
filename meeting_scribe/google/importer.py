"""Google Meet importer and poller (DESIGN §17).

``MeetImporter.sync`` lists finished conference records, and for each one not imported yet whose
transcript is ready, reads entries + participants, converts them and hands the meeting to
:meth:`MeetingService.import_transcript` (state ``transcribed`` → ANALYZE is queued; the existing
runner does the rest). Idempotency is the storage's ``(source, external_id)`` unique index, so two
processes, a restart or an overlapping manual ``google sync`` can never import a record twice.

Transcript readiness: only transcripts in state ``FILE_GENERATED`` are imported. ``ENDED`` means
"the session ended but the file is not generated yet"; entries MAY already be listable, but they
can still be incomplete, so the record is retried on the next poll instead (Google usually gets
there within minutes). ``STARTED`` / no transcript at all → retried while the record is inside the
window; a record without any transcript is never an error (transcription was simply off).

``MeetPoller`` runs ``sync`` on its OWN daemon thread (never on the Discord asyncio loop) every
``google_meet_poll_minutes``, only while holding the ``google-meet-poll`` lease in SQLite, so in a
multi-process install exactly one process polls. ``stop()`` wakes and joins it (reload-safe).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from ..domain.ids import short_id
from ..domain.models import SOURCE_GOOGLE_MEET, Meeting, MeetingState
from . import convert
from .meet_api import MeetApiError, MeetClient, MeetForbidden, MeetRetryLater
from .oauth import GoogleAuthError, GoogleDisconnected

log = logging.getLogger(__name__)
LEASE = "google-meet-poll"
KV = "google."
READY_STATES = frozenset({"FILE_GENERATED"})
RETENTION_DAYS = 30  # Meet deletes conference records and transcript entries 30 days after the end


@dataclass
class SyncReport:
    listed: int = 0
    imported: list[str] = field(default_factory=list)       # meeting ids
    already: int = 0
    pending: list[str] = field(default_factory=list)        # records whose transcript is not ready
    no_transcript: int = 0
    empty: int = 0
    errors: list[str] = field(default_factory=list)
    would_import: list[str] = field(default_factory=list)   # dry-run
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"listed": self.listed, "imported": self.imported, "already_imported": self.already,
                "pending": self.pending, "no_transcript": self.no_transcript, "empty": self.empty,
                "errors": self.errors, "would_import": self.would_import, "dry_run": self.dry_run}


class MeetImporter:
    def __init__(self, *, service: Callable[[], Any], client: Callable[[], MeetClient],
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._service = service
        self._client = client
        self._clock = clock

    # -- status (kv) ------------------------------------------------------------------------------
    def _set(self, **values: Any) -> None:
        repo = self._service().repo
        for k, v in values.items():
            repo.kv_set(KV + k, None if v is None else str(v))

    def status(self) -> dict[str, str]:
        start = len(KV)
        return {k[start:]: v for k, v in self._service().repo.kv_prefix(KV).items()}

    # -- sync -------------------------------------------------------------------------------------
    def window_start(self, *, since: Optional[datetime] = None, days: Optional[int] = None,
                     connected_at: Optional[float] = None) -> datetime:
        now = self._clock()
        oldest = now - timedelta(days=RETENTION_DAYS)
        if since is not None:
            start = since
        elif days is not None:
            start = now - timedelta(days=max(0, days))
        elif connected_at:
            start = datetime.fromtimestamp(float(connected_at), timezone.utc)
        else:
            start = now  # never connected: nothing is "new" yet
        return max(start, oldest)

    def sync(self, *, ended_after: datetime, dry_run: bool = False, record_status: bool = True) -> SyncReport:
        report = SyncReport(dry_run=dry_run)
        svc = self._service()
        try:
            client = self._client()
            records = client.conference_records(ended_after=convert.rfc3339(ended_after))
            report.listed = len(records)
            known = svc.repo.known_external_ids(SOURCE_GOOGLE_MEET)
            for rec in sorted(records, key=lambda r: str(r.get("endTime") or "")):
                name = str(rec.get("name") or "")
                if not name:
                    continue
                if name in known:
                    report.already += 1
                    continue
                self._one(client, rec, report, dry_run)
        except (GoogleDisconnected, GoogleAuthError, MeetApiError) as exc:
            report.errors.append(_describe(exc))
        if record_status and not dry_run:
            now = self._clock().isoformat()
            self._set(last_poll_at=now, last_poll_ok="0" if report.errors else "1",
                      last_error=report.errors[0] if report.errors else None)
            if report.imported:
                self._set(last_import_at=now, last_import_meeting=report.imported[-1])
        return report

    def _one(self, client: MeetClient, rec: dict, report: SyncReport, dry_run: bool) -> None:
        name = str(rec["name"])
        transcripts = client.transcripts(name)
        if not transcripts:
            report.no_transcript += 1
            return
        ready = [t for t in transcripts if t.get("state") in READY_STATES]
        if len(ready) < len(transcripts):
            report.pending.append(name)  # some still STARTED/ENDED: wait for all of them
            return
        start = convert.parse_time(rec.get("startTime"))
        end = convert.parse_time(rec.get("endTime"))
        if start is None:
            report.errors.append(f"{name}: no startTime")
            return
        entries: list[dict] = []
        for tr in sorted(ready, key=lambda t: str(t.get("startTime") or "")):
            entries.extend(client.entries(str(tr["name"])))
        if not entries:
            report.empty += 1
            return
        if dry_run:
            report.would_import.append(name)
            return
        participants = client.participants(name)
        speakers = convert.speakers_from(participants, entries)
        utterances = convert.to_utterances(entries, speakers, start)
        code = ""
        space = str(rec.get("space") or "")
        if space:
            try:
                code = str(client.space(space).get("meetingCode") or "")
            except MeetApiError as exc:  # label only: never block the import on it
                log.info("meeting-scribe: Meet space lookup failed: %s", _describe(exc))
        meeting = Meeting(
            id=short_id(), guild_id="", channel_id=f"gmeet:{space.rsplit('/', 1)[-1] or name}",
            channel_name=code or "Google Meet", started_at=start, ended_at=end or start,
            state=MeetingState.TRANSCRIBED, title=convert.default_title(start, code), speakers=tuple(speakers),
            language=convert.majority_language(entries), source=SOURCE_GOOGLE_MEET, external_id=name)
        created = self._service().import_transcript(meeting, utterances)
        if created is None:
            report.already += 1
        else:
            report.imported.append(created.id)
            log.info("meeting-scribe: imported Google Meet conference as meeting %s (%d lines)",
                     created.id, len(utterances))


def _describe(exc: BaseException) -> str:
    if isinstance(exc, GoogleDisconnected):
        return f"disconnected: {exc}"
    if isinstance(exc, MeetForbidden):
        return f"forbidden (check the Meet API is enabled, the scope was granted and admin policy): {exc}"
    if isinstance(exc, MeetRetryLater):
        return f"temporary: {exc}"
    return f"{type(exc).__name__}: {exc}"


class MeetPoller:
    """Daemon thread; one per runtime. Only the lease holder syncs."""

    def __init__(self, *, importer: Callable[[], Optional[MeetImporter]], repo: Callable[[], Any],
                 settings: Callable[[], Any], connected_at: Callable[[], Optional[float]], owner: str,
                 spawner: Optional[Callable[..., threading.Thread]] = None) -> None:
        self._importer = importer
        self._repo = repo
        self._settings = settings
        self._connected_at = connected_at
        self.owner = owner
        self._spawner = spawner
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def interval(self) -> float:
        return max(2, int(self._settings().google_meet_poll_minutes)) * 60.0

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            spawn = self._spawner or (lambda target, *, name, daemon: threading.Thread(target=target, name=name,
                                                                                        daemon=daemon))
            self._thread = spawn(self._loop, name="meeting-scribe-gmeet", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            self._stop.set()
            thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        try:
            self._repo().release_lease(LEASE, self.owner)
        except Exception:  # repo already closed on unload
            pass

    def tick(self) -> Optional[SyncReport]:
        """One poll if enabled and we hold the lease; ``None`` when skipped."""
        if not self._settings().google_meet_enabled:
            return None
        repo = self._repo()
        if not repo.acquire_lease(LEASE, self.owner, ttl=self.interval() * 3):
            return None
        importer = self._importer()
        if importer is None:
            return None
        start = importer.window_start(connected_at=self._connected_at())
        return importer.sync(ended_after=start)

    def _loop(self) -> None:
        delay = 5.0  # first poll shortly after connect, not in the middle of startup
        while not self._stop.wait(delay):
            delay = self.interval()
            try:
                report = self.tick()
            except Exception:  # never let the thread die: next cycle retries
                log.exception("meeting-scribe: Google Meet poll failed")
                continue
            if report is not None and report.errors:
                log.warning("meeting-scribe: Google Meet poll: %s", "; ".join(report.errors)[:500])
                if any(e.startswith("disconnected") for e in report.errors):
                    delay = max(delay, 3600.0)  # nothing will work until `google connect`: poll hourly
