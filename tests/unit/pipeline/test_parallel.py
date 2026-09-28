"""Parallel workers (DESIGN §22): an atomic pick-and-lease, a cap on concurrent transcriptions and
no double processing across threads or processes sharing the database."""
from __future__ import annotations

import threading
from dataclasses import replace

from meeting_scribe.domain.models import MeetingState, Stage
from meeting_scribe.pipeline.runner import PipelineRunner
from meeting_scribe.pipeline.stages import Stages
from meeting_scribe.storage.repo import Repository

from .conftest import Catalog, FakeAnalyzer, RecordingSink


class GatedTranscriber:
    """Blocks inside ``transcribe`` until released, recording how many run at the same time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.now = 0
        self.peak = 0
        self.entered = threading.Semaphore(0)
        self.release = threading.Event()

    def transcribe(self, meeting, folder, progress=None):
        from meeting_scribe.domain.models import Utterance
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        self.entered.release()
        self.release.wait(10)
        with self.lock:
            self.now -= 1
        return [Utterance(0.0, 1.0, "1", "Ana", "hola")]


def _runner(repo, layout, settings, clock, transcriber, *, workers, transcriptions, sinks=None, owner=None):
    stages = Stages(repo=repo, layout=layout, settings=settings, transcriber=transcriber, analyzer=FakeAnalyzer(),
                    catalogs=lambda: [Catalog()], sinks=lambda: sinks if sinks is not None else [RecordingSink()],
                    archiver=lambda meeting, folder: None)
    return PipelineRunner(repo, stages, clock=clock, workers=workers, max_transcriptions=transcriptions,
                          owner=owner, backoff=(30,))


def _captured(repo, layout, meeting, n):
    ids = []
    for i in range(n):
        m = replace(meeting, id=f"{meeting.id[:6]}{i:02d}", state=MeetingState.CAPTURED, folder="")
        folder = layout.meeting_folder(m)
        folder.mkdir(parents=True)
        m = replace(m, folder=layout.relative(folder))
        repo.save_meeting(m)
        ids.append(m.id)
    return ids


def test_claim_next_job_never_hands_the_same_job_to_two_callers(prepo, clock, meeting, layout):
    ids = _captured(prepo, layout, meeting, 20)
    for mid in ids:
        prepo.enqueue_job(mid, Stage.TRANSCRIBE, now=clock.now())
    other = Repository(layout.db_path())  # a second connection, like a second process
    got: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def worker(repo, owner):
        start.wait()
        while True:
            job = repo.claim_next_job(now=clock.now(), owner=owner)
            if job is None:
                return
            with lock:
                got.append(job.meeting_id)
    threads = [threading.Thread(target=worker, args=(prepo if i % 2 else other, f"w{i}")) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    other.close()
    assert sorted(got) == sorted(ids)  # every job exactly once
    assert all(j.state == "running" for j in prepo.list_jobs(("running",))) and len(prepo.list_jobs(("running",))) == 20


def test_claim_next_job_can_leave_transcriptions_for_later(prepo, clock, meeting, layout):
    a, b = _captured(prepo, layout, meeting, 2)
    prepo.enqueue_job(a, Stage.TRANSCRIBE, now=clock.now())
    prepo.enqueue_job(b, Stage.ANALYZE, now=clock.now())
    job = prepo.claim_next_job(now=clock.now(), owner="w", skip_stages=(Stage.TRANSCRIBE,))
    assert job.meeting_id == b
    assert prepo.claim_next_job(now=clock.now(), owner="w", skip_stages=(Stage.TRANSCRIBE,)) is None
    assert prepo.claim_next_job(now=clock.now(), owner="w").meeting_id == a


def test_workers_process_meetings_in_parallel_with_a_transcription_cap(prepo, layout, settings, clock, meeting):
    gate = GatedTranscriber()
    sink = RecordingSink()
    runner = _runner(prepo, layout, settings, clock, gate, workers=3, transcriptions=2, sinks=[sink])
    ids = _captured(prepo, layout, meeting, 4)
    for mid in ids:
        runner.enqueue(mid)
    runner.start()
    try:
        assert gate.entered.acquire(timeout=5) and gate.entered.acquire(timeout=5)
        assert not gate.entered.acquire(timeout=0.5)  # the third transcription waits for a slot
        assert gate.now == 2
        gate.release.set()
        assert runner.wait_idle(10)
    finally:
        runner.stop()
    assert gate.peak == 2
    assert sorted(sink.calls) == sorted(ids)  # each meeting delivered exactly once
    assert {prepo.get_meeting(mid).state for mid in ids} == {MeetingState.DONE}
    assert not runner.running


def test_non_transcription_work_is_not_blocked_by_a_long_transcription(prepo, layout, settings, clock, meeting):
    gate = GatedTranscriber()
    sink = RecordingSink()
    runner = _runner(prepo, layout, settings, clock, gate, workers=2, transcriptions=1, sinks=[sink])
    slow, other = _captured(prepo, layout, meeting, 2)
    runner.enqueue(slow)
    runner.start()
    try:
        assert gate.entered.acquire(timeout=5)
        from meeting_scribe.domain.models import Utterance
        from meeting_scribe.storage.artifacts import write_transcript
        m = prepo.get_meeting(other)
        write_transcript(layout.meeting_folder(m), [Utterance(0.0, 1.0, "1", "Ana", "hola")])
        prepo.save_meeting(replace(m, state=MeetingState.TRANSCRIBED))
        runner.enqueue(other, Stage.ANALYZE)
        deadline = threading.Event()
        for _ in range(100):
            if other in sink.calls:
                break
            deadline.wait(0.05)
        assert other in sink.calls and slow not in sink.calls  # delivered while the other still transcribes
        gate.release.set()
        assert runner.wait_idle(10)
    finally:
        runner.stop()
    assert sorted(sink.calls) == sorted([slow, other])


def test_two_runners_on_one_database_split_the_queue(prepo, layout, settings, clock, meeting):
    gate = GatedTranscriber()
    gate.release.set()
    sink = RecordingSink()
    second_repo = Repository(layout.db_path())
    first = _runner(prepo, layout, settings, clock, gate, workers=2, transcriptions=2, sinks=[sink], owner="a:1:x")
    second = _runner(second_repo, layout, settings, clock, gate, workers=2, transcriptions=2, sinks=[sink],
                     owner="b:1:x")
    ids = _captured(prepo, layout, meeting, 8)
    for mid in ids:
        first.enqueue(mid)
    first.start()
    second.start()
    try:
        assert first.wait_idle(10) and second.wait_idle(10)
    finally:
        first.stop()
        second.stop()
        second_repo.close()
    assert sorted(sink.calls) == sorted(ids)


def test_reprocess_refuses_a_running_job_and_holds_a_queued_one(prepo, layout, settings, clock, meeting):
    runner = _runner(prepo, layout, settings, clock, GatedTranscriber(), workers=1, transcriptions=1)
    mid, = _captured(prepo, layout, meeting, 1)
    runner.enqueue(mid)
    assert prepo.claim_next_job(now=clock.now(), owner="elsewhere").meeting_id == mid
    try:
        runner.reprocess(mid, Stage.TRANSCRIBE)
    except ValueError as exc:
        assert "being processed" in str(exc)
    else:
        raise AssertionError("reprocess of a running job must be refused")
    prepo.complete_job(prepo.get_job(mid).id)
    prepo.save_meeting(replace(prepo.get_meeting(mid), state=MeetingState.DONE))
    assert runner.reprocess(mid, Stage.ANALYZE) is Stage.ANALYZE
    job = prepo.get_job(mid)
    assert (job.state, job.stage) == ("queued", Stage.ANALYZE)


def test_worker_count_and_cap_are_bounded(prepo, layout, settings, clock):
    runner = _runner(prepo, layout, settings, clock, GatedTranscriber(), workers=lambda: 99,
                     transcriptions=lambda: 0)
    assert runner.workers == PipelineRunner.MAX_WORKERS and runner.max_transcriptions == 1
