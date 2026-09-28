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

A, B, C = 101, 102, 103


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


def make(clock, dave=None, present=(A, B), muted=()):
    cls = scribe_receiver_class(FakeVoiceReceiver)
    conn = FakeConn(dave=dave)
    vc = SimpleNamespace(_connection=conn, channel=SimpleNamespace(members=[]), user=SimpleNamespace(id=9999))
    rx = cls(vc, clock=clock, codec=FakeCodec())
    rx.decoder_factory = FakeCodec  # Hermes' own decoder for mapped SSRCs
    rx.start()
    rx.update_presence(present, muted)
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
def test_plain_sole_candidate_is_mapped_after_the_grace(clock):
    rx = make(clock, present=(A, B))
    rx.map_ssrc(400, A)  # A has spoken already (SPEAKING arrived)
    t0 = clock.t
    talk_plain(rx, clock, 500, 10, step=0.25)  # 2.5 s: past IDENTIFY_GRACE
    out = rx.drain()
    assert list(out) == [B] and out[B][0][0] == t0 and len(out[B]) == 10
    assert rx.voice_report().identified == {500: (B, "sole")}


def test_plain_two_candidates_become_an_unidentified_track_never_a_guess(clock):
    rx = make(clock, present=(A, B))
    talk_plain(rx, clock, 500, 60, step=0.2)  # 12 s, nobody mapped: A or B?
    assert rx.drain() == {}
    un = rx.drain_unidentified()
    assert list(un) == ["unidentified-1"] and len(un["unidentified-1"]) == 60
    talk_plain(rx, clock, 500, 2, start=61)
    assert len(rx.drain_unidentified()["unidentified-1"]) == 2  # keeps streaming there


def test_plain_two_unmapped_ssrcs_talking_are_not_assigned_by_elimination(clock):
    rx = make(clock, present=(A, B, C))
    rx.map_ssrc(400, A)
    for i in range(1, 16):  # B and C talk at once; 2 candidates, 2 SSRCs: no sole rule
        send(rx, 500, b"OPUS%d" % i, seq=i)
        send(rx, 600, b"OPUS%d" % i, seq=i)
        clock.t += 0.2
    assert rx.drain() == {}


def test_plain_muted_members_are_not_candidates(clock):
    rx = make(clock, present=(A, B), muted=(A,))
    talk_plain(rx, clock, 500, 12, step=0.25)
    assert list(rx.drain()) == [B]


def test_plain_a_rejoined_member_is_a_candidate_again(clock):
    rx = make(clock, present=(A, B))
    rx.map_ssrc(400, A)
    rx.map_ssrc(401, B)
    rx.update_presence([A])  # B leaves...
    clock.t += 1
    rx.update_presence([A, B])  # ...and rejoins with a new SSRC and no SPEAKING
    talk_plain(rx, clock, 502, 12, step=0.25)
    assert list(rx.drain()) == [B]


def test_speaking_before_the_first_presence_snapshot_still_counts(clock):
    cls = scribe_receiver_class(FakeVoiceReceiver)
    vc = SimpleNamespace(_connection=FakeConn(), channel=SimpleNamespace(members=[]), user=SimpleNamespace(id=9999))
    rx = cls(vc, clock=clock, codec=FakeCodec())
    rx.decoder_factory = FakeCodec
    rx.start()
    rx.map_ssrc(400, A)  # SPEAKING lands before the session's first tick
    rx.update_presence([A, B])
    talk_plain(rx, clock, 500, 12, step=0.25)
    assert list(rx.drain()) == [B]  # A already has an SSRC: B is the only candidate


def test_plain_other_bot_in_channel_blocks_the_sole_rule(clock):
    rx = make(clock, present=(A, B, 8888))  # a music bot is a possible owner of the SSRC too
    rx.map_ssrc(400, A)
    talk_plain(rx, clock, 500, 12, step=0.25)
    assert rx.drain() == {}


def test_sole_decision_also_happens_after_the_speaker_stops(clock):
    rx = make(clock, present=(A,))
    talk_plain(rx, clock, 500, 5)  # 0.1 s of audio, then silence
    assert rx.drain() == {}
    clock.t += rx.IDENTIFY_GRACE
    assert list(rx.drain()) == [A]


def test_speaking_after_unidentified_names_the_label(clock):
    rx = make(clock, present=(A, B))
    talk_plain(rx, clock, 500, 60, step=0.2)
    rx.drain()
    rx.drain_unidentified()
    rx.map_ssrc(500, B)
    assert rx.voice_report().resolved == {"unidentified-1": B}
    talk_plain(rx, clock, 500, 2, start=61)
    assert list(rx.drain()) == [B]


# -- memory ---------------------------------------------------------------------------------------
def test_retention_is_bounded_per_ssrc_and_globally(clock):
    rx = make(clock, present=(A, B))
    rx.RETAIN_SECONDS = 1.0
    rx.UNIDENTIFIED_AFTER = 1e9
    talk_plain(rx, clock, 500, 200, step=0.02)  # 4 s
    p = rx._pending[500]
    assert len(p.frames) <= 51 and p.dropped >= 149
    rx.RETAIN_MAX_BYTES = 50
    talk_plain(rx, clock, 600, 20)
    assert rx._retained_bytes <= 50
    assert rx._retained_bytes == sum(q.size for q in rx._pending.values())


def test_stop_releases_retained_audio_but_keeps_the_report(clock):
    rx = make(clock, dave=FakeDave([A, B]))
    talk_dave(rx, clock, 700, C, 5)
    rx.stop()
    assert rx._retained_bytes == 0 and not rx._pending[700].frames
    assert rx.voice_report().undecided == {700: 5}
