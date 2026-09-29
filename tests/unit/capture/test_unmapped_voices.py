"""Unmapped SSRCs: Discord did not send SPEAKING for a person (DESIGN §4.1).

Packets go through the faithful copy of Hermes' packet path (fakes.py): NaCl is mocked to return
the payload under test, then Hermes skips DAVE for the unmapped SSRC and asks ``_decoders`` for a
decoder — which is where the scribe keeps the payload instead of decoding it as noise."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from meeting_scribe.capture.receiver import scribe_receiver_class

from .fakes import FakeCodec, FakeConn, FakeDave, FakeVoiceReceiver, build_rtp_packet, dave_frame

pytest.importorskip("nacl")

A, B, C, D = 101, 102, 103, 104


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


def make(clock, dave=None, present=(A, B)):
    cls = scribe_receiver_class(FakeVoiceReceiver)
    conn = FakeConn(dave=dave)
    vc = SimpleNamespace(_connection=conn, channel=SimpleNamespace(members=[]), user=SimpleNamespace(id=9999))
    rx = cls(vc, clock=clock, codec=FakeCodec())
    rx.decoder_factory = FakeCodec  # Hermes' own decoder for mapped SSRCs
    rx.start()
    rx.update_presence(present)
    return rx


def send(rx, ssrc, payload, seq=1):
    with patch("nacl.secret.Aead") as aead:
        aead.return_value.decrypt.return_value = payload
        rx._on_packet(build_rtp_packet(ssrc=ssrc, seq=seq))


def talk_dave(rx, clock, ssrc, owner, n, start=1, step=0.02):
    for i in range(start, start + n):
        send(rx, ssrc, dave_frame(owner, i), seq=i)
        clock.t += step


def talk_plain(rx, clock, ssrc, n, start=1, step=0.02):
    for i in range(start, start + n):
        send(rx, ssrc, b"OPUS" + str(i).encode(), seq=i)
        clock.t += step


def payloads(frames):
    return [pcm[-8:].split(b"OPUS")[-1] for _, pcm in frames]


# -- DAVE -----------------------------------------------------------------------------------------
def test_dave_speakers_already_present_are_identified_by_their_key(clock):
    rx = make(clock, dave=FakeDave([A, B]))
    t_first = clock.t
    talk_dave(rx, clock, 500, A, 3)
    talk_dave(rx, clock, 600, B, 3)
    out = rx.drain()
    assert sorted(out) == [A, B]
    assert payloads(out[A]) == [b"1", b"2", b"3"]  # every frame before identification kept
    assert out[A][0][0] == t_first  # at its ORIGINAL arrival time
    rep = rx.voice_report()
    assert rep.identified == {500: (A, "dave"), 600: (B, "dave")} and not rep.undecided


def test_dave_after_identification_hermes_decrypts_live(clock):
    dave = FakeDave([A, B])
    rx = make(clock, dave=dave)
    talk_dave(rx, clock, 500, A, 3)
    rx.drain()
    dave.calls.clear()
    talk_dave(rx, clock, 500, A, 2, start=4)
    assert dave.calls == [A, A]  # Hermes' own path, one decrypt per packet, no more trial keys
    assert payloads(rx.drain()[A]) == [b"4", b"5"]


def test_dave_needs_several_consistent_frames(clock):
    rx = make(clock, dave=FakeDave([A, B]))
    talk_dave(rx, clock, 500, A, rx.CONFIRM_PACKETS - 1)
    assert rx.drain() == {}
    assert rx.voice_report().undecided == {500: rx.CONFIRM_PACKETS - 1}


def test_dave_frame_no_candidate_key_opens_is_never_attributed(clock):
    rx = make(clock, dave=FakeDave([A, B]))
    talk_dave(rx, clock, 700, C, 20)  # C is not in the channel/group we know
    clock.t += rx.UNIDENTIFIED_AFTER + 1
    assert rx.drain() == {}
    assert rx.drain_unidentified() == {}  # it cannot be decoded: never a noise track
    assert rx.voice_report().undecided == {700: 20}


def test_dave_two_keys_opening_the_same_ssrc_is_ambiguous_and_never_attributed(clock):
    rx = make(clock, dave=FakeDave([A, B], shared={A: B}))  # impossible with real DAVE keys
    talk_dave(rx, clock, 500, A, 5)
    clock.t += rx.UNIDENTIFIED_AFTER + 1
    assert rx.drain() == {}
    un = rx.drain_unidentified()
    assert list(un) == ["unidentified-1"] and payloads(un["unidentified-1"]) == [b"1", b"2", b"3", b"4", b"5"]
    assert rx.voice_report().unidentified == {"unidentified-1": 500}


def test_dave_candidates_include_the_group_even_before_presence(clock):
    rx = make(clock, dave=FakeDave([A, C]), present=(A,))
    talk_dave(rx, clock, 700, C, 3)
    assert list(rx.drain()) == [C]


def test_clients_connect_user_ids_become_key_candidates(clock):
    """The production pattern: people already in the call when the bot connects get no SPEAKING;
    op 11 (recorded by the scribe's voice client during the handshake) lists them, and their DAVE
    key proves which SSRC is theirs. Ops after start() arrive through the listener."""
    from collections import deque

    dave = FakeDave([C, D])
    dave.get_user_ids = lambda: []  # no group info, and nobody in the member list yet
    cls = scribe_receiver_class(FakeVoiceReceiver)
    vc = SimpleNamespace(_connection=FakeConn(dave=dave), channel=SimpleNamespace(members=[]),
                         user=SimpleNamespace(id=9999), voice_op_listener=None,
                         voice_ops=deque([(11, {"user_ids": [str(C)]})]))
    rx = cls(vc, clock=clock, codec=FakeCodec())
    rx.decoder_factory = FakeCodec
    rx.start()
    assert vc.voice_op_listener == rx.note_voice_op
    talk_dave(rx, clock, 700, C, 3)
    assert list(rx.drain()) == [C] and rx.voice_report().identified[700] == (C, "dave")
    vc.voice_op_listener(12, {"user_id": str(D)})
    talk_dave(rx, clock, 701, D, 3)
    assert list(rx.drain()) == [D]
    vc.voice_op_listener(13, {"user_id": str(C)})
    assert C not in rx._voice_clients
    rx.stop()
    assert vc.voice_op_listener is None


def test_late_speaking_replays_retained_dave_audio(clock):
    rx = make(clock, dave=FakeDave([C]), present=(A,))  # C's key not tried yet: no group info...
    rx._dave_session.members = [C]
    rx._dave_session.get_user_ids = lambda: []  # ...and not in the channel list either
    t0 = clock.t
    talk_dave(rx, clock, 700, C, 4)
    assert rx.drain() == {}
    rx.map_ssrc(700, C)  # SPEAKING finally arrives
    out = rx.drain()
    assert payloads(out[C]) == [b"1", b"2", b"3", b"4"] and out[C][0][0] == t0
    assert rx.voice_report().identified == {700: (C, "speaking")}


# -- no DAVE ----------------------------------------------------------------------------------------
# Plain Opus proves nothing: the SSRC always gets its own ``unidentified-N`` track and, when only one
# person can own it, an inferred owner the session applies at the close (test_voice_identity.py has
# the real-meeting sequence).
def talk_and_tick(rx, clock, ssrc, n, step=0.2, present=None):
    for i in range(1, n + 1):
        send(rx, ssrc, b"OPUS" + str(i).encode(), seq=i)
        clock.t += step
        if i % 3 == 0:
            rx.update_presence(present if present is not None else rx._present)
            rx.drain()


def test_plain_sole_candidate_is_inferred_never_written_to_their_track(clock):
    rx = make(clock, present=(A, B))
    rx.map_ssrc(400, A)  # A has spoken already (SPEAKING arrived)
    t0 = clock.t
    talk_and_tick(rx, clock, 500, 60)
    un = rx.drain_unidentified()
    assert list(un) == ["unidentified-1"] and un["unidentified-1"][0][0] == t0 and len(un["unidentified-1"]) == 60
    rep = rx.voice_report()
    assert rep.inferred == {"unidentified-1": (B, "sole")} and B not in rep.identified


def test_plain_two_candidates_become_an_unidentified_track_never_a_guess(clock):
    rx = make(clock, present=(A, B))
    talk_and_tick(rx, clock, 500, 60)
    assert rx.drain() == {}
    un = rx.drain_unidentified()
    assert list(un) == ["unidentified-1"] and len(un["unidentified-1"]) == 60
    assert rx.voice_report().inferred == {}
    talk_plain(rx, clock, 500, 2, start=61)
    assert len(rx.drain_unidentified()["unidentified-1"]) == 2  # keeps streaming there


def test_plain_two_unmapped_ssrcs_talking_are_not_assigned_by_elimination(clock):
    rx = make(clock, present=(A, B, C))
    rx.map_ssrc(400, A)
    for i in range(1, 60):  # B and C talk at once; 2 candidates each: nobody is named
        send(rx, 500, b"OPUS%d" % i, seq=i)
        send(rx, 600, b"OPUS%d" % i, seq=i)
        clock.t += 0.2
        if i % 3 == 0:
            rx.update_presence([A, B, C])
            rx.drain()
    assert rx.drain() == {} and rx.voice_report().inferred == {}


def test_plain_a_rejoined_member_is_a_candidate_again(clock):
    rx = make(clock, present=(A, B))
    rx.map_ssrc(400, A)
    rx.map_ssrc(401, B)
    rx.update_presence([A])  # B leaves...
    clock.t += 1
    rx.update_presence([A, B])  # ...and rejoins with a new SSRC and no SPEAKING
    talk_and_tick(rx, clock, 502, 60)
    assert rx.voice_report().inferred == {"unidentified-1": (B, "sole")}


def test_speaking_before_the_first_presence_snapshot_still_counts(clock):
    cls = scribe_receiver_class(FakeVoiceReceiver)
    vc = SimpleNamespace(_connection=FakeConn(), channel=SimpleNamespace(members=[]), user=SimpleNamespace(id=9999))
    rx = cls(vc, clock=clock, codec=FakeCodec())
    rx.decoder_factory = FakeCodec
    rx.start()
    rx.map_ssrc(400, A)  # SPEAKING lands before the session's first tick
    rx.update_presence([A, B])
    talk_and_tick(rx, clock, 500, 60)
    assert rx.voice_report().inferred == {"unidentified-1": (B, "sole")}  # A already has an SSRC


def test_plain_other_bot_in_channel_blocks_the_inference(clock):
    rx = make(clock, present=(A, B, 8888))  # a music bot is a possible owner of the SSRC too
    rx.map_ssrc(400, A)
    talk_and_tick(rx, clock, 500, 60)
    assert rx.drain() == {} and rx.voice_report().inferred == {}


def test_speaking_after_unidentified_names_the_label(clock):
    rx = make(clock, present=(A, B))
    talk_and_tick(rx, clock, 500, 60)
    rx.drain_unidentified()
    rx.map_ssrc(500, B)
    assert rx.voice_report().resolved == {"unidentified-1": B}
    talk_plain(rx, clock, 500, 2, start=61)
    assert list(rx.drain()) == [B]


# -- memory ---------------------------------------------------------------------------------------
def test_default_retention_keeps_a_whole_meeting_of_one_speaker():
    cls = scribe_receiver_class(FakeVoiceReceiver)
    assert cls.RETAIN_SECONDS >= 4 * 3600  # the default limits_max_duration_minutes
    # 4 h of 20 ms frames at 64 kbps (160 B) plus Python overhead fit the per-SSRC cap
    assert 4 * 3600 * 50 * (160 + 120) <= cls.RETAIN_SSRC_MAX_BYTES <= cls.RETAIN_MAX_BYTES


def test_an_hour_identified_late_is_recovered_from_the_first_packet(clock):
    """Nobody is a key candidate for an hour (no presence, no group, no op 11); when the owner's key
    finally is, the retained audio from the FIRST packet goes to their track, in time order and
    ahead of the live audio."""
    dave = FakeDave([C])
    dave.get_user_ids = lambda: []
    rx = make(clock, dave=dave, present=(A,))
    t0 = clock.t
    talk_dave(rx, clock, 700, C, 3600, step=1.0)  # one packet a second for an hour
    assert rx.drain() == {} and rx.voice_report().undecided == {700: 3600}
    rx.update_presence([A, C])  # C shows up in the member list: a new candidate, retried at once
    out = rx.drain()
    talk_dave(rx, clock, 700, C, 2, start=3601)  # live audio while the backlog replays
    while rx._pending:
        for uid, frames in rx.drain().items():
            out.setdefault(uid, []).extend(frames)
    got = out[C]
    assert len(got) == 3602 and got[0][0] == t0
    assert [t for t, _ in got] == sorted(t for t, _ in got)
    assert payloads(got)[:2] == [b"1", b"2"] and payloads(got)[-1] == b"3602"
    assert rx.voice_report().identified == {700: (C, "dave")}


def test_retained_dave_frames_are_retried_periodically_when_a_key_appears(clock):
    dave = FakeDave([A])  # C is in the channel, but its decryptor does not exist yet
    rx = make(clock, dave=dave, present=(A, C))
    talk_dave(rx, clock, 700, C, 5)
    assert rx.drain() == {}
    dave.members.append(C)  # the MLS commit adding C's key lands; no presence change
    clock.t += rx.RETRY_SECONDS
    assert payloads(rx.drain()[C]) == [b"1", b"2", b"3", b"4", b"5"]


def test_voice_states_alone_name_dave_candidates_without_op_11(clock):
    dave = FakeDave([A, B, C])
    dave.get_user_ids = lambda: []  # no MLS group info either
    rx = make(clock, dave=dave, present=(A, B, C))
    for owner, ssrc in ((A, 500), (B, 600), (C, 700)):
        talk_dave(rx, clock, ssrc, owner, 3)
    assert sorted(rx.drain()) == [A, B, C]


def test_retention_is_bounded_per_ssrc_and_globally(clock):
    rx = make(clock, present=(A, B))
    rx.RETAIN_SECONDS = 1.0
    rx.UNIDENTIFIED_AFTER = 1e9
    talk_plain(rx, clock, 500, 200, step=0.02)  # 4 s
    p = rx._pending[500]
    assert len(p.frames) <= 51 and p.dropped >= 149
    rx.RETAIN_MAX_BYTES = 10_000
    talk_plain(rx, clock, 600, 200)
    assert rx._retained_bytes <= 10_000
    assert rx._retained_bytes == sum(q.size for q in rx._pending.values())


def test_stop_releases_retained_audio_but_keeps_the_report(clock):
    rx = make(clock, dave=FakeDave([A, B]))
    talk_dave(rx, clock, 700, C, 5)
    rx.stop()
    assert rx._retained_bytes == 0 and not rx._pending[700].frames
    assert rx.voice_report().undecided == {700: 5}
