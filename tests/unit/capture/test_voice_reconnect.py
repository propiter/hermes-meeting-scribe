"""A voice connection that drops and comes back (DESIGN §4.1): op 13 CLIENT_DISCONNECT ends the
connection an SSRC belonged to, op 12 CLIENT_CONNECT opens a new one. A proven SSRC of the previous
connection no longer counts as that person's own voice."""
from __future__ import annotations

import pytest

from .test_unmapped_voices import A, B, C, Clock, make, send

pytest.importorskip("nacl")


@pytest.fixture
def clock():
    return Clock()


def owners_now(rx):
    with rx._lock:
        return rx._owners()


def test_op13_retires_the_proven_mapping_of_that_user(clock):
    rx = make(clock, present=(A, C))
    rx.map_ssrc(400, A)
    assert owners_now(rx) == {A}
    rx.note_voice_op(13, {"user_id": str(A)})
    assert owners_now(rx) == set()  # the old SSRC is no voice of A's any more
    send(rx, 400, b"OPUS1", seq=1)
    assert rx.drain() == {}  # after disconnect even a reused SSRC is unproven


def test_op12_is_a_new_connection_even_without_a_voice_state_change(clock):
    rx = make(clock, present=(A, C))
    rx.map_ssrc(400, A)
    rx.note_voice_op(12, {"user_id": str(A)})
    assert owners_now(rx) == set() and rx._joined[A] > rx._mapped_at[400]
    rx.map_ssrc(500, A)  # SPEAKING for the new connection
    assert owners_now(rx) == {A}


def test_op11_after_the_first_one_is_a_join(clock):
    rx = make(clock, present=(A, B))
    rx.note_voice_op(11, {"user_ids": [str(A), str(B)]})
    rx.map_ssrc(400, B)
    rx.note_voice_op(11, {"user_ids": [str(C)]})
    assert rx._joined[C] > rx._mapped_at[400] and owners_now(rx) == {B}


def test_fast_rejoin_leaves_the_new_ssrc_to_the_rejoined_person_or_nobody(clock):
    """A proven on 400; C silent. A reconnects (op 13, op 12) between two snapshots with a new SSRC and
    no SPEAKING: A is a candidate again, so C is never named."""
    rx = make(clock, present=(A, C))
    rx.note_voice_op(11, {"user_ids": [str(A), str(C)]})
    rx.map_ssrc(400, A)
    rx.update_presence([A, C])
    rx.drain()
    clock.t += 5
    rx.note_voice_op(13, {"user_id": str(A)})
    rx.note_voice_op(12, {"user_id": str(A)})
    for i in range(1, 750):
        send(rx, 500, b"OPUS%d" % i, seq=i)
        clock.t += 0.02
        if i % 25 == 0:
            rx.update_presence([A, C])
            rx.drain()
    rx.drain(final=True)
    assert rx.voice_report().owners().get("unidentified-1") in (None, A)
