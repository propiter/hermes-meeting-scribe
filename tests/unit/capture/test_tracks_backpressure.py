"""Review C1: silence is a marker (O(1) memory) and the writer queue is bounded with backpressure."""
from __future__ import annotations

import threading
import time
import tracemalloc
from pathlib import Path
from typing import Any, Optional

import pytest

from meeting_scribe.capture.tracks import BYTES_PER_SAMPLE, SAMPLE_RATE, Aligner, Silence, TrackWriter

FRAME = b"\x10\x00\x10\x00" * 960  # 20 ms stereo s16le


class GatedStdin:
    """ffmpeg stdin that blocks until ``gate`` is set (a slow/stalled encoder)."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.data = bytearray()
        self.closed = False

    def write(self, chunk: Any) -> int:
        self.gate.wait(10)
        self.data += bytes(chunk)
        return len(chunk)

    def close(self) -> None:
        self.closed = True


class FakeProc:
    def __init__(self) -> None:
        self.stdin = GatedStdin()
        self.stderr: Optional[Any] = None
        self.returncode = 0

    def wait(self, timeout: Optional[float] = None) -> int:
        return 0

    def kill(self) -> None:
        pass


def make_writer(tmp_path: Path, **kw: Any) -> tuple[TrackWriter, FakeProc]:
    proc = FakeProc()
    w = TrackWriter(None, tmp_path / "t.ogg", t0=0.0, bitrate_kbps=48, popen=lambda *a, **k: proc, **kw)
    return w, proc


def test_aligner_gap_is_a_constant_size_marker() -> None:
    al = Aligner(t0=0.0)
    tracemalloc.start()
    out = al.feed([(7200.0, FRAME)])  # a speaker who first talks two hours in
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    markers = [c for c in out if isinstance(c, Silence)]
    assert sum(m.samples for m in markers) == 7200 * SAMPLE_RATE
    assert peak < 1_000_000  # the old code allocated ~1.38 GB here


def test_pump_writes_silence_from_a_reused_zero_buffer(tmp_path: Path) -> None:
    w, proc = make_writer(tmp_path)
    proc.stdin.gate.set()
    w.write([(3.0, FRAME)])
    w.close()
    assert len(proc.stdin.data) == 3 * SAMPLE_RATE * BYTES_PER_SAMPLE + len(FRAME)
    assert not bytes(proc.stdin.data[:-len(FRAME)]).strip(b"\x00")
    assert w.error is None


def test_write_never_blocks_and_overflow_is_accounted_not_dropped(tmp_path: Path) -> None:
    w, proc = make_writer(tmp_path, max_queue=4)
    started = time.monotonic()
    for i in range(50):  # the encoder is stalled: the loop-side put must not block
        w.write([(i * 0.02, FRAME)])
    assert time.monotonic() - started < 0.5
    assert w.queued_items <= 4
    assert w.overflow_bytes > 0
    proc.stdin.gate.set()
    w.close()
    assert bytes(proc.stdin.data) == FRAME * 50  # every frame arrived, in order
    assert w.error is None


def test_backlog_beyond_cap_fails_the_track_loudly(tmp_path: Path) -> None:
    w, proc = make_writer(tmp_path, max_queue=1, max_backlog_bytes=len(FRAME) * 3)
    for i in range(20):
        w.write([(i * 0.02, FRAME)])
    assert w.error is not None and "backlog" in w.error
    proc.stdin.gate.set()
    w.close()


def test_stderr_is_not_a_pipe(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def popen(*args: Any, **kw: Any) -> FakeProc:
        captured.update(kw)
        p = FakeProc()
        p.stdin.gate.set()
        return p

    w = TrackWriter(None, tmp_path / "t.ogg", t0=0.0, bitrate_kbps=48, popen=popen)
    w.close()
    import subprocess
    assert captured["stderr"] is not subprocess.PIPE  # a chatty ffmpeg must never fill a pipe
