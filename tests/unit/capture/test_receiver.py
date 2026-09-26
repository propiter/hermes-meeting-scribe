"""ScribeReceiver + TimedBuffer against a faithful copy of Hermes' packet path (see fakes.py)."""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from meeting_scribe.capture.receiver import TimedBuffer, scribe_receiver_class

from .fakes import FRAME, FakeConn, FakeVoiceReceiver, build_rtp_packet

pytest.importorskip("nacl")


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


def make(clock, dave=None):
    cls = scribe_receiver_class(FakeVoiceReceiver)
    conn = FakeConn(dave=dave)
    vc = SimpleNamespace(_connection=conn, channel=SimpleNamespace(members=[]), user=SimpleNamespace(id=9999))
    rx = cls(vc, clock=clock)
    rx.start()
    return rx, conn


def feed(rx, ssrc, n=1):
    with patch("nacl.secret.Aead") as aead:
        aead.return_value.decrypt.return_value = b"\xf8\xff\xfe"
        for i in range(n):
            rx._on_packet(build_rtp_packet(ssrc=ssrc, seq=i + 1))


def test_timed_buffer_records_wallclock_per_extend(clock):
    buf = TimedBuffer(clock)
    buf.extend(b"ab")
    clock.t += 0.02
    buf.extend(b"cd")
    assert buf.frames == [(1000.0, b"ab"), (1000.02, b"cd")]
    assert len(buf) == 4


def test_subclass_is_a_hermes_receiver_and_is_cached():
    cls = scribe_receiver_class(FakeVoiceReceiver)
    assert issubclass(cls, FakeVoiceReceiver)
    assert scribe_receiver_class(FakeVoiceReceiver) is cls


def test_captures_every_user_without_allowlist(clock):
    rx, _ = make(clock)
    rx._allowed_user_ids = {"1"}  # Hermes would filter on this; the scribe must ignore it
    rx.map_ssrc(100, 42)
    rx.map_ssrc(200, 43)
    feed(rx, 100, 2)
    clock.t += 1.0
    feed(rx, 200, 1)
    out = rx.drain()
    assert sorted(out) == [42, 43]
    assert [t for t, _ in out[42]] == [1000.0, 1000.0]
    assert out[43] == [(1001.0, FRAME)]


def test_drain_empties_buffers(clock):
    rx, _ = make(clock)
    rx.map_ssrc(100, 42)
    feed(rx, 100)
    assert rx.drain()
    assert rx.drain() == {}


def test_unmapped_ssrc_is_kept_until_speaking_maps_it(clock):
    rx, _ = make(clock)
    feed(rx, 300)
    assert rx.drain() == {}
    rx.map_ssrc(300, 77)
    assert list(rx.drain()) == [77]


def test_unmapped_frames_older_than_limit_are_dropped(clock):
    rx, _ = make(clock)
    feed(rx, 300)
    clock.t += rx.UNMAPPED_MAX_AGE + 1
    rx.drain()
    rx.map_ssrc(300, 77)
    assert rx.drain() == {}


def test_bot_own_ssrc_is_ignored(clock):
    rx, _ = make(clock)
    feed(rx, 9999)
    assert rx.drain() == {} and dict(rx._buffers) == {}


def test_refresh_connection_picks_up_new_dave_session_and_key(clock):
    rx, conn = make(clock)
    assert rx._dave_session is None
    conn.dave_session = MagicMock()
    conn.dave_session.decrypt.return_value = b"opus"
    conn.secret_key = [1] * 32
    rx.refresh_connection()
    assert rx._dave_session is conn.dave_session
    assert rx._secret_key == bytes([1] * 32)
    rx.map_ssrc(100, 42)
    feed(rx, 100)
    conn.dave_session.decrypt.assert_called_once()


def test_never_calls_check_silence(clock):
    rx, _ = make(clock)
    rx.map_ssrc(100, 42)
    feed(rx, 100)
    rx.drain()  # FakeVoiceReceiver.check_silence raises if called


def test_drain_is_thread_safe(clock):
    rx, _ = make(clock)
    rx.map_ssrc(100, 42)
    total = []
    stop = threading.Event()

    def producer():
        for _ in range(300):
            feed(rx, 100)
        stop.set()

    th = threading.Thread(target=producer)
    th.start()
    while not stop.is_set():
        total += rx.drain().get(42, [])
    th.join()
    total += rx.drain().get(42, [])
    assert len(total) == 300
