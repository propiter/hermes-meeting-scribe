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
    report = runner.recover(live_meeting_ids=set())
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
