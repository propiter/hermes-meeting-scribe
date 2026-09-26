"""Scribe receiver: Hermes' ``VoiceReceiver`` with wall-clock-stamped buffers (DESIGN §4).

Hermes' ``_on_packet`` decrypts (NaCl + DAVE) and decodes Opus, then appends PCM with
``self._buffers[ssrc].extend(pcm)`` under ``self._lock``. We swap ``_buffers`` for a
``defaultdict(TimedBuffer)`` so every 20 ms frame is recorded with the wall-clock time it arrived,
which is what lets :mod:`tracks` align each speaker to the meeting timeline. Nothing else of the
packet path is copied; the compat probe verifies the seam still exists.

The scribe never calls ``check_silence``/``flush_pending`` (they would hand utterances to the
agent) and ignores ``allowed_user_ids``: a meeting records everyone in the channel.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Callable, Optional

Clock = Callable[[], float]
Frames = list[tuple[float, bytes]]
_CLASSES: dict[type, type] = {}


class TimedBuffer:
    """Drop-in for the ``bytearray`` Hermes extends; keeps ``(wallclock, pcm)`` frames."""

    __slots__ = ("_clock", "frames", "_size")

    def __init__(self, clock: Clock = time.time) -> None:
        self._clock = clock
        self.frames: Frames = []
        self._size = 0

    def extend(self, pcm: bytes) -> None:
        data = bytes(pcm)
        self.frames.append((self._clock(), data))
        self._size += len(data)

    def __len__(self) -> int:
        return self._size

    def __bytes__(self) -> bytes:
        return b"".join(d for _, d in self.frames)

    def prune_older_than(self, cutoff: float) -> None:
        kept = [f for f in self.frames if f[0] >= cutoff]
        self.frames = kept
        self._size = sum(len(d) for _, d in kept)


def scribe_receiver_class(base: type) -> type:
    """Build (once per base) the ``ScribeReceiver`` subclass of Hermes' ``VoiceReceiver``.

    Built lazily because the Hermes adapter module only imports inside the gateway."""
    cached = _CLASSES.get(base)
    if cached is not None:
        return cached

    class ScribeReceiver(base):  # type: ignore[valid-type, misc]
        UNMAPPED_MAX_AGE = 5.0  # seconds of audio kept for an SSRC before SPEAKING maps it

        def __init__(self, voice_client: Any, *, clock: Clock = time.time) -> None:
            super().__init__(voice_client, allowed_user_ids=None)
            self._clock = clock
            self._buffers = defaultdict(lambda: TimedBuffer(clock))

        def refresh_connection(self) -> None:
            """Re-read DAVE session / transport key after a voice reconnect or DAVE transition."""
            conn = self._vc._connection
            self._dave_session = getattr(conn, "dave_session", None)
            try:
                self._secret_key = bytes(conn.secret_key)
            except TypeError:  # MISSING while the voice websocket is mid-handshake; keep the old key
                pass

        def drain(self) -> dict[int, Frames]:
            """Swap out all mapped buffers under the receiver lock; ``{user_id: [(t, pcm), ...]}``."""
            out: dict[int, Frames] = {}
            cutoff = self._clock() - self.UNMAPPED_MAX_AGE
            with self._lock:
                mapping = dict(self._ssrc_to_user)
                for ssrc in list(self._buffers):
                    buf = self._buffers[ssrc]
                    user_id: Optional[int] = mapping.get(ssrc)
                    if user_id:
                        out.setdefault(int(user_id), []).extend(buf.frames)
                        del self._buffers[ssrc]
                    else:
                        buf.prune_older_than(cutoff)
                        if not buf.frames:
                            del self._buffers[ssrc]
            for frames in out.values():
                frames.sort(key=lambda f: f[0])
            return out

    ScribeReceiver.__qualname__ = ScribeReceiver.__name__ = "ScribeReceiver"
    _CLASSES[base] = ScribeReceiver
    return ScribeReceiver
