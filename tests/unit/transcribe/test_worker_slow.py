"""Real subprocess contract with faster-whisper ``tiny`` on generated, speech-free audio.

Opt-in (``-m slow``): it may download the tiny model (~75 MB). Skips when the model cannot be
obtained. Proves: the module entry point runs under ``sys.executable``, loads a real model on CPU,
writes per-track results, and that VAD + filters yield no utterances from a pure tone/silence.
"""
from __future__ import annotations

import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.transcribe.client import SubprocessTranscriber

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def tiny_model_available():
    try:
        from faster_whisper.utils import download_model
        download_model("tiny")
    except Exception as exc:  # network/model hub unavailable -> skip, not fail
        pytest.skip(f"faster-whisper tiny unavailable: {exc}")


def test_real_worker_on_speech_free_audio(tiny_model_available, ff, make_track, tmp_path, meeting):
    make_track("10", seconds=3.0, freq=440)
    make_track("11", seconds=3.0, silence=True)
    settings = settings_from_mapping({"transcribe.model": "tiny", "transcribe.language": "en",
                                      "transcribe.cpu_threads": 2})
    seen: list[float] = []
    utts = SubprocessTranscriber(lambda: settings, lambda: ff, poll_interval=0.2).transcribe(
        meeting, tmp_path, progress=lambda _sid, frac: seen.append(frac))
    assert utts == []
    assert seen and seen[-1] == 1.0
    assert not (tmp_path / ".work").exists()
