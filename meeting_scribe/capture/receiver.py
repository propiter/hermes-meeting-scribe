"""Scribe receiver: Hermes' ``VoiceReceiver`` with timeline-stamped buffers (DESIGN §4, §15).

Hermes' ``_on_packet`` decrypts (NaCl + DAVE) and decodes Opus, then appends PCM with
``self._buffers[ssrc].extend(pcm)`` under ``self._lock``. We swap ``_buffers`` for a
:class:`_Buffers` mapping of :class:`TimedBuffer` so every 20 ms frame is recorded with the
(monotonic) time it arrived, which is what lets :mod:`tracks` align each speaker to the meeting
timeline. Nothing else of the packet path is copied; the compat probe verifies the seam exists.

DAVE (review note): Hermes skips DAVE decryption for an SSRC that SPEAKING has not mapped yet and
decodes the still-E2EE payload as Opus, which yields noise. While a DAVE session is active, frames
of an unmapped SSRC are therefore dropped at the seam instead of being attached to the speaker
once SPEAKING arrives.

The scribe never calls ``check_silence``/``flush_pending`` (they would hand utterances to the
agent) and ignores ``allowed_user_ids``: a meeting records everyone in the channel.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

Clock = Callable[[], float]
Frames = list[tuple[float, bytes]]
_CLASSES: dict[type, type] = {}


class TimedBuffer:
    """Drop-in for the ``bytearray`` Hermes extends; keeps ``(time, pcm)`` frames."""

    __slots__ = ("clock_fn", "frames", "_size", "_accept")

    def __init__(self, clock: Clock = time.monotonic, accept: Optional[Callable[[], bool]] = None) -> None:
        self.clock_fn = clock
        self.frames: Frames = []
        self._size = 0
        self._accept = accept

    def extend(self, pcm: bytes) -> None:
        if self._accept is not None and not self._accept():
            return
        data = bytes(pcm)
        self.frames.append((self.clock_fn(), data))
        self._size += len(data)

    def __len__(self) -> int:
        return self._size

    def __bytes__(self) -> bytes:
        return b"".join(d for _, d in self.frames)

    def prune_older_than(self, cutoff: float) -> None:
        kept = [f for f in self.frames if f[0] >= cutoff]
        self.frames = kept
        self._size = sum(len(d) for _, d in kept)


class _Buffers(dict):
    """``defaultdict`` whose factory knows the SSRC (needed for the DAVE unmapped-SSRC guard)."""

    def __init__(self, make: Callable[[int], TimedBuffer]) -> None:
        super().__init__()
        self._make = make

    def __missing__(self, ssrc: int) -> TimedBuffer:
        buf = self[ssrc] = self._make(ssrc)
        return buf


def scribe_receiver_class(base: type) -> type:
    """Build (once per base) the ``ScribeReceiver`` subclass of Hermes' ``VoiceReceiver``.

    Built lazily because the Hermes adapter module only imports inside the gateway."""
    cached = _CLASSES.get(base)
    if cached is not None:
        return cached

    class ScribeReceiver(base):  # type: ignore[valid-type, misc]
        UNMAPPED_MAX_AGE = 5.0  # seconds of audio kept for an SSRC before SPEAKING maps it

        def __init__(self, voice_client: Any, *, clock: Clock = time.monotonic) -> None:
            super().__init__(voice_client, allowed_user_ids=None)
            self._clock = clock
            self._buffers = _Buffers(self._new_buffer)

        def _new_buffer(self, ssrc: int) -> TimedBuffer:
            def accept() -> bool:  # runs inside Hermes' ``with self._lock`` around extend()
                return not (self._dave_session and not self._ssrc_to_user.get(ssrc))
            return TimedBuffer(self._clock, accept)

        def refresh_connection(self) -> None:
            """Re-read DAVE session / transport key (cheap attribute reads; called every tick)."""
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
