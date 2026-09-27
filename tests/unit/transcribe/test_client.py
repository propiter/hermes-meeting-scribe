import sys

import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.transcribe.client import SubprocessTranscriber, TranscriptionError, worker_timeout

FAKE_WORKER = r"""
import json, sys, time
job = json.load(open(sys.argv[1]))
mode = job.get("_fake_mode", "ok")
if mode == "hang":
    time.sleep(60)
if mode == "fail":
    sys.stderr.write("kaboom\n"); sys.exit(3)
for i, tr in enumerate(job["tracks"]):
    seg = {"start": 0.5, "end": 1.5, "text": "hola " + tr["speaker_id"], "avg_logprob": -0.1,
           "no_speech_prob": 0.05, "words": []}
    json.dump({"speaker_id": tr["speaker_id"], "done": True, "language": "es", "segments": [seg]},
              open(tr["out"], "w"))
    json.dump({"done": [t["speaker_id"] for t in job["tracks"][: i + 1]], "fraction": (i + 1) / len(job["tracks"])},
              open(job["progress"], "w"))
"""


@pytest.fixture
def fake_worker(tmp_path):
    p = tmp_path / "fake_worker.py"
    p.write_text(FAKE_WORKER)
    return p


def _transcriber(ff, fake_worker, mode="ok", timeout_floor=30.0):
    settings = settings_from_mapping({"transcribe_language": "es", "transcribe_model": "tiny"})
    return SubprocessTranscriber(lambda: settings, lambda: ff, command=[sys.executable, str(fake_worker)],
                                 extra_job={"_fake_mode": mode}, timeout_floor=timeout_floor, poll_interval=0.05)


def _folder(tmp_path, make_track):
    make_track("10", 1.0)
    make_track("11", 1.0)
    return tmp_path


def test_transcribe_end_to_end_with_fake_worker(ff, fake_worker, make_track, tmp_path, meeting):
    folder = _folder(tmp_path, make_track)
    progress = []
    utts = _transcriber(ff, fake_worker).transcribe(meeting, folder, progress=lambda sid, f: progress.append(f))
    assert [(u.speaker, u.text) for u in utts] == [("Ana", "hola 10"), ("Luis", "hola 11")]
    assert progress and progress[-1] == 1.0
    assert not (folder / ".work").exists()


def test_worker_failure_raises_with_stderr(ff, fake_worker, make_track, tmp_path, meeting):
    folder = _folder(tmp_path, make_track)
    with pytest.raises(TranscriptionError, match="kaboom"):
        _transcriber(ff, fake_worker, "fail").transcribe(meeting, folder)
    assert (folder / ".work").exists()  # decoded wavs kept for resume


def test_worker_timeout_kills(ff, fake_worker, make_track, tmp_path, meeting, monkeypatch):
    folder = _folder(tmp_path, make_track)
    t = _transcriber(ff, fake_worker, "hang", timeout_floor=0.5)
    monkeypatch.setattr("meeting_scribe.transcribe.client.worker_timeout", lambda *a, **k: 0.5)
    with pytest.raises(TranscriptionError, match="timed out"):
        t.transcribe(meeting, folder)


def test_no_tracks_raises(ff, fake_worker, tmp_path, meeting):
    with pytest.raises(TranscriptionError, match="no audio"):
        _transcriber(ff, fake_worker).transcribe(meeting, tmp_path)


def test_tracks_restored_from_archive(ff, fake_worker, make_track, tmp_path, meeting):
    from meeting_scribe.audio.archive import build_archive
    folder = _folder(tmp_path, make_track)
    build_archive(ff, {"10": folder / "tracks/10.ogg", "11": folder / "tracks/11.ogg"}, meeting.speakers,
                  folder, "multitrack", 48)
    utts = _transcriber(ff, fake_worker).transcribe(meeting, folder)
    assert {u.speaker_id for u in utts} == {"10", "11"}
    assert not (folder / "tracks").exists()  # restored copies are temporary


def test_timeout_scales_with_duration_and_model():
    assert worker_timeout(3600, "medium", floor=300) > worker_timeout(3600, "tiny", floor=300)
    assert worker_timeout(10, "tiny", floor=900) == 900


def test_timeout_scales_with_the_sum_of_track_durations(ff, fake_worker, make_track, tmp_path, meeting,
                                                        monkeypatch):
    """Review finding 9: five 60-min speakers are ~5 h of sequential work, not 1 h."""
    for sid in ("10", "11", "12"):
        make_track(sid, 1.0)
    monkeypatch.setattr("meeting_scribe.transcribe.client.probe_duration", lambda ff_, path: 3600.0)
    seen: list[float] = []
    real = worker_timeout
    monkeypatch.setattr("meeting_scribe.transcribe.client.worker_timeout",
                        lambda secs, model, **kw: seen.append(secs) or real(secs, model, **kw))
    _transcriber(ff, fake_worker).transcribe(meeting, tmp_path)
    assert seen == [3 * 3600.0]
