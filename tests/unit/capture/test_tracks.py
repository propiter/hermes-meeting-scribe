"""Per-speaker live Opus writer with timeline alignment (DESIGN §4)."""
from __future__ import annotations

import time

import pytest

from meeting_scribe.audio.ffmpeg import probe_duration
from meeting_scribe.capture.tracks import BYTES_PER_SAMPLE, SAMPLE_RATE, Aligner, TrackWriter

FRAME_SAMPLES = 960
FRAME = b"\x10\x00\x10\x00" * FRAME_SAMPLES  # 20 ms stereo s16le


def frames(start: float, seconds: float) -> list[tuple[float, bytes]]:
    n = int(round(seconds / 0.02))
    return [(start + i * 0.02, FRAME) for i in range(n)]


def silence_samples(chunks: list[bytes]) -> int:
    return sum(len(c) for c in chunks if not c.strip(b"\x00")) // BYTES_PER_SAMPLE


def test_aligner_pads_leading_gap_and_gaps_over_100ms():
    al = Aligner(t0=1000.0)
    out = al.feed(frames(1000.5, 0.2))
    assert silence_samples(out) == int(0.5 * SAMPLE_RATE)
    out = al.feed(frames(1000.7 + 0.3, 0.02))  # 300 ms gap
    assert silence_samples(out) == int(0.3 * SAMPLE_RATE)
    assert al.position_seconds == pytest.approx(1.02, abs=1e-6)


def test_aligner_ignores_small_gaps_and_never_goes_backwards():
    al = Aligner(t0=0.0)
    al.feed(frames(0.0, 0.1))
    out = al.feed([(0.15, FRAME)])  # 50 ms late: jitter, appended without padding
    assert silence_samples(out) == 0
    out = al.feed([(0.05, FRAME)])  # arrives "in the past": appended, never rewinds
    assert silence_samples(out) == 0
    assert al.position_seconds == pytest.approx(0.14, abs=1e-6)


def test_aligner_splits_huge_gaps_into_bounded_chunks():
    al = Aligner(t0=0.0)
    out = al.feed([(600.0, FRAME)])  # 10 min of silence must not be one 115 MB bytes object
    assert max(len(c) for c in out) <= Aligner.MAX_CHUNK
    assert silence_samples(out) == 600 * SAMPLE_RATE


def test_writer_timeline_matches_ffprobe_duration(ff, tmp_path):
    out = tmp_path / "tracks" / "42.ogg"
    w = TrackWriter(ff, out, t0=500.0, bitrate_kbps=48)
    w.write(frames(500.4, 1.0))       # leading 0.4 s gap
    w.write(frames(502.4 + 0.6, 1.0))  # 1.6 s gap after 1.4 s → ends at 4.0 s
    w.close()
    assert w.error is None
    assert probe_duration(ff, out) == pytest.approx(4.0, abs=0.05)


def test_write_is_non_blocking(ff, tmp_path):
    w = TrackWriter(ff, tmp_path / "t.ogg", t0=0.0, bitrate_kbps=48)
    started = time.monotonic()
    w.write(frames(0.0, 30.0))
    assert time.monotonic() - started < 0.2
    w.close()
    assert probe_duration(ff, tmp_path / "t.ogg") == pytest.approx(30.0, abs=0.05)


def test_close_is_idempotent_and_writes_after_close_are_ignored(ff, tmp_path):
    w = TrackWriter(ff, tmp_path / "t.ogg", t0=0.0, bitrate_kbps=48)
    w.write(frames(0.0, 0.5))
    w.close()
    w.close()
    w.write(frames(1.0, 0.5))
    assert probe_duration(ff, tmp_path / "t.ogg") == pytest.approx(0.5, abs=0.05)


def test_ffmpeg_crash_is_reported_not_raised(ff, tmp_path):
    w = TrackWriter(ff, tmp_path / "t.ogg", t0=0.0, bitrate_kbps=48)
    w._proc.kill()
    w._proc.wait()
    w.write(frames(0.0, 2.0))
    w.close()
    assert w.error is not None


def test_empty_writer_leaves_no_file(ff, tmp_path):
    w = TrackWriter(ff, tmp_path / "t.ogg", t0=0.0, bitrate_kbps=48)
    w.close()
    assert not (tmp_path / "t.ogg").exists()
