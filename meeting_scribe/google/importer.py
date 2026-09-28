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

Spaces (DESIGN §23): every space has its own Google connection, so there is one importer and one
poller PER SPACE. Its status, its ``Retry-After`` pause, its per-record memory and its lease are all
keyed by the space, so one team's expired token or quota never delays another team's imports, and
imported meetings carry their space from the start.

``MeetPoller`` runs ``sync`` on its OWN daemon thread (never on the Discord asyncio loop) every
``google_meet_poll_minutes``, only while holding the space's ``google-meet-poll:<space>`` lease in
SQLite, so in a multi-process install exactly one process polls each space. ``stop()`` wakes and
joins it (reload-safe).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from ..domain.ids import short_id
from ..domain.models import SOURCE_GOOGLE_MEET, Meeting, MeetingState
from . import convert
from .meet_api import MeetApiError, MeetAuthError, MeetClient, MeetForbidden, MeetRetryLater, SyncStopped
from .oauth import GoogleAuthError, GoogleDisconnected

log = logging.getLogger(__name__)
READY_STATES = frozenset({"FILE_GENERATED"})
RETENTION_DAYS = 30  # Meet deletes conference records and transcript entries 30 days after the end


def lease_name(space: str) -> str:
    return f"google-meet-poll:{space}"


def status_kv(space: str) -> str:
    """KV prefix of a space's poll status (``last_poll_at``, ``last_error``, ``retry_after_until``...)."""
    return f"google.{space}."


def record_kv(space: str) -> str:
    """KV prefix of a space's per-record memory: failures / settled without transcript."""
    return f"gmeet.record.{space}."
MAX_RECORD_FAILURES = 5  # permanent errors (403/404/malformed) on one record before it is given up
# A record still without any transcript this long after it ended never gets one (transcription was
# off: Meet creates the transcript resource while the meeting runs). It is settled and not re-read.
NO_TRANSCRIPT_SETTLE_SECONDS = 3600


@dataclass
class SyncReport:
    listed: int = 0
    imported: list[str] = field(default_factory=list)       # meeting ids
    already: int = 0
    pending: list[str] = field(default_factory=list)        # records whose transcript is not ready
    no_transcript: int = 0
    skipped: int = 0                                        # settled earlier (no transcript): not re-read
    given_up: list[str] = field(default_factory=list)       # failed MAX_RECORD_FAILURES times: not retried
    empty: int = 0
    errors: list[str] = field(default_factory=list)
    would_import: list[str] = field(default_factory=list)   # dry-run
    dry_run: bool = False
    stopped: bool = False                                   # interrupted by stop(): nothing half-imported

    def as_dict(self) -> dict[str, Any]:
        return {"listed": self.listed, "imported": self.imported, "already_imported": self.already,
                "pending": self.pending, "no_transcript": self.no_transcript, "skipped": self.skipped,
                "given_up": self.given_up, "empty": self.empty, "errors": self.errors,
                "would_import": self.would_import, "dry_run": self.dry_run}


class _AbortPass(Exception):
    """Internal: stop the whole pass (auth/quota/network), not just the current record."""


def _aborts_pass(exc: BaseException) -> bool:
    """Errors that make every other record fail too: stop the pass instead of hammering the API."""
    if isinstance(exc, (GoogleAuthError, MeetAuthError)):
        return True
    return isinstance(exc, MeetRetryLater) and (exc.status == 429 or exc.status == 0)


def _transient(exc: BaseException) -> bool:
    """A record error that does not count towards giving up on it (5xx: Google's side)."""
    return isinstance(exc, MeetRetryLater)


class MeetImporter:
    """Imports ONE space's Google Meet transcripts (its own connection, status and memory)."""

    def __init__(self, *, space: str, service: Callable[[], Any], client: Callable[[], MeetClient],
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.space = space
        self._service = service
        self._client = client
        self._clock = clock
        self._kv = status_kv(space)
        self._record_kv = record_kv(space)

    # -- status (kv) ------------------------------------------------------------------------------
    def set_status(self, **values: Any) -> None:
        repo = self._service().repo
        for k, v in values.items():
            repo.kv_set(self._kv + k, None if v is None else str(v))

    def status(self) -> dict[str, str]:
        start = len(self._kv)
        return {k[start:]: v for k, v in self._service().repo.kv_prefix(self._kv).items()}

    def now(self) -> datetime:
        return self._clock()

    def clean_leftovers(self, *, older_than: float) -> int:
        clean = getattr(self._service(), "clean_import_leftovers", None)
        return int(clean(older_than=older_than)) if callable(clean) else 0

    def backoff_until(self) -> Optional[datetime]:
        """End of a ``Retry-After`` pause requested by Google (429), if one is pending."""
        raw = self._service().repo.kv_get(self._kv + "retry_after_until")
        return convert.parse_time(raw) if raw else None

    # -- per-record memory (kv, outside the status prefix) ------------------------------------------
    def _record_state(self, repo: Any) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key, raw in repo.kv_prefix(self._record_kv).items():
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if isinstance(data, dict):
                out[key[len(self._record_kv):]] = data
        return out

    def _remember(self, repo: Any, name: str, data: Optional[dict[str, Any]]) -> None:
        repo.kv_set(self._record_kv + name, None if data is None else json.dumps(data, ensure_ascii=False))

    def _prune(self, repo: Any, memory: dict[str, dict[str, Any]]) -> None:
        """Forget records Meet itself has deleted (30 days after they ended)."""
        horizon = self._clock() - timedelta(days=RETENTION_DAYS + 1)
        for name, data in memory.items():
            end = convert.parse_time(data.get("end"))
            if end is not None and end < horizon:
                self._remember(repo, name, None)

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

    def sync(self, *, ended_after: datetime, dry_run: bool = False, record_status: bool = True,
             should_stop: Callable[[], bool] = lambda: False) -> SyncReport:
        """One pass. Never raises: every failure ends up in ``report.errors`` (and the status)."""
        report = SyncReport(dry_run=dry_run)
        try:
            self._sync(report, ended_after, dry_run, should_stop)
        except _AbortPass:
            pass
        except SyncStopped:  # unloading: the record in flight is simply retried by the next process/poll
            report.stopped = True
        except (GoogleDisconnected, GoogleAuthError, MeetApiError) as exc:
            report.errors.append(_describe(exc))
        except Exception as exc:  # storage locked, a bug in conversion...: reported, never raised
            log.exception("meeting-scribe: Google Meet sync failed")
            report.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            if record_status and not dry_run and not report.stopped:
                self._write_status(report)
        return report

    def _write_status(self, report: SyncReport) -> None:
        now = self._clock().isoformat()
        try:
            self.set_status(last_poll_at=now, last_poll_ok="0" if report.errors else "1",
                            last_error=report.errors[0] if report.errors else None,
                            records_given_up=len(report.given_up) or None,
                            records_given_up_last=", ".join(report.given_up[-3:]) or None)
            if report.imported:
                self.set_status(last_import_at=now, last_import_meeting=report.imported[-1])
        except Exception:  # the DB itself is the problem: the log is all we have
            log.exception("meeting-scribe: could not record the Google Meet poll status")

    def _sync(self, report: SyncReport, ended_after: datetime, dry_run: bool,
              should_stop: Callable[[], bool]) -> None:
        svc = self._service()
        repo = svc.repo
        client = self._client()
        if hasattr(client, "should_stop"):
            client.should_stop = should_stop
        try:
            records = client.conference_records(ended_after=convert.rfc3339(ended_after))
        except MeetRetryLater as exc:
            self._pause(repo, exc, dry_run)
            raise
        report.listed = len(records)
        known = repo.known_external_ids(self.space, SOURCE_GOOGLE_MEET)
        memory = self._record_state(repo)
        if not dry_run:
            self._prune(repo, memory)
            repo.kv_set(self._kv + "retry_after_until", None)
        for rec in sorted(records, key=lambda r: str(r.get("endTime") or "")):
            if should_stop():
                report.stopped = True
                return
            name = str(rec.get("name") or "")
            if not name:
                continue
            if name in known:
                report.already += 1
                continue
            seen = memory.get(name) or {}
            if seen.get("no_transcript"):
                report.skipped += 1
                continue
            if int(seen.get("failures") or 0) >= MAX_RECORD_FAILURES:
                report.given_up.append(name)
                continue
            try:
                self._one(client, rec, report, dry_run)
            except (GoogleAuthError, MeetApiError) as exc:
                if _aborts_pass(exc):
                    report.errors.append(_describe(exc))
                    if isinstance(exc, MeetRetryLater):
                        self._pause(repo, exc, dry_run)
                    raise _AbortPass() from None
                self._failed(repo, name, rec, seen, exc, report, dry_run)
            except (ValueError, KeyError, TypeError) as exc:  # malformed record: skip it, keep going
                self._failed(repo, name, rec, seen, exc, report, dry_run)
            else:
                if not dry_run and seen.get("failures"):
                    self._remember(repo, name, None)

    def _pause(self, repo: Any, exc: MeetRetryLater, dry_run: bool) -> None:
        """Honour ``Retry-After`` (capped at 6 h): the poller skips its passes until then."""
        if exc.retry_after and not dry_run:
            until = self._clock() + timedelta(seconds=min(max(float(exc.retry_after), 0.0), 6 * 3600.0))
            repo.kv_set(self._kv + "retry_after_until", convert.rfc3339(until))

    def _failed(self, repo: Any, name: str, rec: dict, seen: dict, exc: BaseException, report: SyncReport,
                dry_run: bool) -> None:
        report.errors.append(f"{name}: {_describe(exc)}")
        log.info("meeting-scribe: Google Meet record %s failed: %s", name, _describe(exc))
        if dry_run or _transient(exc):
            return
        failures = int(seen.get("failures") or 0) + 1
        self._remember(repo, name, {"failures": failures, "end": rec.get("endTime"),
                                    "error": _describe(exc)[:300]})
        if failures >= MAX_RECORD_FAILURES:
            log.warning("meeting-scribe: giving up on Google Meet record %s after %d failures", name, failures)

    def _settle_without_transcript(self, rec: dict, dry_run: bool) -> None:
        """No transcript long after the end: transcription was off. Never ask about it again."""
        end = convert.parse_time(rec.get("endTime"))
        if dry_run or end is None or self._clock() - end < timedelta(seconds=NO_TRANSCRIPT_SETTLE_SECONDS):
            return
        self._remember(self._service().repo, str(rec["name"]), {"no_transcript": True, "end": rec.get("endTime")})

    def _one(self, client: MeetClient, rec: dict, report: SyncReport, dry_run: bool) -> None:
        name = str(rec["name"])
        transcripts = client.transcripts(name)
        if not transcripts:
            report.no_transcript += 1
            self._settle_without_transcript(rec, dry_run)
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
            language=convert.majority_language(entries), source=SOURCE_GOOGLE_MEET, external_id=name,
            space=self.space)
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
    """Daemon thread; one per space. Only the holder of the space's lease syncs.

    ``stop()`` sets a flag checked between records and before every page request, so the pass ends
    at the next request boundary; the THREAD releases the lease when it exits (after its last DB
    access), never the caller while the thread may still be using the repository.
    """

    FIRST_DELAY = 5.0  # first poll shortly after start, not in the middle of startup
    LEFTOVER_AGE_SECONDS = 3600.0  # import folders without a row older than this are crash leftovers

    def __init__(self, *, space: str, importer: Callable[[], Optional[MeetImporter]], repo: Callable[[], Any],
                 settings: Callable[[], Any], connected_at: Callable[[], Optional[float]], owner: str,
                 spawner: Optional[Callable[..., threading.Thread]] = None) -> None:
        self.space = space
        self._lease = lease_name(space)
        self._importer = importer
        self._repo = repo
        self._settings = settings
        self._connected_at = connected_at
        self.owner = owner
        self._spawner = spawner
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._leased_repo: Any = None  # the repository the lease was taken on (released on that one)

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
            self._thread = spawn(self._loop, name=f"meeting-scribe-gmeet-{self.space}", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            self._stop.set()
            thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():  # finishing its in-flight request; it releases the lease itself
                log.info("meeting-scribe: Google Meet poller still finishing a request; it will exit on its own")
                return
        self._release()

    def _release(self) -> None:
        # Never call the ``repo`` factory here: on unload the runtime holds its lock while stopping us.
        repo, self._leased_repo = self._leased_repo, None
        if repo is None:
            return
        try:
            repo.release_lease(self._lease, self.owner)
        except Exception:  # repo already closed on unload
            pass

    def tick(self) -> Optional[SyncReport]:
        """One poll if enabled and we hold the lease; ``None`` when skipped."""
        if not self._settings().google_meet_enabled:
            return None
        repo = self._repo()
        if not repo.acquire_lease(self._lease, self.owner, ttl=self.interval() * 3):
            return None
        self._leased_repo = repo
        importer = self._importer()
        if importer is None:
            return None
        pause = importer.backoff_until()
        if pause is not None and pause > importer.now():
            log.info("meeting-scribe: Google asked space %s to retry after %s; skipping this poll", self.space,
                     pause.isoformat())
            return None
        try:  # crash leftovers of an earlier import (files written, row never committed)
            importer.clean_leftovers(older_than=self.LEFTOVER_AGE_SECONDS)
        except Exception as exc:
            log.info("meeting-scribe: import leftover cleanup skipped: %s", exc)
        start = importer.window_start(connected_at=self._connected_at())
        return importer.sync(ended_after=start, should_stop=self._stop.is_set)

    def _loop(self) -> None:
        try:
            self._poll_forever()
        finally:
            self._release()

    def _poll_forever(self) -> None:
        delay = self.FIRST_DELAY
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
