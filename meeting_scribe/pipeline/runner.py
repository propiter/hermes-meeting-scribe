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
    STAGE_ORDER, Meeting, MeetingState, Stage, rewind_target, running_state, stage_after,
)
from ..domain.ports import Clock
from ..storage.owner import owner_alive, owner_dead, process_owner_id
from ..storage.repo import Repository
from .stages import Stages

log = logging.getLogger(__name__)
Spawner = Callable[..., threading.Thread]
Listener = Callable[[str, str, Any], None]  # meeting_id, event, detail
# DELIVER has no output state of its own: the meeting stays ``delivering`` until archive finishes.
_OUTPUT: dict[Stage, Optional[MeetingState]] = {
    Stage.TRANSCRIBE: MeetingState.TRANSCRIBED, Stage.ANALYZE: MeetingState.ANALYZED,
    Stage.DELIVER: None, Stage.ARCHIVE: MeetingState.DONE}
_IN_PROGRESS = {MeetingState.TRANSCRIBING: Stage.TRANSCRIBE, MeetingState.ANALYZING: Stage.ANALYZE,
                MeetingState.DELIVERING: Stage.DELIVER}


def _thread_spawner(target: Callable[[], None], *, name: str, daemon: bool = True) -> threading.Thread:
    return threading.Thread(target=target, name=name, daemon=daemon)


class PipelineRunner:
    THREAD_NAME = "meeting-scribe-pipeline"
    LEASE_SECONDS = 180.0
    HEARTBEAT_SECONDS = 30.0
    RECLAIM_SECONDS = 60.0  # how often the worker loop re-checks for expired/orphaned leases

    def __init__(self, repo: Repository, stages: Stages, *, clock: Clock, spawner: Spawner = _thread_spawner,
                 max_attempts: int = 3, backoff: Sequence[float] = (60, 300, 900),
                 owner: Optional[str] = None) -> None:
        self.repo = repo
        self.owner = owner or process_owner_id()
        self.stages = stages
        self.clock = clock
        self.spawner = spawner
        self.max_attempts = max_attempts
        self.backoff = tuple(backoff)
        self._listeners: list[Listener] = []
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._busy = threading.Event()
        self._last_reclaim: Optional[float] = None  # set by recover(); gates periodic lease reclaim
        self._thread: Optional[threading.Thread] = None

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

    def reprocess(self, meeting_id: str, stage: Stage) -> None:
        meeting = self._meeting(meeting_id)
        if meeting.state is MeetingState.RECORDING:
            raise ValueError("meeting is still recording")
        job = self.repo.get_job(meeting_id)
        if job is not None and job.state == "running":
            raise ValueError("meeting is being processed right now")
        self.stages.persist(meeting.with_state(rewind_target(stage), rewind=True))
        self.enqueue(meeting_id, stage)

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
                meeting = self.stages.persist(meeting.with_state(rewind_target(_IN_PROGRESS[meeting.state]),
                                                                 rewind=True))
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
                self.stages.persist(meeting.with_state(rewind_target(_IN_PROGRESS[meeting.state]), rewind=True))
            log.warning("meeting-scribe: took back abandoned job for meeting %s", meeting_id)

    def _heartbeat(self, job_id: int, stop: threading.Event) -> None:
        """Keep our lease fresh while a (possibly hours-long) stage runs."""
        while not stop.wait(self.HEARTBEAT_SECONDS):
            try:
                if not self.repo.heartbeat_job(job_id, self.owner, now=self.clock.now().timestamp()):
                    log.warning("meeting-scribe: lost the lease on job %s", job_id)
                    return
            except Exception:  # a locked/closed DB must not kill the worker; the next beat retries
                log.exception("meeting-scribe: job %s heartbeat failed", job_id)

    def _run_job(self, job_id: int, meeting_id: str, first: Stage, attempts: int) -> None:
        meeting = self._meeting(meeting_id)
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
        except Exception as exc:  # every stage failure is recorded, retried or parked
            self._fail(job_id, meeting_id, stage, attempts, exc)
            return
        self.repo.complete_job(job_id)
        self._emit(meeting_id, "done", self.stages.last_results.pop(meeting_id, []))

    def _fail(self, job_id: int, meeting_id: str, stage: Stage, attempts: int, exc: Exception) -> None:
        error = f"{type(exc).__name__}: {exc}"
        log.warning("meeting-scribe %s failed at %s (attempt %d): %s", meeting_id, stage.value, attempts + 1, error)
        meeting = self._meeting(meeting_id)
        if attempts + 1 >= self.max_attempts:
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
            return
        self.recover(live_meeting_ids, owns_capture=owns_capture)
        self._stop.clear()
        self._thread = self.spawner(self._loop, name=self.THREAD_NAME, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
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
