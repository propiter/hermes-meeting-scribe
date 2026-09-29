"""Only a gap-free handshake observer permits closing-time elimination."""
from types import SimpleNamespace
from collections import deque

from meeting_scribe.capture.receiver import scribe_receiver_class
from .fakes import FakeVoiceReceiver, FakeCodec, FakeConn
from .test_unmapped_voices import A, B, C, Clock, send


def receiver(clock, complete=True):
    vc = SimpleNamespace(_connection=FakeConn(), voice_membership_complete=complete,
                         voice_ops=deque([(11, {"user_ids": [str(A), str(B)]})]))
    rx = scribe_receiver_class(FakeVoiceReceiver)(vc, clock=clock, codec=FakeCodec())
    rx.decoder_factory = FakeCodec
    rx.start()
    rx.update_presence([A, B])
    return rx


def audio(rx, clock, count, present=(A, B)):
    for i in range(count):
        send(rx, 500, b"OPUS", seq=i + 1)
        clock.t += .02
        if i % 25 == 24:
            rx.update_presence(present)
            rx.drain()


def test_real_initial_list_b_speaking_at_2_7_seconds_a_never_speaks_event():
    clock = Clock()
    rx = receiver(clock)
    audio(rx, clock, 135)
    rx.map_ssrc(400, B)
    audio(rx, clock, 600)
    assert rx.voice_report().owners() == {}  # not before closing
    rx.drain(final=True)
    assert rx.voice_report().owners() == {"unidentified-1": A}


def test_late_snapshot_or_gap_prevents_auto_assignment():
    for problem in ("late", "gap", "reconnect"):
        clock = Clock()
        rx = receiver(clock)
        rx.map_ssrc(400, B)
        audio(rx, clock, 150)
        if problem == "late":
            rx.update_presence([A, B, C])
        elif problem == "gap":
            rx._vc.voice_membership_complete = False
        else:
            rx.note_voice_op(13, {"user_id": str(B)})
            rx.note_voice_op(12, {"user_id": str(B)})
            rx.map_ssrc(401, B)
        audio(rx, clock, 600)
        rx.drain(final=True)
        assert rx.voice_report().owners() == {}


def test_plain_receiver_with_identical_list_is_only_a_suggestion():
    clock = Clock()
    rx = receiver(clock, complete=False)
    rx.map_ssrc(400, B)
    audio(rx, clock, 750)
    rx.drain(final=True)
    assert rx.voice_report().inferred == {"unidentified-1": (A, "sole")}
    assert rx.voice_report().owners() == {}
