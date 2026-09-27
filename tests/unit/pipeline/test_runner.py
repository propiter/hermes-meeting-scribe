from dataclasses import replace

import pytest

from meeting_scribe.domain.models import MeetingState, Stage
from meeting_scribe.pipeline.runner import PipelineRunner
from meeting_scribe.pipeline.stages import Stages
from meeting_scribe.storage.artifacts import read_notes, read_transcript

from .conftest import Catalog, FakeAnalyzer, FakeTranscriber, RecordingSink, SyncSpawner


def build(prepo, layout, settings, clock, *, transcriber=None, sinks=None, archive=None):
    transcriber = transcriber or FakeTranscriber()
    analyzer = FakeAnalyzer()
    sinks = sinks if sinks is not None else [RecordingSink()]
    stages = Stages(repo=prepo, layout=layout, settings=settings, transcriber=transcriber, analyzer=analyzer,
                    catalogs=lambda: [Catalog()], sinks=lambda: sinks,
                    archiver=archive or (lambda meeting, folder: None))
    runner = PipelineRunner(prepo, stages, clock=clock, spawner=SyncSpawner(), backoff=(30, 120))
    return runner, transcriber, analyzer, sinks


def captured(prepo, layout, meeting):
    m = replace(meeting, state=MeetingState.CAPTURED)
    folder = layout.meeting_folder(m)
    folder.mkdir(parents=True)
    m = replace(m, folder=layout.relative(folder))
    prepo.save_meeting(m)
    return m, folder


def drain(runner, limit=20):
    n = 0
    while runner.run_once() and n < limit:
        n += 1
    return n


def test_desktop_hook_failures_do_not_starve_jobs_or_lease(prepo, layout, settings, clock, meeting, caplog):
    import threading
    runner, *_ = build(prepo, layout, settings, clock)
    m, _ = captured(prepo, layout, meeting)
    runner.enqueue(m.id)
    def broken():
        raise RuntimeError("desktop hook failed")
    runner.control = broken
    assert runner.run_once() is True
    assert prepo.get_job(m.id).state == "done"
    runner.enqueue(m.id, Stage.ARCHIVE)
    job = prepo.get_job(m.id)
    prepo.claim_job(job.id, now=clock.now(), owner=runner.owner)
    clock.advance(10)
    runner.pulse = broken
    class OneBeat:
        calls = 0
        def wait(self, seconds):
            self.calls += 1
            return self.calls > 1
    runner._heartbeat(job.id, OneBeat())
    assert prepo.get_job(m.id).heartbeat == clock.now().timestamp()
    assert "desktop" in caplog.text
    # Real worker loop also reaches run_once when its pulse fails.
    runner._stop.clear()
    reached = []
    def once():
        reached.append(True)
        runner._stop.set()
        return True
    runner.run_once = once
    thread = threading.Thread(target=runner._loop, daemon=True)
    thread.start()
    thread.join(0.5)
    runner._stop.set()
    runner._wake.set()
    thread.join(1)
    assert reached == [True]


def test_full_pipeline_to_done(prepo, layout, settings, clock, meeting):
    runner, tr, an, sinks = build(prepo, layout, settings, clock)
    m, folder = captured(prepo, layout, meeting)
    events = []
    runner.subscribe(lambda mid, event, detail: events.append((mid, event)))
    runner.enqueue(m.id)
    drain(runner)
    done = prepo.get_meeting(m.id)
    assert done.state is MeetingState.DONE and done.project == "Website"
    assert done.folder == m.folder  # folder is stable even though the title changed
    assert done.title == "Informe semanal"
    assert [u.text for u in read_transcript(folder)] == ["Hola equipo", "Yo envío el informe"]
    assert read_notes(folder).action_items[0].id == "a1"
    assert prepo.search("informe")[0]["meeting_id"] == m.id
    assert [a.id for a in prepo.list_action_items(m.id)] == ["a1"]
    assert sinks[0].calls == [m.id] and prepo.get_job(m.id).state == "done"
    assert (m.id, "done") in events
    assert an.seen_candidates[0].key == "hermes:p1"


def test_retry_with_backoff_then_success(prepo, layout, settings, clock, meeting):
    runner, tr, _, _ = build(prepo, layout, settings, clock, transcriber=FakeTranscriber(fail_times=1))
    m, _ = captured(prepo, layout, meeting)
    runner.enqueue(m.id)
    assert runner.run_once() is True
    job = prepo.get_job(m.id)
    assert job.state == "queued" and job.attempts == 1 and job.failed_stage is Stage.TRANSCRIBE
    assert "worker crashed" in job.error
    assert prepo.get_meeting(m.id).state is MeetingState.CAPTURED  # rewound to stage input
    assert runner.run_once() is False  # backoff not elapsed
    clock.advance(31)
    drain(runner)
    assert prepo.get_meeting(m.id).state is MeetingState.DONE and tr.calls == 2


def test_gives_up_after_max_attempts(prepo, layout, settings, clock, meeting):
    runner, tr, _, _ = build(prepo, layout, settings, clock, transcriber=FakeTranscriber(fail_times=99))
    m, _ = captured(prepo, layout, meeting)
    runner.enqueue(m.id)
    for _ in range(5):
        runner.run_once()
        clock.advance(1000)
    assert tr.calls == 3
    job = prepo.get_job(m.id)
    assert job.state == "failed" and job.attempts == 3
    assert prepo.get_meeting(m.id).state is MeetingState.FAILED


def test_sink_errors_retry_deliver_only(prepo, layout, settings, clock, meeting):
    sink = RecordingSink(fail_times=1)
    runner, tr, an, _ = build(prepo, layout, settings, clock, sinks=[sink])
    m, _ = captured(prepo, layout, meeting)
    runner.enqueue(m.id)
    drain(runner)
    job = prepo.get_job(m.id)
    assert job.failed_stage is Stage.DELIVER and "503" in job.error
    clock.advance(31)
    drain(runner)
    assert prepo.get_meeting(m.id).state is MeetingState.DONE
    assert tr.calls == 1 and an.calls == 1 and len(sink.calls) == 2


def test_disabled_sinks_skipped(prepo, layout, settings, clock, meeting):
    sink = RecordingSink(on=False)
    runner, *_ = build(prepo, layout, settings, clock, sinks=[sink])
    m, _ = captured(prepo, layout, meeting)
    runner.enqueue(m.id)
    drain(runner)
    assert sink.calls == [] and prepo.get_meeting(m.id).state is MeetingState.DONE


def test_reprocess_from_analyze_keeps_transcript(prepo, layout, settings, clock, meeting):
    runner, tr, an, _ = build(prepo, layout, settings, clock)
    m, _ = captured(prepo, layout, meeting)
    runner.enqueue(m.id)
    drain(runner)
    runner.reprocess(m.id, Stage.ANALYZE)
    assert prepo.get_meeting(m.id).state is MeetingState.TRANSCRIBED
    drain(runner)
    assert tr.calls == 1 and an.calls == 2 and prepo.get_meeting(m.id).state is MeetingState.DONE


def test_reprocess_rejects_live_recording(prepo, layout, settings, clock, meeting):
    runner, *_ = build(prepo, layout, settings, clock)
    prepo.save_meeting(replace(meeting, state=MeetingState.RECORDING))
    with pytest.raises(ValueError):
        runner.reprocess(meeting.id, Stage.TRANSCRIBE)


def test_recover_resumes_interrupted_work(prepo, layout, settings, clock, meeting):
    runner, tr, _, _ = build(prepo, layout, settings, clock)
    m, _ = captured(prepo, layout, meeting)
    # crashed mid-transcription: job running, meeting transcribing
    prepo.enqueue_job(m.id, Stage.TRANSCRIBE, now=clock.now())
    prepo.mark_job_running(prepo.get_job(m.id).id, now=clock.now())
    prepo.save_meeting(replace(prepo.get_meeting(m.id), state=MeetingState.TRANSCRIBING))
    # orphan recording (gateway died while recording) and an analyzed meeting with no job
    orphan = replace(meeting, id="orphan01", state=MeetingState.RECORDING)
    prepo.save_meeting(orphan)
    analyzed = replace(meeting, id="analyz01", state=MeetingState.ANALYZED)
    prepo.save_meeting(analyzed)
    clock.advance(runner.LEASE_SECONDS + 1)  # the crashed worker's lease expired
    report = runner.recover(live_meeting_ids=set(), owns_capture=True)
    assert report == {"requeued": 1, "orphans": 1, "resumed": 2}
    o = prepo.get_meeting("orphan01")
    assert o.state is MeetingState.CAPTURED and o.partial is True
    assert prepo.get_job("analyz01").stage is Stage.DELIVER


def test_recover_leaves_live_recordings(prepo, layout, settings, clock, meeting):
    runner, *_ = build(prepo, layout, settings, clock)
    prepo.save_meeting(replace(meeting, state=MeetingState.RECORDING))
    runner.recover(live_meeting_ids={meeting.id})
    assert prepo.get_meeting(meeting.id).state is MeetingState.RECORDING


def test_background_thread_processes_queue(prepo, layout, settings, clock, meeting):
    runner, *_ = build(prepo, layout, settings, clock)
    m, _ = captured(prepo, layout, meeting)
    runner.start(live_meeting_ids=set())
    try:
        runner.enqueue(m.id)
        assert runner.wait_idle(timeout=10)
    finally:
        runner.stop(timeout=5)
    assert prepo.get_meeting(m.id).state is MeetingState.DONE
    assert runner.spawner.names == ["meeting-scribe-pipeline"]


def test_archive_runs_last(prepo, layout, settings, clock, meeting):
    order = []
    runner, _, _, sinks = build(prepo, layout, settings, clock,
                                archive=lambda meeting, folder: order.append("archive"))
    sinks[0].deliver = lambda *a: order.append("deliver") or __import__(
        "meeting_scribe.domain.models", fromlist=["SinkResult"]).SinkResult("rec", True)
    m, _ = captured(prepo, layout, meeting)
    runner.enqueue(m.id)
    drain(runner)
    assert order == ["deliver", "archive"]


# -- review finding 2: a second process must not hijack the gateway's work ----------------------
def test_recover_keeps_a_live_lease_and_its_meeting(prepo, layout, settings, clock, meeting):
    gateway, *_ = build(prepo, layout, settings, clock)
    m, _ = captured(prepo, layout, meeting)
    prepo.enqueue_job(m.id, Stage.TRANSCRIBE, now=clock.now())
    assert prepo.claim_job(prepo.get_job(m.id).id, now=clock.now(), owner=gateway.owner)
    prepo.save_meeting(replace(prepo.get_meeting(m.id), state=MeetingState.TRANSCRIBING))
    cli, *_ = build(prepo, layout, settings, clock)
    cli.owner = "otherhost:1:cli"
    clock.advance(30)
    report = cli.recover(live_meeting_ids=())
    assert report["requeued"] == 0
    job = prepo.get_job(m.id)
    assert job.state == "running" and job.owner == gateway.owner
    assert prepo.get_meeting(m.id).state is MeetingState.TRANSCRIBING  # not rewound under the worker


def _dead_owner():
    import socket
    return f"{socket.gethostname()}:999999999:gone"


def test_recover_takes_back_a_dead_workers_job_before_its_lease_expires(prepo, layout, settings, clock, meeting):
    """Live finding: a gateway restart left the job 'running' under the dead pid for 3 minutes, and
    the new gateway never looked again — the meeting sat in 'analyzing' forever."""
    m, _ = captured(prepo, layout, meeting)
    prepo.enqueue_job(m.id, Stage.ANALYZE, now=clock.now())
    assert prepo.claim_job(prepo.get_job(m.id).id, now=clock.now(), owner=_dead_owner())
    prepo.save_meeting(replace(prepo.get_meeting(m.id), state=MeetingState.ANALYZING))
    fresh, *_ = build(prepo, layout, settings, clock)
    clock.advance(10)  # lease (180 s) still valid, but its owner no longer exists
    report = fresh.recover(live_meeting_ids=())
    assert report["requeued"] == 1
    assert prepo.get_job(m.id).state == "queued"
    assert prepo.get_meeting(m.id).state is not MeetingState.ANALYZING


def test_worker_loop_reclaims_expired_leases_while_running(prepo, layout, settings, clock, meeting):
    """The worker must keep reclaiming stale jobs, not only once at start-up."""
    runner, *_ = build(prepo, layout, settings, clock)
    runner.recover(live_meeting_ids=())
    m, _ = captured(prepo, layout, meeting)
    prepo.enqueue_job(m.id, Stage.TRANSCRIBE, now=clock.now())
    # Owner on another host (liveness unknown): only the lease expiry can free it.
    assert prepo.claim_job(prepo.get_job(m.id).id, now=clock.now(), owner="otherhost:1:x")
    prepo.save_meeting(replace(prepo.get_meeting(m.id), state=MeetingState.TRANSCRIBING))
    assert runner.run_once() is False  # lease still valid: nothing to do
    assert prepo.get_job(m.id).owner == "otherhost:1:x"
    clock.advance(runner.LEASE_SECONDS + runner.RECLAIM_SECONDS + 1)
    drain(runner)
    assert prepo.get_meeting(m.id).state is MeetingState.DONE


def test_recover_without_capture_ownership_never_closes_recordings(prepo, layout, settings, clock, meeting):
    runner, *_ = build(prepo, layout, settings, clock)
    prepo.save_meeting(replace(meeting, state=MeetingState.RECORDING))
    report = runner.recover(live_meeting_ids=())  # e.g. a CLI process
    assert report["orphans"] == 0 and prepo.get_meeting(meeting.id).state is MeetingState.RECORDING


def test_recover_spares_recordings_owned_by_another_live_process(prepo, layout, settings, clock, meeting):
    import os
    import socket

    runner, *_ = build(prepo, layout, settings, clock)
    prepo.save_meeting(replace(meeting, state=MeetingState.RECORDING))
    prepo.set_capture_owner(meeting.id, f"{socket.gethostname()}:{os.getppid()}:parent")  # alive
    assert runner.recover(live_meeting_ids=(), owns_capture=True)["orphans"] == 0
    prepo.set_capture_owner(meeting.id, f"{socket.gethostname()}:999999999:gone")  # dead pid
    assert runner.recover(live_meeting_ids=(), owns_capture=True)["orphans"] == 1


def test_running_job_keeps_its_lease_fresh(prepo, layout, settings, clock, meeting, monkeypatch):
    import threading as th

    gate = th.Event()

    class Slow:
        calls = 0

        def transcribe(self, meeting, folder, progress=None):
            gate.wait(5)
            return []

    runner, *_ = build(prepo, layout, settings, clock, transcriber=Slow())
    runner.HEARTBEAT_SECONDS = 0.02
    m, _ = captured(prepo, layout, meeting)
    runner.enqueue(m.id)
    worker = th.Thread(target=runner.run_once)
    worker.start()
    try:
        first = None
        for _ in range(100):
            job = prepo.get_job(m.id)
            if job.state == "running" and job.heartbeat is not None:
                first = first or job.heartbeat
                clock.advance(10)
                if job.heartbeat > first:
                    break
            gate.wait(0.02)
        assert job.owner == runner.owner and job.heartbeat > first
    finally:
        gate.set()
        worker.join(5)


def test_ensure_pipeline_is_a_noop_outside_the_gateway(tmp_path):
    from meeting_scribe.runtime import Runtime
    from tests.unit.test_runtime import host

    h, _ = host(tmp_path)
    h.is_gateway = lambda: False
    rt = Runtime(h)
    rt.ensure_pipeline()
    assert not rt.pipeline_running()
    rt.close()


def test_reanalysis_that_rephrases_a_title_keeps_the_item_id(prepo, layout, settings, clock, meeting):
    """E2E finding: a reprocess from=analyze changed a title slightly and Kanban got a duplicate."""
    from meeting_scribe.domain.ids import action_item_id
    from meeting_scribe.domain.models import ActionItem, Notes

    class Rephrasing(FakeAnalyzer):
        def analyze(self, meeting, utterances, candidates):
            self.calls += 1
            title = "Enviar el informe semanal" if self.calls == 1 else "Enviar informe semanal"
            return Notes(meeting_title="Informe", tldr="t", summary="s", language="es", action_items=(
                ActionItem(id=action_item_id(title, "11"), title=title, owner_speaker_id="11", quote="yo envío"),))

    runner, _tr, _an, _ = build(prepo, layout, settings, clock)
    runner.stages.analyzer = Rephrasing()
    m, _ = captured(prepo, layout, meeting)
    runner.enqueue(m.id)
    drain(runner)
    first = [a.id for a in prepo.list_action_items(m.id)]
    runner.reprocess(m.id, Stage.ANALYZE)
    drain(runner)
    assert [a.id for a in prepo.list_action_items(m.id)] == first
    folder = layout.meeting_folder(prepo.get_meeting(m.id))
    notes = read_notes(folder)
    assert [a.id for a in notes.action_items] == first and notes.action_items[0].title == "Enviar informe semanal"


def test_capture_ownership_arriving_after_start_runs_the_orphan_recovery(prepo, layout, settings, clock, meeting):
    """The gateway starts the worker at register (no Discord yet); Discord connecting later must
    still close orphan recordings of a dead process (finding 15)."""
    runner, *_ = build(prepo, layout, settings, clock)
    runner.spawner = lambda target, *, name, daemon=True: _Alive()
    prepo.save_meeting(replace(meeting, id="orphan01", state=MeetingState.RECORDING))
    runner.start(())
    assert prepo.get_meeting("orphan01").state is MeetingState.RECORDING
    runner.start((), owns_capture=True)
    assert prepo.get_meeting("orphan01").state is not MeetingState.RECORDING
    runner.start((), owns_capture=True)  # once only


class _Alive:
    def start(self):
        pass

    def is_alive(self):
        return True

    def join(self, timeout=None):
        pass
