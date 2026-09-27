"""A recording in which nobody's voice was captured is discarded, not failed (no retries, no LLM)."""
import sqlite3
from dataclasses import replace

import pytest

from meeting_scribe.commands import MeetingCommands
from meeting_scribe.domain.errors import NothingToReprocess
from meeting_scribe.domain.models import MeetingState, Stage
from meeting_scribe.storage import repo as repo_mod
from meeting_scribe.storage.artifacts import read_meta
from meeting_scribe.storage.repo import Repository
from meeting_scribe.transcribe.client import SubprocessTranscriber, TranscriptionError

from .conftest import FakeTranscriber
from .test_commands import CALLER, make
from .test_runner import build, captured, drain
from .test_service import svc


class SilentTranscriber(FakeTranscriber):
    """Tracks existed but held only silence/noise the filters dropped: no utterances at all."""

    def transcribe(self, meeting, folder, progress=None):
        self.calls += 1
        return []


class NoTracksTranscriber(FakeTranscriber):
    """The real client's behaviour when the folder has nothing but meta.json."""

    def transcribe(self, meeting, folder, progress=None):
        self.calls += 1
        return SubprocessTranscriber(lambda: None, lambda: None)._tracks(None, folder)


def test_no_audio_tracks_error_is_an_empty_recording(tmp_path):
    from meeting_scribe.domain.errors import EmptyRecording

    with pytest.raises(EmptyRecording) as exc:
        SubprocessTranscriber(lambda: None, lambda: None)._tracks(None, tmp_path)
    assert isinstance(exc.value, TranscriptionError)  # still what older callers catch


@pytest.mark.parametrize("transcriber", [SilentTranscriber, NoTracksTranscriber])
def test_pipeline_discards_without_retry_llm_or_publish(prepo, layout, settings, clock, meeting, transcriber):
    runner, tr, analyzer, sinks = build(prepo, layout, settings, clock, transcriber=transcriber())
    events = []
    runner.subscribe(lambda mid, ev, detail: events.append(ev))
    m, folder = captured(prepo, layout, meeting)
    (folder / "tracks").mkdir()
    (folder / ".work").mkdir()
    runner.enqueue(m.id)
    drain(runner)
    got = prepo.get_meeting(m.id)
    assert got.state is MeetingState.EMPTY and got.state.terminal
    assert tr.calls == 1 and analyzer.calls == 0 and sinks[0].calls == []
    job = prepo.get_job(m.id)
    assert job.state == "done" and job.error is None and job.attempts == 0
    assert "retry" not in events and "failed" not in events and "discarded" in events
    assert read_meta(folder).state is MeetingState.EMPTY
    assert not (folder / "tracks").exists() and not (folder / ".work").exists()  # nothing worth keeping
    assert prepo.list_jobs(("failed",)) == []


def test_analyze_of_an_empty_transcript_is_discarded(prepo, layout, settings, clock, meeting):
    """Defence in depth (imports, legacy rows at ``transcribed``): no utterances, no LLM call."""
    runner, _tr, analyzer, _ = build(prepo, layout, settings, clock)
    m, folder = captured(prepo, layout, meeting)
    prepo.save_meeting(replace(m, state=MeetingState.TRANSCRIBED))
    runner.enqueue(m.id, Stage.ANALYZE)
    drain(runner)
    assert prepo.get_meeting(m.id).state is MeetingState.EMPTY and analyzer.calls == 0


def test_finish_recording_without_audio_skips_the_pipeline(prepo, layout, settings, clock, meeting):
    service, runner, _ = svc(prepo, layout, settings, clock)
    live = service.begin_recording(replace(meeting, state=MeetingState.RECORDING, ended_at=None, folder=""))
    folder = layout.meeting_folder(live)
    (folder / "tracks").mkdir()
    done = service.finish_recording(live.id, heard=False)
    assert done.state is MeetingState.EMPTY and done.ended_at == clock.now()
    assert prepo.get_job(live.id) is None and runner.run_once() is False
    assert read_meta(folder).state is MeetingState.EMPTY and not (folder / "tracks").exists()
    assert folder.is_dir()  # the row keeps a folder with meta.json (traceability; no audio kept)


def test_reprocess_of_a_discarded_meeting_is_refused(prepo, layout, settings, clock, meeting):
    cmds, service, runner = make(prepo, layout, settings, clock)
    m, _ = captured(prepo, layout, meeting)
    prepo.save_meeting(replace(m, state=MeetingState.EMPTY))
    with pytest.raises(NothingToReprocess):
        service.reprocess(m.id, Stage.TRANSCRIBE)
    assert prepo.get_job(m.id) is None
    reply = cmds.handle(f"reprocess {m.id}", CALLER, "meeting")
    assert "no audio" in reply.lower() and m.id in reply


def test_status_and_list_show_it_as_discarded_not_failed(prepo, layout, settings, clock, meeting):
    cmds, service, runner = make(prepo, layout, settings, clock)
    m, _ = captured(prepo, layout, meeting)
    prepo.save_meeting(replace(m, state=MeetingState.EMPTY))
    for sub in ("status", "list"):
        out = cmds.handle(sub, CALLER, "meeting")
        assert "No audio: discarded" in out and "Couldn't finish" not in out


def test_migration_reclassifies_legacy_no_audio_failures(tmp_path, meeting):
    import json
    path = tmp_path / "index.sqlite"
    conn = sqlite3.connect(str(path), isolation_level=None)
    for version, script in enumerate(repo_mod._MIGRATIONS[:6], start=1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={version};\nCOMMIT;")
    rows = {"empty1": "TranscriptionError: no audio tracks in /data/meetings/2026/09/x",
            "real1": "TranscriptionError: worker exited 1: boom"}
    for mid, error in rows.items():
        data = json.dumps(replace(meeting, id=mid, state=MeetingState.FAILED).to_dict())
        conn.execute("INSERT INTO meetings (id, guild_id, channel_id, state, started_at, title, folder, data,"
                     " updated_at) VALUES (?, '1', '2', 'failed', '2026-09-01T00:00:00+00:00', 't', '', ?, 0)",
                     (mid, data))
        conn.execute("INSERT INTO jobs (meeting_id, stage, state, attempts, failed_stage, error, created_at,"
                     " updated_at) VALUES (?, 'transcribe', 'failed', 3, 'transcribe', ?, 0, 0)", (mid, error))
    conn.close()
    for _ in range(2):  # idempotent: a second open changes nothing
        r = Repository(path)
        assert r.get_meeting("empty1").state is MeetingState.EMPTY
        assert r.get_job("empty1").state == "done" and r.get_job("empty1").error is None
        assert r.get_meeting("real1").state is MeetingState.FAILED
        assert r.get_job("real1").state == "failed"
        assert [j.meeting_id for j in r.list_jobs(("failed",))] == ["real1"]
        r.close()
