"""Scribe receiver: Hermes' ``VoiceReceiver`` with timeline-stamped buffers (DESIGN §4, §4.1, §15).

Hermes' ``_on_packet`` decrypts (NaCl + DAVE) and decodes Opus, then appends PCM with
``self._buffers[ssrc].extend(pcm)`` under ``self._lock``. We swap ``_buffers`` for a
:class:`_Buffers` mapping of :class:`TimedBuffer` so every 20 ms frame is recorded with the
(monotonic) time it arrived, which is what lets :mod:`tracks` align each speaker to the meeting
timeline. Nothing else of the packet path is copied; the compat probe verifies the seams exist.

Unmapped SSRCs (DESIGN §4.1). Hermes learns who owns an SSRC only from the voice SPEAKING event
(op 5), and Discord does not always send it — e.g. for people already talking when the bot joins.
Hermes then skips DAVE for that SSRC and hands the still end-to-end encrypted payload to the Opus
decoder. We swap ``_decoders`` for :class:`_Decoders`: for an unmapped SSRC it returns a
:class:`_Retainer` instead of a real decoder, so the payload Hermes produced after the transport
(NaCl) layer is *kept* — never decoded as noise — while the SSRC is identified:

* DAVE frame (ends with the ``0xFAFA`` marker, a DAVE session is active): the frame is opened with
  ``dave_session.decrypt(user_id, ...)`` of every candidate (people in the voice channel plus the
  DAVE group). It is AES-GCM under a per-sender key, so only its owner's key authenticates it.
  After ``CONFIRM_PACKETS`` frames that open *and* decode as Opus for exactly one person — and never
  for anyone else — the SSRC is theirs. The opened Opus is cached with the frame: a DAVE decryptor
  refuses a nonce it has already processed, so a frame cannot be opened twice.
* Plain Opus (no DAVE, or DAVE passthrough): mapped only when, after ``IDENTIFY_GRACE`` seconds
  without SPEAKING, exactly one unmuted person in the channel has no SSRC and this is the only
  unmapped SSRC talking.

Once mapped, the retained audio is decoded and stamped with its ORIGINAL arrival times, so nothing
said before identification is lost. When no safe decision exists the audio is never attributed to
a guess: after ``UNIDENTIFIED_AFTER`` seconds audio that can be decoded becomes an "unidentified
participant" track; DAVE audio no candidate's key opens cannot be decoded at all, stays pending
(bounded) and is reported at the end (:meth:`voice_report`).

Memory: retained payloads are compressed packets (~100-200 bytes / 20 ms), capped per SSRC by
``RETAIN_SECONDS`` and globally by ``RETAIN_MAX_BYTES`` (oldest dropped first, counted).

The scribe never calls ``check_silence``/``flush_pending`` (they would hand utterances to the
agent) and ignores ``allowed_user_ids``: a meeting records everyone in the channel.
"""
from __future__ import annotations

import collections
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger(__name__)

Clock = Callable[[], float]
Frames = list[tuple[float, bytes]]
_CLASSES: dict[type, type] = {}
DAVE_MAGIC = b"\xfa\xfa"
UNIDENTIFIED_PREFIX = "unidentified-"
_HOW = {"dave": "DAVE key", "sole": "only person without SSRC", "speaking": "late SPEAKING"}


class TimedBuffer:
    """Drop-in for the ``bytearray`` Hermes extends; keeps ``(time, pcm)`` frames."""

    __slots__ = ("clock_fn", "frames", "_size")

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self.clock_fn = clock
        self.frames: Frames = []
        self._size = 0

    def extend(self, pcm: bytes) -> None:
        if pcm:  # a retained packet (unmapped SSRC) yields b"": nothing to record yet
            self.add(self.clock_fn(), bytes(pcm))

    def add(self, t: float, pcm: bytes) -> None:
        self.frames.append((t, pcm))
        self._size += len(pcm)

    def __len__(self) -> int:
        return self._size

    def __bytes__(self) -> bytes:
        return b"".join(d for _, d in self.frames)


class _Buffers(dict):
    """``defaultdict`` of :class:`TimedBuffer` sharing the receiver clock."""

    def __init__(self, clock: Clock) -> None:
        super().__init__()
        self._clock = clock

    def __missing__(self, ssrc: int) -> TimedBuffer:
        buf = self[ssrc] = TimedBuffer(self._clock)
        return buf


class HostCodec:
    """Opus decoder + DAVE media type from the Hermes interpreter (imported lazily: gateway only)."""

    def new_decoder(self) -> Any:
        import discord.opus  # type: ignore[import-not-found]

        return discord.opus.Decoder()

    def audio_media(self) -> Any:
        import davey  # type: ignore[import-not-found]

        return davey.MediaType.audio


class _Frame:
    """One retained payload: arrival time, what Hermes had after NaCl, and the Opus each
    candidate's DAVE key opened it to (a DAVE frame can be opened only once)."""

    __slots__ = ("t", "raw", "opened")

    def __init__(self, t: float, raw: bytes) -> None:
        self.t = t
        self.raw = raw
        self.opened: dict[int, bytes] = {}

    @property
    def size(self) -> int:
        return len(self.raw) + sum(len(o) for o in self.opened.values())


@dataclass
class _Pending:
    """An SSRC without owner: its retained payloads and the identification evidence."""

    first: float
    last: float
    frames: "collections.deque[_Frame]" = field(default_factory=collections.deque)
    size: int = 0
    dropped: int = 0
    packets: int = 0
    dave: bool = False
    hits: dict[int, int] = field(default_factory=dict)  # user id -> frames their key opened
    label: Optional[str] = None  # set once it streams into an "unidentified participant" track

    @property
    def ambiguous(self) -> bool:
        return len(self.hits) > 1


@dataclass(frozen=True)
class VoiceReport:
    """What happened to SSRCs SPEAKING never mapped (read at the end of a recording)."""

    identified: dict[int, tuple[int, str]]  # ssrc -> (user id, "dave" | "sole" | "speaking")
    unidentified: dict[str, int]  # track label -> ssrc (audio kept, owner unknown)
    resolved: dict[str, int]  # unidentified label -> user a later SPEAKING named
    undecided: dict[int, int]  # ssrc -> packets that could be neither attributed nor decoded


class _Retainer:
    """Stand-in decoder Hermes calls for an unmapped SSRC: keeps the payload, yields no PCM."""

    __slots__ = ("_rx", "_ssrc")

    def __init__(self, rx: Any, ssrc: int) -> None:
        self._rx = rx
        self._ssrc = ssrc

    def decode(self, payload: bytes) -> bytes:
        self._rx._retain(self._ssrc, bytes(payload))
        return b""


class _Decoders(dict):
    """Hermes' per-SSRC Opus decoders; an unmapped SSRC gets a :class:`_Retainer` instead.

    Hermes runs ``if ssrc not in self._decoders: ... = Decoder()`` then
    ``self._decoders[ssrc].decode(payload)``; both lookups go through here."""

    def __init__(self, rx: Any) -> None:
        super().__init__()
        self._rx = rx

    def __contains__(self, ssrc: object) -> bool:
        return self._rx._is_unmapped(ssrc) or dict.__contains__(self, ssrc)

    def __getitem__(self, ssrc: Any) -> Any:
        if self._rx._is_unmapped(ssrc):
            return _Retainer(self._rx, ssrc)
        return dict.__getitem__(self, ssrc)  # mapped between the two lookups: Hermes skips 20 ms


def scribe_receiver_class(base: type) -> type:
    """Build (once per base) the ``ScribeReceiver`` subclass of Hermes' ``VoiceReceiver``.

    Built lazily because the Hermes adapter module only imports inside the gateway."""
    cached = _CLASSES.get(base)
    if cached is not None:
        return cached

    class ScribeReceiver(base):  # type: ignore[valid-type, misc]
        CONFIRM_PACKETS = 3  # DAVE frames one person's key must open before the SSRC is theirs
        IDENTIFY_GRACE = 2.0  # seconds SPEAKING may lag before the sole-candidate rule applies
        UNIDENTIFIED_AFTER = 10.0  # decodable audio still unattributable -> "unidentified" track
        ACTIVE_WINDOW = 5.0  # an unmapped SSRC silent longer than this is not "talking now"
        RETAIN_SECONDS = 60.0  # per-SSRC retention window
        RETAIN_MAX_BYTES = 4 * 1024 * 1024  # all SSRCs together

        def __init__(self, voice_client: Any, *, clock: Clock = time.monotonic,
                     codec: Optional[Any] = None) -> None:
            super().__init__(voice_client, allowed_user_ids=None)
            self._clock = clock
            self._codec = codec or HostCodec()
            self._buffers = _Buffers(clock)
            self._decoders = _Decoders(self)
            self._present: Optional[frozenset[int]] = None  # None until the first voice-state snapshot
            self._unmuted: frozenset[int] = frozenset()
            # Ordering of joins and mappings (a counter, not the clock): a mapping older than the
            # member's last join belongs to a previous connection (a rejoin gets a new SSRC).
            self._order = itertools.count(1)
            self._joined: dict[int, int] = {}
            self._mapped_at: dict[int, int] = {}
            self._pending: dict[int, _Pending] = {}
            self._retained_bytes = 0
            self._unidentified: dict[str, Frames] = {}
            self._label_decoders: dict[str, Any] = {}
            self._identified: dict[int, tuple[int, str]] = {}
            self._labels: dict[str, int] = {}
            self._resolved: dict[str, int] = {}  # unidentified label -> user SPEAKING named later

        # -- presence (event-loop thread) -------------------------------------------------------
        def update_presence(self, user_ids: Iterable[int], muted: Iterable[int] = ()) -> None:
            """The people in the voice channel right now (voice states) and which are muted."""
            present = frozenset(int(u) for u in user_ids)
            with self._lock:
                if self._present is not None:  # the first snapshot is the baseline, not joins
                    for u in present - self._present:
                        self._joined[u] = next(self._order)
                self._present = present
                self._unmuted = present - {int(u) for u in muted}

        def refresh_connection(self) -> None:
            """Re-read DAVE session / transport key (cheap attribute reads; called every tick)."""
            conn = self._vc._connection
            self._dave_session = getattr(conn, "dave_session", None)
            try:
                self._secret_key = bytes(conn.secret_key)
            except TypeError:  # MISSING while the voice websocket is mid-handshake; keep the old key
                pass

        def _dave_frame(self, payload: bytes) -> bool:
            return bool(self._dave_session) and payload[-2:] == DAVE_MAGIC

        def _key_candidates(self) -> list[int]:
            """Whose DAVE key to try: people in the channel plus the DAVE group, minus the bot."""
            ids = set(self._present or ())
            try:
                ids.update(int(u) for u in self._dave_session.get_user_ids())
            except (AttributeError, TypeError, ValueError):  # a session without a group yet
                pass
            ids.discard(int(getattr(self._dave_session, "user_id", 0) or 0))
            return sorted(ids)

        def map_ssrc(self, ssrc: int, user_id: int) -> None:
            """SPEAKING (authoritative): map, and replay audio retained before it arrived."""
            with self._lock:
                p = self._pending.get(ssrc)
                if p is not None and p.label is None:
                    kept = self._claim(ssrc, int(user_id), "speaking")
                else:
                    if p is not None:  # already streaming as unidentified: name that track after them
                        self._resolved[p.label] = int(user_id)
                        self._pending.pop(ssrc)
                    self._ssrc_to_user[ssrc] = user_id
                    self._mapped_at[ssrc] = next(self._order)
                    return
            self._log_identified(ssrc, int(user_id), "speaking", kept, p)

        # -- unmapped SSRCs (reader thread, via _Retainer) ---------------------------------------
        def _is_unmapped(self, ssrc: object) -> bool:
            return not self._ssrc_to_user.get(ssrc)  # type: ignore[call-overload]

        def _retain(self, ssrc: int, payload: bytes) -> None:
            now = self._clock()
            dave = self._dave_frame(payload)
            frame = _Frame(now, payload)
            if dave:  # outside the lock: trial decryption is the slow part
                for u in self._key_candidates():
                    opus = self._open(u, payload)
                    if opus is not None and self._decodes(opus):
                        frame.opened[u] = opus
            with self._lock:
                p = self._pending.get(ssrc)
                if p is None:
                    p = self._pending[ssrc] = _Pending(first=now, last=now)
                    log.info("meeting-scribe: audio from ssrc=%d before SPEAKING; identifying", ssrc)
                p.last = now
                p.packets += 1
                p.dave = p.dave or dave
                was_ambiguous = p.ambiguous
                for u in frame.opened:
                    p.hits[u] = p.hits.get(u, 0) + 1
                if p.ambiguous and not was_ambiguous:
                    log.warning("meeting-scribe: ssrc=%d opens with several keys %s; not attributed",
                                ssrc, sorted(p.hits))
                label = p.label
                if label is None:
                    self._keep(p, frame)
            if label is not None:
                self._stream_unidentified(label, frame)
            elif dave:
                self._try_keys(ssrc)
            elif now - p.first >= self.IDENTIFY_GRACE:
                self._try_sole(ssrc)

        def _keep(self, p: _Pending, frame: _Frame) -> None:
            """Caller holds ``_lock``. Bounded: window per SSRC, then a global byte cap."""
            p.frames.append(frame)
            p.size += frame.size
            self._retained_bytes += frame.size
            while p.frames and frame.t - p.frames[0].t > self.RETAIN_SECONDS:
                self._drop_oldest(p)
            while self._retained_bytes > self.RETAIN_MAX_BYTES:
                self._drop_oldest(min((q for q in self._pending.values() if q.frames),
                                      key=lambda q: q.frames[0].t))

        def _drop_oldest(self, p: _Pending) -> None:
            old = p.frames.popleft()
            p.size -= old.size
            p.dropped += 1
            self._retained_bytes -= old.size

        def _open(self, user_id: int, payload: bytes) -> Optional[bytes]:
            """Opus payload of a DAVE frame if ``user_id``'s key authenticates it, else None."""
            try:
                return bytes(self._dave_session.decrypt(int(user_id), self._codec.audio_media(), payload))
            except Exception:  # wrong key, no decryptor for that user, nonce seen, passthrough refused
                return None

        def _decodes(self, opus: bytes) -> bool:
            try:
                return bool(self._codec.new_decoder().decode(opus))
            except Exception:  # not Opus: whatever "opened" it, it is no evidence
                return False

        def _try_keys(self, ssrc: int) -> None:
            with self._lock:
                p = self._pending.get(ssrc)
                if p is None or p.label is not None or p.ambiguous or not p.hits:
                    return
                (user_id, hits), = p.hits.items()
                if hits < self.CONFIRM_PACKETS:
                    return
                kept = self._claim(ssrc, user_id, "dave")
            self._log_identified(ssrc, user_id, "dave", kept, p)

        def _try_sole(self, ssrc: int) -> None:
            now = self._clock()
            with self._lock:
                p = self._pending.get(ssrc)
                if p is None or p.label is not None or p.hits or (p.dave and self._dave_session):
                    return
                talking = [s for s, q in self._pending.items()
                           if q.label is None and now - q.last <= self.ACTIVE_WINDOW]
                mapped = {int(u) for s, u in self._ssrc_to_user.items()
                          if u and self._mapped_at.get(s, 0) >= self._joined.get(int(u), 0)}
                candidates = sorted(self._unmuted - mapped)
                if talking != [ssrc] or len(candidates) != 1:
                    return
                user_id = candidates[0]
                kept = self._claim(ssrc, user_id, "sole")
            self._log_identified(ssrc, user_id, "sole", kept, p)

        def _claim(self, ssrc: int, user_id: int, how: str) -> int:
            """Caller holds ``_lock``. Map the SSRC and decode its retained audio into the buffer at
            the original arrival times, atomically: no drain and no second guess can interleave.
            Returns how many retained frames made it."""
            p = self._pending.pop(ssrc)
            self._retained_bytes -= p.size
            self._ssrc_to_user[ssrc] = user_id
            self._mapped_at[ssrc] = next(self._order)
            self._identified[ssrc] = (user_id, how)
            decoder = self._codec.new_decoder()
            buf = self._buffers[ssrc]
            kept = 0
            for f in p.frames:
                pcm = self._pcm(decoder, f, user_id)
                if pcm:
                    buf.add(f.t, pcm)
                    kept += 1
            return kept

        def _pcm(self, decoder: Any, frame: _Frame, user_id: Optional[int]) -> Optional[bytes]:
            """PCM of a retained frame for ``user_id`` (None when it cannot be opened/decoded)."""
            payload: Optional[bytes] = frame.raw
            if user_id is not None and user_id in frame.opened:
                payload = frame.opened[user_id]
            elif self._dave_frame(frame.raw):
                payload = self._open(user_id, frame.raw) if user_id is not None else None
            if payload is None:
                return None
            try:
                return bytes(decoder.decode(payload))
            except Exception:  # a corrupt packet: skip it like Hermes does, keep the rest
                return None

        @staticmethod
        def _log_identified(ssrc: int, user_id: int, how: str, kept: int, p: _Pending) -> None:
            log.info("meeting-scribe: ssrc=%d identified as user %d (%s); %d earlier frames kept, %d lost",
                     ssrc, user_id, _HOW[how], kept, p.dropped + len(p.frames) - kept)

        # -- unidentified participant tracks -----------------------------------------------------
        def _stream_unidentified(self, label: str, frame: _Frame) -> None:
            """Audio whose owner is unknown: plain Opus, or a DAVE frame several keys opened (the
            content is the same whichever key opened it)."""
            opened = next(iter(frame.opened.values()), None)
            if opened is None and self._dave_frame(frame.raw):
                return
            try:
                pcm = bytes(self._label_decoders[label].decode(opened if opened is not None else frame.raw))
            except Exception:  # corrupt packet
                return
            if pcm:
                with self._lock:
                    self._unidentified.setdefault(label, []).append((frame.t, pcm))

        def _settle(self) -> None:
            """Event-loop side of identification: late sole-candidate decisions (the speaker stopped
            before the grace ran out) and the switch of unattributable audio to a labelled track."""
            now = self._clock()
            with self._lock:
                waiting = [(s, p) for s, p in self._pending.items() if p.label is None]
            for ssrc, p in waiting:
                if now - p.first >= self.IDENTIFY_GRACE:
                    self._try_sole(ssrc)
                with self._lock:
                    if self._pending.get(ssrc) is not p or now - p.first < self.UNIDENTIFIED_AFTER:
                        continue
                    if p.dave and self._dave_session and not p.ambiguous:
                        continue  # only its owner's key can open it: keep trying (bounded)
                    label = f"{UNIDENTIFIED_PREFIX}{len(self._labels) + 1}"
                    self._labels[label] = ssrc
                    p.label = label
                    retained, p.frames = list(p.frames), collections.deque()
                    self._retained_bytes -= p.size
                    p.size = 0
                    self._label_decoders[label] = self._codec.new_decoder()
                log.warning("meeting-scribe: ssrc=%d could not be attributed to anyone; recorded as %s",
                            ssrc, label)
                for frame in retained:
                    self._stream_unidentified(label, frame)

        # -- drain (event-loop thread) -----------------------------------------------------------
        def drain(self) -> dict[int, Frames]:
            """Swap out all mapped buffers under the receiver lock; ``{user_id: [(t, pcm), ...]}``."""
            self._settle()
            out: dict[int, Frames] = {}
            with self._lock:
                mapping = dict(self._ssrc_to_user)
                for ssrc in list(self._buffers):
                    user_id: Optional[int] = mapping.get(ssrc)
                    buf = self._buffers.pop(ssrc)
                    if user_id and buf.frames:
                        out.setdefault(int(user_id), []).extend(buf.frames)
            for frames in out.values():
                frames.sort(key=lambda f: f[0])
            return out

        def drain_unidentified(self) -> dict[str, Frames]:
            """``{label: frames}`` of audio whose owner could not be established safely."""
            with self._lock:
                out, self._unidentified = self._unidentified, {}
            for frames in out.values():
                frames.sort(key=lambda f: f[0])
            return out

        def voice_report(self) -> VoiceReport:
            with self._lock:
                undecided = {s: p.packets for s, p in self._pending.items() if p.label is None}
                return VoiceReport(dict(self._identified), dict(self._labels), dict(self._resolved), undecided)

        def stop(self) -> None:
            super().stop()
            with self._lock:  # the report (identified/labels/undecided counts) survives the stop
                for p in self._pending.values():
                    p.frames.clear()
                    p.size = 0
                self._retained_bytes = 0

    ScribeReceiver.__qualname__ = ScribeReceiver.__name__ = "ScribeReceiver"
    _CLASSES[base] = ScribeReceiver
    return ScribeReceiver
