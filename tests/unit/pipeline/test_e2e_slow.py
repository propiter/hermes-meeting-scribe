"""End-to-end with the production transcriber (real subprocess, faster-whisper ``tiny``, real
ffmpeg) and a fake LLM: capture handoff → done → multitrack archive → reprocess from the .mka.

Opt-in with ``-m slow`` (may download the tiny model)."""
from __future__ import annotations

import os
from dataclasses import replace

import pytest

from meeting_scribe.audio.ffmpeg import stream_count
from meeting_scribe.config import settings_from_mapping
from meeting_scribe.domain.models import MeetingState, Stage

from .conftest import FakeAnalyzer, FakeClock, RecordingSink, SyncSpawner

pytestmark = pytest.mark.slow


def test_real_transcriber_end_to_end(tmp_path, meeting, ff, monkeypatch):
    pytest.importorskip("faster_whisper")
    from meeting_scribe.pipeline.runner import PipelineRunner
    from meeting_scribe.pipeline.service import MeetingService
    from meeting_scribe.pipeline.stages import Stages, make_archiver
    from meeting_scribe.storage.layout import Layout
    from meeting_scribe.storage.repo import Repository
    from meeting_scribe.transcribe.client import SubprocessTranscriber

    s = settings_from_mapping({"transcribe.model": "tiny", "transcribe.language": "en",
                               "transcribe.cpu_threads": min(4, os.cpu_count() or 1), "audio.retention": "multitrack"})
    settings = lambda: s  # noqa: E731
    layout = Layout(lambda: tmp_path / "data")
    repo = Repository(layout.db_path())
    stages = Stages(repo=repo, layout=layout, settings=settings,
                    transcriber=SubprocessTranscriber(settings, lambda: ff, timeout_floor=900),
                    analyzer=FakeAnalyzer(), catalogs=lambda: [], sinks=lambda: [RecordingSink()],
                    archiver=make_archiver(settings, lambda: ff))
    clock = FakeClock()
    runner = PipelineRunner(repo, stages, clock=clock, spawner=SyncSpawner())
    service = MeetingService(repo, layout, runner, settings, clock=clock, item_sinks=lambda: {},
                             catalogs=lambda: [])

    live = service.begin_recording(replace(meeting, state=MeetingState.RECORDING, ended_at=None))
    import subprocess
    for uid, freq in (("10", 440), ("11", 660)):
        subprocess.run([str(ff.ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-t", "3", "-i",
                        f"sine=frequency={freq}:sample_rate=48000", "-ac", "1", "-c:a", "libopus", "-b:a", "48k",
                        str(service.track_path(live, uid))], check=True)
    service.finish_recording(live.id)
    try:
        while runner.run_once():
            pass
    except Exception as exc:  # pragma: no cover - environment-specific (model download)
        pytest.skip(f"whisper unavailable: {exc}")
    job = repo.get_job(live.id)
    if job.state == "queued" and job.error and "download" in job.error.lower():
        pytest.skip(f"model download unavailable: {job.error}")
    done = repo.get_meeting(live.id)
    assert done.state is MeetingState.DONE, job
    folder = layout.meeting_folder(done)
    archive = folder / "recording.mka"
    assert archive.exists() and stream_count(ff, archive) == 3
    assert not (folder / "tracks").exists() and not (folder / ".work").exists()
    assert (folder / "transcript.jsonl").exists() and (folder / "notes.md").exists()

    runner.reprocess(live.id, Stage.TRANSCRIBE)
    while runner.run_once():
        pass
    assert repo.get_meeting(live.id).state is MeetingState.DONE
    assert stream_count(ff, archive) == 3 and not (folder / "tracks").exists()
    repo.close()
