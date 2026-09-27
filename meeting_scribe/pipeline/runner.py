"""Single-worker, resumable job runner (DESIGN §9).

One thread (via ``spawn_context_thread`` in production, so profile contextvars follow it) pulls
the next ready job from SQLite and runs its remaining stages in order. Failures rewind the meeting
to the failed stage's input state and requeue with backoff; after ``max_attempts`` the meeting is
``failed`` and waits for a manual ``reprocess``. Transcription is CPU-heavy, so one meeting at a
time is intentional.

Multi-process safety (DESIGN §15, review finding 2): a claimed job carries a LEASE (owner id +
heartbeat refreshed by a helper thread while it runs). ``recover`` only requeues jobs whose lease
expired and never rewinds a meeting whose job is still leased. Orphan recordings are closed only
when ``owns_capture`` (the Discord-connected gateway) and the row's ``capture_owner`` is not a
live process.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from datetime import timedelta
from typing import Any, Callable, Iterable, Optional, Sequence

from ..domain.models import (
    KV_MOVE_FROM_DM, SOURCE_DISCORD, STAGE_ORDER, Meeting, MeetingState, Stage, rewind_target, running_state, stage_after,
)
from ..domain.ports import Clock
from ..storage.owner import owner_alive, owner_dead, process_owner_id
from ..storage.repo import Repository
from .stages import SINKS_DONE_KV, StageDeferred, Stages

log = logging.getLogger(__name__)
Spawner = Callable[..., threading.Thread]
Listener = Callable[[str, str, Any], None]  # meeting_id, event, detail
# DELIVER has no output state of its own: the meeting stays ``delivering`` until archive finishes.
_OUTPUT: dict[Stage, Optional[MeetingState]] = {
    Stage.TRANSCRIBE: MeetingState.TRANSCRIBED, Stage.ANALYZE: MeetingState.ANALYZED,
    Stage.DELIVER: None, Stage.ARCHIVE: MeetingState.DONE}
_IN_PROGRESS = {MeetingState.TRANSCRIBING: Stage.TRANSCRIBE, MeetingState.ANALYZING: Stage.ANALYZE,
                MeetingState.DELIVERING: Stage.DELIVER}


def effective_stage(meeting: Meeting, stage: Stage) -> Stage:
    """Imported meetings (Google Meet) have no audio: re-transcribing them means re-analyzing."""
    if stage is Stage.TRANSCRIBE and meeting.source != SOURCE_DISCORD:
        return Stage.ANALYZE
    return stage


def _in_progress_rewind(meeting: Meeting) -> Meeting:
    """Where an interrupted stage restarts from.

    An imported meeting caught in ``transcribing`` (a reprocess queued by an older version) has its
    transcript already: it goes FORWARD to ``transcribed`` (then ANALYZE), never to ``captured``.
    """
    stage = _IN_PROGRESS[meeting.state]
    if effective_stage(meeting, stage) is not stage:
        return replace(meeting, state=MeetingState.TRANSCRIBED)
    return meeting.with_state(rewind_target(stage), rewind=True)


_DEFER_KV = "pipeline.deferred_since."  # meeting id -> first deferral (epoch seconds)
WAITING_KV = "pipeline.waiting_destination."  # meeting id -> why (shown by status/doctor), DESIGN §19


def _thread_spawner(target: Callable[[], None], *, name: str, daemon: bool = True) -> threading.Thread:
    return threading.Thread(target=target, name=name, daemon=daemon)


class PipelineRunner:
    THREAD_NAME = "meeting-scribe-pipeline"
    LEASE_SECONDS = 180.0
    HEARTBEAT_SECONDS = 30.0
    RECLAIM_SECONDS = 60.0  # how often the worker loop re-checks for expired/orphaned leases
    STRAGGLER_AGE_SECONDS = 600.0
    DEFER_SECONDS = 60.0  # retry delay for a stage that only waits for a target (not an attempt)
    DEFER_MAX_SECONDS = 6 * 3600.0  # after this long waiting for Discord, deferrals count as normal failures
    WAIT_SECONDS = 120.0  # re-check interval while a delivery waits for a destination (no deadline)

    def __init__(self, repo: Repository, stages: Stages, *, clock: Clock, spawner: Spawner = _thread_spawner,
                 max_attempts: "int | Callable[[], int]" = 3, backoff: Sequence[float] = (60, 300, 900),
                 owner: Optional[str] = None) -> None:
        self.repo = repo
        self.owner = owner or process_owner_id()
        self.stages = stages
        self.clock = clock
        self.spawner = spawner
        self._max_attempts = max_attempts
        self.backoff = tuple(backoff)
        # Desktop hooks (set by the runtime in the gateway): ``control`` runs one queued operator
        # command (True when it did work); ``pulse`` records worker liveness for the Desktop page.
        self.control: Optional[Callable[[], bool]] = None
        self.pulse: Optional[Callable[[], None]] = None
        self._listeners: list[Listener] = []
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._busy = threading.Event()
        self._owns_capture = False  # recover() already ran with capture ownership
        self._last_reclaim: Optional[float] = None  # set by recover(); gates periodic lease reclaim
        self._thread: Optional[threading.Thread] = None

    @property
    def max_attempts(self) -> int:
        """Read per failure so ``pipeline_max_attempts`` edits apply without a restart."""
        value = self._max_attempts() if callable(self._max_attempts) else self._max_attempts
        return max(1, int(value))

    @max_attempts.setter
    def max_attempts(self, value: "int | Callable[[], int]") -> None:
        self._max_attempts = value

    # -- observers ----------------------------------------------------------------------------
    def subscribe(self, listener: Listener) -> None:
        """Phase B uses this to post progress/failures to Discord."""
        self._listeners.append(listener)

    def _emit(self, meeting_id: str, event: str, detail: Any = None) -> None:
        for listener in list(self._listeners):
            try:
                listener(meeting_id, event, detail)
            except Exception:  # a broken notifier must never break processing
                log.exception("meeting-scribe listener failed for %s/%s", meeting_id, event)

    # -- queue API ----------------------------------------------------------------------------
    def enqueue(self, meeting_id: str, stage: Optional[Stage] = None) -> Stage:
        meeting = self._meeting(meeting_id)
        stage = stage or stage_after(meeting.state)
        if stage is None:
            raise ValueError(f"meeting {meeting_id} in state {meeting.state.value} has nothing to process")
        self.repo.enqueue_job(meeting_id, stage, now=self.clock.now(), reset_attempts=True)
        self._wake.set()
        return stage

    def reprocess(self, meeting_id: str, stage: Stage) -> Stage:
        """Rewind and queue; returns the stage actually used (see :func:`effective_stage`)."""
        meeting = self._meeting(meeting_id)
        if meeting.state is MeetingState.RECORDING:
            raise ValueError("meeting is still recording")
        job = self.repo.get_job(meeting_id)
        if job is not None and job.state == "running":
            raise ValueError("meeting is being processed right now")
        stage = effective_stage(meeting, stage)
        # Only an explicit re-delivery may move notes an older version posted in a DM (DESIGN §19).
        self.repo.kv_set(KV_MOVE_FROM_DM + meeting_id, "1" if stage is Stage.DELIVER else None)
        self.repo.kv_set(SINKS_DONE_KV + meeting_id, None)  # an explicit re-delivery runs every sink
        self.stages.persist(meeting.with_state(rewind_target(stage), rewind=True))
        self.enqueue(meeting_id, stage)
        return stage

    def recover(self, live_meeting_ids: Iterable[str], *, owns_capture: bool = False) -> dict[str, int]:
        """Resume after a restart: requeue STALE jobs, close orphan recordings, enqueue stragglers."""
        live = set(live_meeting_ids)
        stale_before = self.clock.now().timestamp() - self.LEASE_SECONDS
        report = {"requeued": len(self.repo.requeue_stale(stale_before=stale_before, owner_dead=owner_dead)),
                  "orphans": 0, "resumed": 0}
        self._last_reclaim = self.clock.now().timestamp()
        jobs = self.repo.list_jobs(("queued", "running"))
        active = {j.meeting_id for j in jobs}
        leased = {j.meeting_id for j in jobs if j.state == "running"}
        pending = [s for s in MeetingState if not s.terminal]
        for meeting in self.repo.list_meetings(limit=10_000, states=pending):
            if meeting.id in leased:
                continue  # another worker is processing it right now (live lease)
            if meeting.state is MeetingState.RECORDING:
                if meeting.id in live or not owns_capture or owner_alive(self.repo.capture_owner(meeting.id)):
                    continue  # still being captured (here or by another live process), or not ours to judge
                ended = meeting.ended_at or self.clock.now()
                meeting = self.stages.persist(replace(meeting, state=MeetingState.CAPTURED, partial=True,
                                                      ended_at=ended))
                report["orphans"] += 1
            elif meeting.state in _IN_PROGRESS:
                meeting = self.stages.persist(_in_progress_rewind(meeting))
            if meeting.id not in active and stage_after(meeting.state) is not None:
                self.repo.enqueue_job(meeting.id, stage_after(meeting.state), now=self.clock.now())
                report["resumed"] += 1
        self._wake.set()
        return report

    # -- execution ----------------------------------------------------------------------------
    def _meeting(self, meeting_id: str) -> Meeting:
        meeting = self.repo.get_meeting(meeting_id)
        if meeting is None:
            raise KeyError(f"unknown meeting {meeting_id}")
        return meeting

    def run_once(self) -> bool:
        """Run one ready job to completion or failure; False when nothing is ready."""
        if self.control is not None and self.control():
            return True
        self._maybe_reclaim()
        job = self.repo.next_job(now=self.clock.now())
        if job is None:
            return False
        if not self.repo.claim_job(job.id, now=self.clock.now(), owner=self.owner):
            return True  # another process took it; look again
        self._busy.set()
        beat_stop = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(job.id, beat_stop), name=f"{self.THREAD_NAME}-lease",
                                daemon=True)
        beat.start()
        try:
            self._run_job(job.id, job.meeting_id, job.stage, job.attempts)
        finally:
            beat_stop.set()
            beat.join(timeout=5)
            self._busy.clear()
        return True

    def _maybe_reclaim(self) -> None:
        """Periodically take back jobs whose worker died after we started (lease expired or dead pid).

        ``recover`` runs once at start-up; without this, a job abandoned later — or one whose lease
        was still fresh at our start-up — would stay 'running' forever.
        """
        last = self._last_reclaim
        if last is None:
            return  # recover() has not run: not our call to judge leases (CLI / tests)
        now = self.clock.now().timestamp()
        if now - last < self.RECLAIM_SECONDS:
            return
        self._last_reclaim = now
        for meeting_id in self.repo.requeue_stale(stale_before=now - self.LEASE_SECONDS, owner_dead=owner_dead):
            meeting = self.repo.get_meeting(meeting_id)
            if meeting is not None and meeting.state in _IN_PROGRESS:
                self.stages.persist(_in_progress_rewind(meeting))
            log.warning("meeting-scribe: took back abandoned job for meeting %s", meeting_id)
        self._requeue_stragglers(time.time())  # meetings.updated_at is wall-clock time

    def _requeue_stragglers(self, now: float) -> None:
        """Rows waiting for a stage that have NO job at all (a crash between an import's commit and
        its enqueue, or a job row lost otherwise): queue the next stage. Light: one indexed query for
        the handful of non-terminal rows; rows touched recently are left alone (may be mid-write)."""
        waiting = (MeetingState.CAPTURED, MeetingState.TRANSCRIBED, MeetingState.ANALYZED)
        try:
            active = {j.meeting_id for j in self.repo.list_jobs(("queued", "running"))}
            for meeting_id in self.repo.meetings_without_job(waiting, updated_before=now - self.STRAGGLER_AGE_SECONDS):
                if meeting_id in active:
                    continue
                meeting = self.repo.get_meeting(meeting_id)
                stage = stage_after(meeting.state) if meeting is not None else None
                if stage is None:
                    continue
                self.repo.enqueue_job(meeting_id, effective_stage(meeting, stage), now=self.clock.now())
                log.warning("meeting-scribe: re-queued meeting %s (%s) that had no job", meeting_id,
                            meeting.state.value)
        except Exception:  # never let housekeeping break the worker
            log.exception("meeting-scribe: straggler sweep failed")

    def _heartbeat(self, job_id: int, stop: threading.Event) -> None:
        """Keep our lease fresh while a (possibly hours-long) stage runs."""
        while not stop.wait(self.HEARTBEAT_SECONDS):
            try:
                if self.pulse is not None:
                    self.pulse()
                if not self.repo.heartbeat_job(job_id, self.owner, now=self.clock.now().timestamp()):
                    log.warning("meeting-scribe: lost the lease on job %s", job_id)
                    return
            except Exception:  # a locked/closed DB must not kill the worker; the next beat retries
                log.exception("meeting-scribe: job %s heartbeat failed", job_id)

    def _run_job(self, job_id: int, meeting_id: str, first: Stage, attempts: int) -> None:
        meeting = self._meeting(meeting_id)
        if effective_stage(meeting, first) is not first:  # imported meeting queued at TRANSCRIBE (older version)
            first = effective_stage(meeting, first)
            if meeting.state in (MeetingState.CAPTURED, MeetingState.TRANSCRIBING, MeetingState.FAILED):
                meeting = self.stages.persist(replace(meeting, state=MeetingState.TRANSCRIBED))
        stage = first
        try:
            for stage in STAGE_ORDER[STAGE_ORDER.index(first):]:
                self.repo.advance_job(job_id, stage)
                self._emit(meeting_id, "stage", stage.value)
                if stage is Stage.ARCHIVE and meeting.state is MeetingState.ANALYZED:
                    meeting = meeting.with_state(MeetingState.DELIVERING)  # archive retry after deliver
                running = running_state(stage)
                if running is not None:
                    meeting = self.stages.persist(meeting.with_state(running))
                meeting = self.stages.run(stage, meeting)
                output = _OUTPUT[stage]
                meeting = self.stages.persist(meeting.with_state(output) if output else meeting)
        except StageDeferred as exc:  # e.g. Discord still connecting after a gateway start: no attempt used
            if self._defer(job_id, meeting_id, stage, exc):
                return
            self._fail(job_id, meeting_id, stage, attempts, exc)  # waited too long: a normal failure
            return
        except Exception as exc:  # every stage failure is recorded, retried or parked
            self._fail(job_id, meeting_id, stage, attempts, exc)
            return
        self.repo.kv_set(_DEFER_KV + meeting_id, None)
        self.repo.kv_set(WAITING_KV + meeting_id, None)
        self.repo.kv_set(SINKS_DONE_KV + meeting_id, None)
        self.repo.complete_job(job_id)
        self._emit(meeting_id, "done", self.stages.last_results.pop(meeting_id, []))

    def _defer(self, job_id: int, meeting_id: str, stage: Stage, exc: Exception) -> bool:
        """Re-queue without using an attempt; ``False`` once the wait exceeded ``DEFER_MAX_SECONDS``.

        Waiting for a DESTINATION (no channel configured or found) has no deadline: dropping the
        meeting after hours would lose notes the user only has to point somewhere (DESIGN §19)."""
        if getattr(exc, "waiting", False):
            self.repo.kv_set(WAITING_KV + meeting_id, str(exc)[:2000])
            self.repo.defer_job(job_id, stage, str(exc),
                                retry_at=self.clock.now() + timedelta(seconds=self.WAIT_SECONDS))
            self.stages.persist(self._meeting(meeting_id).with_state(rewind_target(stage), rewind=True))
            self._emit(meeting_id, "waiting", {"stage": stage.value, "reason": str(exc)})
            return True
        self.repo.kv_set(WAITING_KV + meeting_id, None)
        now = self.clock.now().timestamp()
        raw = self.repo.kv_get(_DEFER_KV + meeting_id)
        try:
            since = float(raw) if raw else now
        except ValueError:
            since = now
        if now - since > self.DEFER_MAX_SECONDS:
            return False  # the marker stays: further deferrals keep counting as attempts until the job ends
        if raw is None:
            self.repo.kv_set(_DEFER_KV + meeting_id, str(now))
        self.repo.defer_job(job_id, stage, str(exc), retry_at=self.clock.now() + timedelta(seconds=self.DEFER_SECONDS))
        self.stages.persist(self._meeting(meeting_id).with_state(rewind_target(stage), rewind=True))
        self._emit(meeting_id, "deferred", {"stage": stage.value, "reason": str(exc)})
        return True

    def _fail(self, job_id: int, meeting_id: str, stage: Stage, attempts: int, exc: Exception) -> None:
        error = f"{type(exc).__name__}: {exc}"
        log.warning("meeting-scribe %s failed at %s (attempt %d): %s", meeting_id, stage.value, attempts + 1, error)
        meeting = self._meeting(meeting_id)
        self.repo.kv_set(WAITING_KV + meeting_id, None)
        if attempts + 1 >= self.max_attempts:
            self.repo.kv_set(_DEFER_KV + meeting_id, None)
            self.repo.kv_set(KV_MOVE_FROM_DM + meeting_id, None)  # a later retry must be asked for again
            self.repo.kv_set(SINKS_DONE_KV + meeting_id, None)
            self.repo.fail_job(job_id, stage, error, retry_at=None)
            self.stages.persist(meeting.with_state(MeetingState.FAILED))
            self._emit(meeting_id, "failed", {"stage": stage.value, "error": error})
            return
        delay = self.backoff[min(attempts, len(self.backoff) - 1)]
        self.repo.fail_job(job_id, stage, error, retry_at=self.clock.now() + timedelta(seconds=delay))
        self.stages.persist(meeting.with_state(rewind_target(stage), rewind=True))
        self._emit(meeting_id, "retry", {"stage": stage.value, "error": error, "delay": delay})

    # -- background thread --------------------------------------------------------------------
    def start(self, live_meeting_ids: Iterable[str] = (), *, owns_capture: bool = False) -> None:
        if self._thread is not None and self._thread.is_alive():
            if owns_capture and not self._owns_capture:
                # Started earlier without capture (gateway register); Discord just connected: only now
                # may orphan recordings of a dead process be closed. recover skips leased meetings.
                self._owns_capture = True
                self.recover(live_meeting_ids, owns_capture=True)
            return
        self._owns_capture = owns_capture
        self.recover(live_meeting_ids, owns_capture=owns_capture)
        self._stop.clear()
        self._thread = self.spawner(self._loop, name=self.THREAD_NAME, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.pulse is not None:
                    self.pulse()
                worked = self.run_once()
            except Exception:  # keep the worker alive; the job row keeps the error context
                log.exception("meeting-scribe pipeline iteration crashed")
                worked = False
            if not worked:
                self._wake.wait(timeout=5.0)
                self._wake.clear()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def wait_idle(self, timeout: float) -> bool:
        """Test/CLI helper: True once no job is ready or running."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._busy.is_set() and self.repo.next_job(now=self.clock.now()) is None and not any(
                    j.state == "running" for j in self.repo.list_jobs(("running",))):
                return True
            time.sleep(0.05)
        return False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
