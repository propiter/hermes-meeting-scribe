"""Live per-speaker Ogg/Opus writer, aligned to the meeting timeline (DESIGN §4).

One ``ffmpeg`` process per speaker reads s16le 48 kHz stereo on stdin and writes a mono Ogg/Opus
file. Ogg is page-based and streamable, so a crash leaves a playable file up to the last page.

Alignment: sample position ``n`` of every track corresponds to ``t0 + n / 48000`` of the meeting.
Frames carry the wall-clock time they were decoded; when a frame arrives more than 100 ms after
the current write position, the gap is filled with zeros. Frames that arrive "early" (network
jitter) are appended as-is — the position never moves backwards, so a small drift is accepted
instead of overwriting audio.

``write`` only enqueues; a dedicated thread feeds ffmpeg, so the gateway event loop never blocks
on a pipe. Failures (ffmpeg crash, broken pipe) are recorded in ``error`` and never raised into
the drain loop: one broken speaker track must not end the meeting for everyone.
"""
from __future__ import annotations

import logging
import queue
import subprocess
import threading
from pathlib import Path
from typing import Iterable, Optional

from ..audio.ffmpeg import Ffmpeg

log = logging.getLogger(__name__)
SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SAMPLE = 2 * CHANNELS  # one stereo s16le sample frame
GAP_THRESHOLD = 0.100
_CLOSE = object()


class Aligner:
    """Pure timeline bookkeeping: turns timed frames into the byte chunks to write."""

    MAX_CHUNK = SAMPLE_RATE * BYTES_PER_SAMPLE  # 1 s of silence per chunk

    def __init__(self, t0: float) -> None:
        self.t0 = t0
        self.samples = 0

    @property
    def position_seconds(self) -> float:
        return self.samples / SAMPLE_RATE

    def _silence(self, samples: int) -> list[bytes]:
        out: list[bytes] = []
        remaining = samples * BYTES_PER_SAMPLE
        while remaining > 0:
            size = min(remaining, self.MAX_CHUNK)
            out.append(bytes(size))
            remaining -= size
        return out

    def feed(self, frames: Iterable[tuple[float, bytes]]) -> list[bytes]:
        out: list[bytes] = []
        for t, pcm in frames:
            target = int(round((t - self.t0) * SAMPLE_RATE))
            gap = target - self.samples
            if gap > GAP_THRESHOLD * SAMPLE_RATE:
                out += self._silence(gap)
                self.samples += gap
            usable = len(pcm) - len(pcm) % BYTES_PER_SAMPLE
            if usable:
                out.append(pcm[:usable])
                self.samples += usable // BYTES_PER_SAMPLE
        return out


class TrackWriter:
    def __init__(self, ff: Ffmpeg, path: Path, *, t0: float, bitrate_kbps: int) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.error: Optional[str] = None
        self._aligner = Aligner(t0)
        self._queue: "queue.Queue[object]" = queue.Queue()
        self._closed = False
        self._wrote = False
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(
            [str(ff.ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(SAMPLE_RATE),
             "-ac", str(CHANNELS), "-i", "pipe:0", "-ac", "1", "-c:a", "libopus", "-b:a", f"{bitrate_kbps}k",
             "-application", "voip", "-f", "ogg", str(self.path)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self._thread = threading.Thread(target=self._pump, name=f"meeting-scribe-track-{self.path.stem}",
                                        daemon=True)
        self._thread.start()

    @property
    def duration_seconds(self) -> float:
        return self._aligner.position_seconds

    def write(self, frames: Iterable[tuple[float, bytes]]) -> None:
        with self._lock:
            if self._closed or self.error:
                return
            chunks = self._aligner.feed(frames)
            if chunks:
                self._wrote = True
                for chunk in chunks:
                    self._queue.put(chunk)

    def _pump(self) -> None:
        stdin = self._proc.stdin
        assert stdin is not None
        while True:
            item = self._queue.get()
            if item is _CLOSE:
                break
            if self.error:
                continue  # keep draining so close() never waits on a full queue
            try:
                stdin.write(item)  # type: ignore[arg-type]
            except (BrokenPipeError, OSError, ValueError) as exc:
                self.error = f"ffmpeg stdin closed: {type(exc).__name__}: {exc}"
                log.warning("meeting-scribe track %s: %s", self.path.name, self.error)

    def close(self, timeout: float = 30.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._queue.put(_CLOSE)
        self._thread.join(timeout)
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            self.error = self.error or f"ffmpeg stdin close failed: {exc}"
        try:
            rc = self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            rc = self._proc.wait()
            self.error = self.error or "ffmpeg did not finish in time; killed"
        stderr = (self._proc.stderr.read().decode("utf-8", "replace").strip() if self._proc.stderr else "")
        if self._proc.stderr is not None:
            self._proc.stderr.close()
        if rc != 0:
            self.error = self.error or f"ffmpeg exited {rc}: {stderr[-400:]}"
        if not self._wrote:
            self.path.unlink(missing_ok=True)
