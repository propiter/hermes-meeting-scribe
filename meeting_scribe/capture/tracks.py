"""Live per-speaker Ogg/Opus writer, aligned to the meeting timeline (DESIGN §4, §15).

One ``ffmpeg`` process per speaker reads s16le 48 kHz stereo on stdin and writes a mono Ogg/Opus
file. Ogg is page-based and streamable, so a crash leaves a playable file up to the last page.

Alignment: sample position ``n`` of every track corresponds to ``t0 + n / 48000`` of the meeting
(``t0`` and the frame stamps come from the same monotonic clock, so NTP steps cannot inject or
swallow audio). When a frame arrives more than 100 ms after the current write position the gap is
filled with silence. Frames that arrive "early" (network jitter) are appended as-is — the position
never moves backwards, so a small drift is accepted instead of overwriting audio.

Memory (review C1): a gap is a :class:`Silence` *marker* (a sample count), never materialised
bytes; the pump thread writes it from one reused zero buffer. A speaker who first talks two hours
in costs a few bytes, not 1.4 GB.

Backpressure (review C1): the event loop calls :meth:`TrackWriter.write`, which must never block
and must never drop audio. Items go to a bounded ``queue.Queue`` with ``put_nowait``; when it is
full they wait, in order, in an overflow deque that the pump thread moves into the queue as slots
free up. The pump thread is the only one that blocks (on ffmpeg's stdin). ``overflow_bytes`` /
``max_overflow_bytes`` account for the backlog. If the backlog exceeds ``max_backlog_bytes`` the
encoder is considered stalled and the track fails loudly (``error``) — earlier audio is still
flushed — instead of growing without bound or silently discarding the oldest audio.

ffmpeg's stderr goes to an anonymous temp file, never a pipe nobody reads. Failures are recorded
in ``error`` and never raised into the drain loop: one broken track must not end the meeting.
"""
from __future__ import annotations

import collections
import logging
import queue
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Union

from ..audio.ffmpeg import Ffmpeg

log = logging.getLogger(__name__)
SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SAMPLE = 2 * CHANNELS  # one stereo s16le sample frame
GAP_THRESHOLD = 0.100
MAX_QUEUE = 256  # ~5 s of 20 ms frames between the loop and the pump before overflow
MAX_BACKLOG_BYTES = 64 * 1024 * 1024  # ~5.8 min of stereo PCM: beyond that ffmpeg is stalled
_CLOSE = object()


@dataclass(frozen=True)
class Silence:
    """``samples`` stereo sample frames of digital silence (materialised only by the pump)."""

    samples: int


Chunk = Union[bytes, Silence]


class Aligner:
    """Pure timeline bookkeeping: turns timed frames into the chunks to write."""

    MAX_CHUNK = SAMPLE_RATE * BYTES_PER_SAMPLE  # size of the pump's reused zero buffer (1 s)

    def __init__(self, t0: float) -> None:
        self.t0 = t0
        self.samples = 0

    @property
    def position_seconds(self) -> float:
        return self.samples / SAMPLE_RATE

    def feed(self, frames: Iterable[tuple[float, bytes]]) -> list[Chunk]:
        out: list[Chunk] = []
        for t, pcm in frames:
            target = int(round((t - self.t0) * SAMPLE_RATE))
            gap = target - self.samples
            if gap > GAP_THRESHOLD * SAMPLE_RATE:
                out.append(Silence(gap))
                self.samples += gap
            usable = len(pcm) - len(pcm) % BYTES_PER_SAMPLE
            if usable:
                out.append(bytes(pcm[:usable]))
                self.samples += usable // BYTES_PER_SAMPLE
        return out


_ZEROS = bytes(Aligner.MAX_CHUNK)


def _size(item: object) -> int:
    return len(item) if isinstance(item, bytes) else 0


class TrackWriter:
    def __init__(self, ff: Optional[Ffmpeg], path: Path, *, t0: float, bitrate_kbps: int,
                 max_queue: int = MAX_QUEUE, max_backlog_bytes: int = MAX_BACKLOG_BYTES,
                 popen: Callable[..., Any] = subprocess.Popen) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.error: Optional[str] = None
        self.max_overflow_bytes = 0
        self._aligner = Aligner(t0)
        self._queue: "queue.Queue[object]" = queue.Queue(maxsize=max(1, max_queue))
        self._overflow: "collections.deque[object]" = collections.deque()
        self._overflow_bytes = 0
        self._max_backlog = max_backlog_bytes
        self._closed = False
        self._wrote = False
        self._stdin_broken = False
        self._lock = threading.Lock()
        self._stderr = tempfile.TemporaryFile()
        binary = str(ff.ffmpeg) if ff is not None else "ffmpeg"
        self._proc = popen(
            [binary, "-hide_banner", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(SAMPLE_RATE),
             "-ac", str(CHANNELS), "-i", "pipe:0", "-ac", "1", "-c:a", "libopus", "-b:a", f"{bitrate_kbps}k",
             "-application", "voip", "-f", "ogg", str(self.path)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._stderr)
        self._thread = threading.Thread(target=self._pump, name=f"meeting-scribe-track-{self.path.stem}",
                                        daemon=True)
        self._thread.start()

    @property
    def duration_seconds(self) -> float:
        return self._aligner.position_seconds

    @property
    def queued_items(self) -> int:
        return self._queue.qsize()

    @property
    def overflow_bytes(self) -> int:
        return self._overflow_bytes

    # -- loop side (never blocks) ---------------------------------------------------------------
    def _enqueue(self, item: object) -> None:
        """Caller holds ``_lock``. Order is preserved: once overflowing, everything overflows."""
        if not self._overflow:
            try:
                self._queue.put_nowait(item)
                return
            except queue.Full:
                pass
        self._overflow.append(item)
        self._overflow_bytes += _size(item)
        self.max_overflow_bytes = max(self.max_overflow_bytes, self._overflow_bytes)

    def write(self, frames: Iterable[tuple[float, bytes]]) -> None:
        with self._lock:
            if self._closed or self.error:
                return
            chunks = self._aligner.feed(frames)
            if not chunks:
                return
            self._wrote = True
            for chunk in chunks:
                self._enqueue(chunk)
            if self._overflow_bytes > self._max_backlog:
                self.error = (f"encoder backlog exceeded {self._max_backlog} bytes (ffmpeg stalled); "
                              "track stopped, earlier audio kept")
                log.error("meeting-scribe track %s: %s", self.path.name, self.error)

    # -- pump thread ----------------------------------------------------------------------------
    def _refill(self) -> None:
        with self._lock:
            while self._overflow:
                try:
                    self._queue.put_nowait(self._overflow[0])
                except queue.Full:
                    return
                self._overflow_bytes -= _size(self._overflow.popleft())

    @staticmethod
    def _emit(stdin: Any, item: object) -> None:
        if isinstance(item, Silence):
            remaining = item.samples * BYTES_PER_SAMPLE
            view = memoryview(_ZEROS)
            while remaining > 0:
                n = min(remaining, len(_ZEROS))
                stdin.write(view[:n])
                remaining -= n
        else:
            stdin.write(item)

    def _pump(self) -> None:
        stdin = self._proc.stdin
        assert stdin is not None
        while True:
            item = self._queue.get()
            self._refill()
            if item is _CLOSE:
                break
            if self._stdin_broken:
                continue  # keep draining so close() never waits on a full queue
            try:
                self._emit(stdin, item)
            except (BrokenPipeError, OSError, ValueError) as exc:
                self._stdin_broken = True
                self.error = self.error or f"ffmpeg stdin closed: {type(exc).__name__}: {exc}"
                log.warning("meeting-scribe track %s: %s", self.path.name, self.error)

    # -- close (worker thread) ------------------------------------------------------------------
    def close(self, timeout: float = 30.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._enqueue(_CLOSE)
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
        stderr = self._read_stderr()
        if rc != 0:
            self.error = self.error or f"ffmpeg exited {rc}: {stderr[-400:]}"
        if not self._wrote:
            self.path.unlink(missing_ok=True)

    def _read_stderr(self) -> str:
        try:
            self._stderr.seek(0)
            return self._stderr.read().decode("utf-8", "replace").strip()
        except (OSError, ValueError) as exc:
            return f"<stderr unavailable: {exc}>"
        finally:
            self._stderr.close()
