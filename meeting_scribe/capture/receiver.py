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
  Frames retained before the owner's key was a candidate (their key/presence came later) are tried
  again whenever the candidate set grows, and every ``RETRY_SECONDS``, from the event loop.
* Plain Opus (no DAVE, or DAVE passthrough): no proof exists, so it is NEVER written to a person's
  own track. After ``UNIDENTIFIED_AFTER`` seconds without SPEAKING (or when the recording ends) the
  SSRC gets its own ``unidentified-N`` track — one SSRC is one Discord connection, so one person —
  and its owner is *inferred* as metadata (:attr:`VoiceReport.inferred`) whenever exactly one
  person can own it: of everyone in the call while it sent audio (voice states and ops 11/12,
  bots included), minus people whose SSRC is already known (mapped, proven or inferred), people
  absent at two consecutive snapshots while it talked and people whose join (voice state, op 11/12)
  was seen more than ``JOIN_LAG`` seconds after its first packet, one is left, and no other SSRC
  without owner could be theirs. Mute flags are no evidence (the voice-state cache lags: a real
  meeting had a person flagged muted while their audio arrived). The inference
  is recomputed on every drain, so it follows the call; a SPEAKING that contradicts it wins and is
  logged as a WARNING — the audio never touched the wrong person's track, only the label moves.

Once an owner is PROVEN (DAVE key or SPEAKING), the whole retained audio — from the first packet — is
decoded and stamped with its ORIGINAL arrival times, in batches of ``REPLAY_BATCH`` frames per drain
so a long backlog never stalls the event loop; the SSRC is handed to Hermes' live path only when the
backlog is empty, so the track stays in time order. DAVE audio no candidate's key opens cannot be
decoded at all, stays pending and is reported at the end (:meth:`voice_report`).

Memory: retained payloads are the compressed packets (~100-200 bytes per 20 ms frame, about
``FRAME_OVERHEAD`` more in Python) kept for the WHOLE meeting if needed: ``RETAIN_SECONDS`` (4 h,
the default ``limits_max_duration_minutes``) and ``RETAIN_SSRC_MAX_BYTES`` (256 MiB ≈ 4 h of one
speaker at 64 kbps with overhead) per SSRC, ``RETAIN_MAX_BYTES`` (1 GiB) overall; oldest dropped
first, counted.

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

from ..domain.models import UNIDENTIFIED_PREFIX

log = logging.getLogger(__name__)

Clock = Callable[[], float]
Frames = list[tuple[float, bytes]]
_CLASSES: dict[type, type] = {}
DAVE_MAGIC = b"\xfa\xfa"
_HOW = {"dave": "DAVE key", "speaking": "late SPEAKING", "sole": "only person in the call without a voice"}
FRAME_OVERHEAD = 120  # bytes of Python objects per retained frame (``_Frame`` + its ``bytes``)


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
        return FRAME_OVERHEAD + len(self.raw) + sum(len(o) for o in self.opened.values())


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
    tried: set[int] = field(default_factory=set)  # DAVE keys already tried on the retained frames
    retried_at: float = 0.0
    recent: int = 0  # packets since the last presence snapshot
    dave_frames: int = 0  # packets that carried the DAVE marker (logged when the SSRC is labelled)
    snaps: int = 0  # presence snapshots taken while it sent audio
    heard_by: dict[int, int] = field(default_factory=dict)  # user in the call while it talked -> first snapshot
    absent_at: dict[int, int] = field(default_factory=dict)  # user -> last such snapshot they were absent
    excluded: set[int] = field(default_factory=set)  # absent at two consecutive ones: not the owner
    owner: Optional[int] = None  # proven (key/SPEAKING); the backlog is being replayed into its track
    how: str = ""
    decoder: Any = None
    kept: int = 0

    @property
    def ambiguous(self) -> bool:
        return len(self.hits) > 1

    def snapshot(self, in_call: frozenset[int], joined_at: dict[int, float], lag: float) -> None:
        """A presence snapshot taken while this SSRC sent audio. Its owner is connected whenever it
        sends, so someone out of the call at two consecutive such snapshots cannot own it (two, not
        one: presence lags), nor can a newcomer whose join was seen more than ``lag`` seconds after
        this SSRC's first packet — a connection that did not exist yet sent nothing."""
        i, self.snaps = self.snaps, self.snaps + 1
        for u in in_call - self.heard_by.keys():
            self.heard_by[u] = i
            if joined_at.get(u, float("-inf")) > self.first + lag:
                self.excluded.add(u)
        for u in self.heard_by.keys() - in_call:
            if self.absent_at.get(u) == i - 1:
                self.excluded.add(u)
            self.absent_at[u] = i


@dataclass(frozen=True)
class VoiceReport:
    """What happened to SSRCs SPEAKING never mapped (read at the end of a recording)."""

    identified: dict[int, tuple[int, str]]  # ssrc -> (user id, "dave" | "speaking"): audio in their track
    unidentified: dict[str, int]  # track label -> ssrc (one SSRC = one connection = one person)
    resolved: dict[str, int]  # unidentified label -> user a later SPEAKING named (authoritative)
    undecided: dict[int, int]  # ssrc -> packets that could be neither attributed nor decoded
    inferred: dict[str, tuple[int, str]] = field(default_factory=dict)  # label -> (user, "sole")
    contradicted: dict[int, tuple[int, int]] = field(default_factory=dict)  # ssrc -> (inferred, SPEAKING)

    def owners(self) -> dict[str, int]:
        """Owner of each unidentified track that has one: SPEAKING first, then the inference."""
        out = {label: uid for label, (uid, _how) in self.inferred.items()}
        out.update(self.resolved)
        return out


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
        UNIDENTIFIED_AFTER = 10.0  # decodable audio without SPEAKING/key -> its own "unidentified" track
        JOIN_LAG = 2.0  # a join seen this long after an SSRC's first packet cannot be its owner's
        RETRY_SCAN_FRAMES = 250  # newest retained frames (5 s of audio) a key is tried on: any of them proves it
        RETRY_SECONDS = 5.0  # retained DAVE frames are tried again with every candidate this often
        REPLAY_BATCH = 1500  # retained frames (30 s of audio) replayed per drain
        RETAIN_SECONDS = 4 * 3600.0  # per-SSRC retention window: a whole default-length meeting
        RETAIN_SSRC_MAX_BYTES = 256 * 1024 * 1024  # per SSRC (~4 h at 64 kbps with overhead)
        RETAIN_MAX_BYTES = 1024 * 1024 * 1024  # all SSRCs together

        def __init__(self, voice_client: Any, *, clock: Clock = time.monotonic,
                     codec: Optional[Any] = None) -> None:
            super().__init__(voice_client, allowed_user_ids=None)
            self._clock = clock
            self._codec = codec or HostCodec()
            self._buffers = _Buffers(clock)
            self._decoders = _Decoders(self)
            self._present: Optional[frozenset[int]] = None  # None until the first voice-state snapshot
            # Ordering of joins and mappings (a counter, not the clock): a mapping older than the
            # member's last join belongs to a previous connection (a rejoin gets a new SSRC).
            self._order = itertools.count(1)
            self._joined: dict[int, int] = {}  # user -> order of their last join (voice state, op 11/12)
            self._left: dict[int, int] = {}  # user -> order of their last CLIENT_DISCONNECT (op 13)
            self._joined_at: dict[int, float] = {}  # clock of each join seen (voice state, op 12)
            self._mapped_at: dict[int, int] = {}
            self._pending: dict[int, _Pending] = {}
            self._retained_bytes = 0
            self._unidentified: dict[str, Frames] = {}
            self._label_decoders: dict[str, Any] = {}
            self._identified: dict[int, tuple[int, str]] = {}
            self._labels: dict[str, int] = {}
            self._resolved: dict[str, int] = {}  # unidentified label -> user SPEAKING named later
            self._inferred: dict[str, tuple[int, str]] = {}  # unidentified label -> (user, rule)
            self._contradicted: dict[int, tuple[int, int]] = {}  # ssrc -> (inferred user, SPEAKING user)
            self._voice_clients: set[int] = set()  # users op 11/12 says have media in the call
            self._candidates_grew = False  # a new DAVE key candidate: retry the retained frames

        # -- voice gateway opcodes ----------------------------------------------------------------
        def start(self) -> None:
            """Also follow CLIENTS_CONNECT/CLIENT_CONNECT/CLIENT_DISCONNECT (ops 11/12/13) that the
            scribe's voice client (``voice_client.py``) recorded from the handshake on: op 11 lists
            the ``user_ids`` already in the call, the people Discord often sends no SPEAKING for.
            They carry user ids, never SSRCs: they widen the DAVE key candidates; the SSRC is still
            proven by the key (DESIGN §4.1)."""
            super().start()
            backlog = getattr(self._vc, "voice_ops", None)
            if backlog is None:  # a plain discord.py client: voice states alone name the candidates
                return
            self._vc.voice_op_listener = self.note_voice_op
            for op, data in list(backlog):
                self.note_voice_op(op, data)

        def note_voice_op(self, op: int, data: dict[str, Any]) -> None:
            if op == 11:
                ids = {int(u) for u in data.get("user_ids") or () if str(u).isdigit()}
                log.info("meeting-scribe: voice CLIENTS_CONNECT: %d user(s) already in the call: %s",
                         len(ids), sorted(ids))
                with self._lock:
                    if self._present is not None or self._voice_clients:  # not the handshake's own list
                        for u in ids - self._voice_clients:
                            self._joined[u] = next(self._order)
                            self._joined_at[u] = self._clock()
                    self._candidates_grew |= not ids <= self._voice_clients
                    self._voice_clients |= ids
            elif op in (12, 13) and str(data.get("user_id") or "").isdigit():
                uid = int(data["user_id"])
                with self._lock:
                    if op == 12:  # a NEW connection: a mapping from before it is a previous connection's
                        self._candidates_grew |= uid not in self._voice_clients
                        self._voice_clients.add(uid)
                        self._joined[uid] = next(self._order)
                        self._joined_at[uid] = self._clock()
                    else:  # that connection is gone: its SSRC no longer proves a voice of theirs
                        self._voice_clients.discard(uid)
                        self._left[uid] = next(self._order)

        # -- presence (event-loop thread) -------------------------------------------------------
        def update_presence(self, user_ids: Iterable[int]) -> None:
            """The people in the voice channel right now (voice states). Mute flags are no evidence of
            who owns an SSRC: the voice-state cache lags (a real meeting had a person flagged muted for
            seconds while their audio arrived), so the receiver never sees them."""
            present = frozenset(int(u) for u in user_ids)
            with self._lock:
                if self._present is not None:  # the first snapshot is the baseline, not joins
                    for u in present - self._present:
                        self._joined[u] = next(self._order)
                        self._joined_at[u] = self._clock()
                in_call = present | self._voice_clients
                for p in self._pending.values():
                    if p.owner is None and p.recent:  # it sent audio since the last snapshot
                        p.snapshot(in_call, self._joined_at, self.JOIN_LAG)
                    p.recent = 0
                self._candidates_grew |= not present <= (self._present or frozenset())
                self._present = present

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
            """Whose DAVE key to try: people in the channel, op 11/12 users and the DAVE group, minus
            the bot."""
            ids = set(self._present or ()) | self._voice_clients
            try:
                ids.update(int(u) for u in self._dave_session.get_user_ids())
            except (AttributeError, TypeError, ValueError):  # a session without a group yet
                pass
            ids.discard(int(getattr(self._dave_session, "user_id", 0) or 0))
            return sorted(ids)

        def map_ssrc(self, ssrc: int, user_id: int) -> None:
            """SPEAKING (authoritative): map, and replay audio retained before it arrived. For an SSRC
            already streaming as ``unidentified-N`` the track is named after them; an inferred owner
            it contradicts is dropped with a WARNING (the audio was never in their track)."""
            uid = int(user_id)
            with self._lock:
                p = self._pending.get(ssrc)
                if p is not None and p.label is None and p.owner is None:
                    self._assign(p, uid, "speaking")
                else:
                    if p is not None and p.owner is not None and p.owner != uid:
                        log.warning("meeting-scribe: SPEAKING ssrc=%d -> user %d contradicts its DAVE key "
                                    "(user %d); SPEAKING wins", ssrc, uid, p.owner)
                        p.owner = uid
                        return
                    if p is not None and p.label is not None:
                        guess = self._inferred.pop(p.label, None)
                        if guess is not None and guess[0] != uid:
                            self._contradicted[ssrc] = (guess[0], uid)
                            log.warning("meeting-scribe: SPEAKING ssrc=%d -> user %d contradicts the inference "
                                        "%s = user %d; %s is theirs", ssrc, uid, p.label, guess[0], p.label)
                        self._resolved[p.label] = uid
                        self._identified[ssrc] = (uid, "speaking")
                        self._pending.pop(ssrc)
                        log.info("meeting-scribe: ssrc=%d (%s) named user %d by a late SPEAKING", ssrc, p.label, uid)
                    self._ssrc_to_user[ssrc] = user_id
                    self._mapped_at[ssrc] = next(self._order)
                    return
            self._log_identified(ssrc, p)

        # -- unmapped SSRCs (reader thread, via _Retainer) ---------------------------------------
        def _is_unmapped(self, ssrc: object) -> bool:
            return not self._ssrc_to_user.get(ssrc)  # type: ignore[call-overload]

        def _retain(self, ssrc: int, payload: bytes) -> None:
            now = self._clock()
            dave = self._dave_frame(payload)
            frame = _Frame(now, payload)
            with self._lock:
                p = self._pending.get(ssrc)
                owned = p is not None and p.owner is not None
            candidates = self._key_candidates() if dave and not owned else []
            for u in candidates:  # outside the lock: trial decryption is the slow part
                opus = self._open(u, payload)
                if opus is not None and self._decodes(opus):
                    frame.opened[u] = opus
            with self._lock:
                p = self._pending.get(ssrc)
                if p is None:
                    p = self._pending[ssrc] = _Pending(first=now, last=now, tried=set(candidates))
                    log.info("meeting-scribe: audio from ssrc=%d before SPEAKING; identifying", ssrc)
                p.last = now
                p.packets += 1
                p.recent += 1
                p.dave = p.dave or dave
                p.dave_frames += dave
                if p.owner is not None:  # identified, backlog still replaying: queue behind it
                    self._keep(p, frame)
                    return
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

        def _keep(self, p: _Pending, frame: _Frame) -> None:
            """Caller holds ``_lock``. Bounded: window per SSRC, then a global byte cap."""
            p.frames.append(frame)
            p.size += frame.size
            self._retained_bytes += frame.size
            while p.frames and (frame.t - p.frames[0].t > self.RETAIN_SECONDS
                                or p.size > self.RETAIN_SSRC_MAX_BYTES):
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
                if hits < self.CONFIRM_PACKETS or p.owner is not None:
                    return
                self._assign(p, user_id, "dave")
            self._log_identified(ssrc, p)

        def _retry_keys(self, ssrc: int, p: _Pending, now: float, *, periodic: bool) -> None:
            """Event loop: open the newest ``RETRY_SCAN_FRAMES`` retained DAVE frames with keys not tried
            on them yet (the owner's presence or MLS membership came after they spoke) — and, every
            ``RETRY_SECONDS`` (``periodic``), with every candidate not proven yet, since a key can start
            working without a new candidate (the MLS commit adding its decryptor). Then decide as
            :meth:`_try_keys`. Proof needs only a few frames; the replay opens the rest."""
            candidates = self._key_candidates()
            fresh = [u for u in candidates if u not in p.tried or (periodic and p.hits.get(u, 0) < self.CONFIRM_PACKETS)]
            p.retried_at = now
            if not fresh:
                return
            with self._lock:
                frames = list(p.frames)[-self.RETRY_SCAN_FRAMES:]
                p.tried |= set(fresh)
            for f in reversed(frames):
                for u in fresh:
                    if u in f.opened or p.hits.get(u, 0) >= self.CONFIRM_PACKETS:
                        continue
                    opus = self._open(u, f.raw)
                    if opus is not None and self._decodes(opus):
                        with self._lock:
                            f.opened[u] = opus
                            p.size += len(opus)
                            self._retained_bytes += len(opus)
                            p.hits[u] = p.hits.get(u, 0) + 1
            self._try_keys(ssrc)

        def _owners(self) -> set[int]:
            """Caller holds ``_lock``. Users whose current connection has a PROVEN SSRC (SPEAKING or a
            DAVE key); a mapping older than their last join (voice state, op 11/12) or their last
            CLIENT_DISCONNECT (op 13) belongs to a previous connection and proves nothing now."""
            mapped = {int(u) for s, u in self._ssrc_to_user.items()
                      if u and self._mapped_at.get(s, 0) >= max(self._joined.get(int(u), 0), self._left.get(int(u), 0))}
            return mapped | {q.owner for q in self._pending.values() if q.owner is not None}

        def _candidates(self, p: _Pending, owners: set[int]) -> set[int]:
            """Caller holds ``_lock``. Who may own ``p``: everyone in the call while it sent audio
            (voice states, ops 11/12 — newcomers and other bots included), minus people absent at two
            consecutive snapshots while it talked and people whose voice is already proven."""
            return set(p.heard_by) - p.excluded - owners

        def _infer(self) -> None:
            """Caller holds ``_lock``. The owner of each unidentified track when only one person can
            own it and no other SSRC without owner, active at the same time, can only be theirs
            either (one SSRC is one Discord connection: one person). Recomputed on every drain, so a
            SPEAKING or a proven key elsewhere can name a track later; never writes audio anywhere."""
            owners = self._owners()
            unowned = {s: q for s, q in self._pending.items() if q.owner is None and q.heard_by}
            cands = {s: self._candidates(q, owners) for s, q in unowned.items()}
            for ssrc, q in unowned.items():
                if q.label is None or q.label in self._resolved:
                    continue
                mine = cands[ssrc]
                user = next(iter(mine)) if len(mine) == 1 else None
                rival = user is not None and any(
                    s != ssrc and cands[s] == mine and r.first <= q.last and q.first <= r.last
                    for s, r in unowned.items())
                new = (user, "sole") if user is not None and not rival else None
                old = self._inferred.get(q.label)
                if new == old:
                    continue
                if new is None:
                    self._inferred.pop(q.label, None)
                    log.info("meeting-scribe: %s (ssrc=%d) no longer attributable: %s", q.label, ssrc,
                             self._why(ssrc, mine, rival))
                else:
                    self._inferred[q.label] = new
                    log.info("meeting-scribe: %s (ssrc=%d) is user %d (%s); its track keeps the label "
                             "until the recording closes", q.label, ssrc, user, _HOW["sole"])

        @staticmethod
        def _why(ssrc: int, cands: set[int], rival: bool) -> str:
            if rival:
                return f"another SSRC without owner can only be user {next(iter(cands))} too"
            if not cands:
                return "0 candidates (everyone in the call already has a proven voice)"
            return f"{len(cands)} candidates {sorted(cands)}"

        def _assign(self, p: _Pending, user_id: int, how: str) -> None:
            """Caller holds ``_lock``. The SSRC is ``user_id``'s: its retained frames (from the first
            packet) are replayed by :meth:`drain` at their original times; Hermes' live path takes
            over when the backlog is empty (:meth:`_replay`)."""
            p.owner, p.how = user_id, how
            p.decoder = self._codec.new_decoder()

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

        def _log_identified(self, ssrc: int, p: _Pending) -> None:
            with self._lock:
                self._identified[ssrc] = (int(p.owner), p.how)
            log.info("meeting-scribe: ssrc=%d identified as user %d (%s); replaying %d retained frames "
                     "from %.1f s before (%d dropped by the retention caps)", ssrc, p.owner, _HOW[p.how],
                     len(p.frames), self._clock() - p.first, p.dropped)

        def _replay(self, out: dict[int, Frames], limit: Optional[int]) -> None:
            """Decode up to ``limit`` retained frames of each identified SSRC into ``out`` (``None``: all).
            The SSRC is mapped for Hermes only when its backlog is empty, under the lock, so no live
            frame can overtake a retained one."""
            with self._lock:
                jobs = [(s, p) for s, p in self._pending.items() if p.owner is not None]
            for ssrc, p in jobs:
                with self._lock:
                    n = len(p.frames) if limit is None else min(limit, len(p.frames))
                    batch = [p.frames.popleft() for _ in range(n)]
                    for f in batch:
                        p.size -= f.size
                        self._retained_bytes -= f.size
                    done = not p.frames
                    if done:
                        self._pending.pop(ssrc)
                        self._ssrc_to_user[ssrc] = p.owner
                        self._mapped_at[ssrc] = next(self._order)
                frames = [(f.t, pcm) for f in batch if (pcm := self._pcm(p.decoder, f, p.owner))]
                p.kept += len(frames)
                if frames:
                    out.setdefault(int(p.owner), []).extend(frames)
                if done:
                    log.info("meeting-scribe: ssrc=%d replay done: %d frames of user %d recovered, %d dropped",
                             ssrc, p.kept, p.owner, p.dropped)

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

        def _settle(self, final: bool = False) -> None:
            """Event-loop side of identification: DAVE key retries, the switch of audio without a
            proven owner to its own ``unidentified-N`` track (``final``: whatever is left, however
            short), and the owner inference of those tracks (:meth:`_infer`)."""
            now = self._clock()
            with self._lock:
                grew, self._candidates_grew = self._candidates_grew, False
                waiting = [(s, p) for s, p in self._pending.items() if p.owner is None and p.label is None]
            for ssrc, p in waiting:
                periodic = now - p.retried_at >= self.RETRY_SECONDS
                if p.dave and self._dave_session and (grew or periodic):
                    self._retry_keys(ssrc, p, now, periodic=periodic)
                with self._lock:
                    if (self._pending.get(ssrc) is not p or p.owner is not None
                            or (now - p.first < self.UNIDENTIFIED_AFTER and not final)):
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
                    cands = self._candidates(p, self._owners())
                log.warning("meeting-scribe: ssrc=%d has no SPEAKING and no DAVE key proves its owner; recorded "
                            "as %s (DAVE frames: %d/%d, DAVE session %s; %d candidates %s)", ssrc, label,
                            p.dave_frames, p.packets, "active" if self._dave_session else "none", len(cands),
                            sorted(cands))
                for frame in retained:
                    self._stream_unidentified(label, frame)
            with self._lock:
                self._infer()

        # -- drain (event-loop thread) -----------------------------------------------------------
        def drain(self, *, final: bool = False) -> dict[int, Frames]:
            """Swap out all mapped buffers under the receiver lock; ``{user_id: [(t, pcm), ...]}``, with
            the next batch of replayed retained audio (``final``: all of it — the recording ends)."""
            self._settle(final)
            out: dict[int, Frames] = {}
            self._replay(out, None if final else self.REPLAY_BATCH)
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
                undecided = {s: p.packets for s, p in self._pending.items() if p.label is None and p.owner is None}
                return VoiceReport(dict(self._identified), dict(self._labels), dict(self._resolved), undecided,
                                   dict(self._inferred), dict(self._contradicted))

        def stop(self) -> None:
            if getattr(self._vc, "voice_op_listener", None) == self.note_voice_op:
                self._vc.voice_op_listener = None
            super().stop()
            with self._lock:  # the report (identified/labels/undecided counts) survives the stop
                for p in self._pending.values():
                    p.frames.clear()
                    p.size = 0
                self._retained_bytes = 0

    ScribeReceiver.__qualname__ = ScribeReceiver.__name__ = "ScribeReceiver"
    _CLASSES[base] = ScribeReceiver
    return ScribeReceiver
