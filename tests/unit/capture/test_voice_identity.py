"""Regressions from a real meeting (DESIGN §4.1): a voice given to the wrong person, and a voice left
unidentified although only one person could own it.

The sequence, with invented ids and the real timings: the bot joins a call where Ana and Beto already
are; op 11 lists both. Ana's SSRC sends audio before any SPEAKING, while the voice-state cache still
flags her muted. Beto's SPEAKING arrives 2.7 s in. 53 s in Carla joins (voice state, then op 11);
her SSRC sends a little audio 2.8 s after the join, and SPEAKING names it 8.4 s later.

Before the fix: Ana's SSRC became ``unidentified-1`` with nobody behind it — the stale mute flag left
no "unmuted person without SSRC" — and Carla's packets went to Ana's track, "the only person without
SSRC" once Ana's flag caught up and Carla's own join was still flagged muted.
"""
from __future__ import annotations

import logging

import pytest

from meeting_scribe.capture.session import RecordingSession

from .fakes import FakeCodec, FakeMember
from .test_session import FakeWriter, world  # noqa: F401  (fixture)
from .test_unmapped_voices import A, B, C, D, Clock, FakeDave, dave_frame, make, payloads, send

pytest.importorskip("nacl")

ANA, BETO, CARLA = A, B, C
S_ANA, S_BETO, S_CARLA = 42813, 42827, 42834


@pytest.fixture
def clock():
    return Clock()


class Call:
    """Drives the receiver like the session does: one presence snapshot + drain every 0.5 s."""

    def __init__(self, clock, dave=None):
        self.clock = clock
        self.present = [ANA, BETO]
        self.rx = make(clock, dave=dave, present=self.present)
        self.rx.note_voice_op(11, {"user_ids": [str(ANA), str(BETO)]})
        self.out: dict = {}
        self.unidentified: dict = {}
        self.seq = 0
        self.dave = dave

    def tick(self, seconds, talkers=()):
        """``seconds`` of time; each ``(ssrc, owner, every_n_frames)`` in ``talkers`` sends packets."""
        for i in range(int(round(seconds / 0.02))):
            for ssrc, owner, every in talkers:
                if i % every == 0:
                    self.seq += 1
                    body = dave_frame(owner, self.seq) if self.dave else b"OPUS%d-%d" % (owner, self.seq)
                    send(self.rx, ssrc, body, seq=self.seq)
            self.clock.t += 0.02
            if i % 25 == 24:
                self.drain()

    def drain(self, final=False):
        self.rx.update_presence(self.present)
        for uid, frames in self.rx.drain(final=final).items():
            self.out.setdefault(uid, []).extend(frames)
        for label, frames in self.rx.drain_unidentified().items():
            self.unidentified.setdefault(label, []).extend(frames)

    def audio_of(self, key) -> bytes:
        return b"".join(pcm for _, pcm in (self.out if isinstance(key, int) else self.unidentified).get(key, ()))


def real_meeting(call: Call, *, carla_speaking: bool = True):
    ana = (S_ANA, ANA, 1)
    call.tick(2.7, [ana])                           # 08:02:02.3: Ana talks, no SPEAKING
    call.rx.map_ssrc(S_BETO, BETO)                  # 08:02:05.0: SPEAKING for Beto
    call.tick(5.0, [ana, (S_BETO, BETO, 1)])
    call.tick(45.4, [ana])                          # Ana goes on talking
    call.present = [ANA, BETO, CARLA]               # 08:02:55.7: Carla joins
    call.tick(1.6, [ana])
    call.rx.note_voice_op(11, {"user_ids": [str(CARLA)]})  # 08:02:57.4: op 11 for Carla
    call.tick(1.1, [ana])
    call.tick(8.4, [ana, (S_CARLA, CARLA, 10)])     # 08:02:58.5: Carla's SSRC sends a little audio
    if carla_speaking:
        call.rx.map_ssrc(S_CARLA, CARLA)            # 08:03:06.9: SPEAKING names it
    call.tick(5.0, [ana, (S_CARLA, CARLA, 1)])
    call.drain(final=True)
    return call.rx.voice_report()


def test_real_meeting_never_gives_a_voice_to_the_wrong_person_and_names_the_unidentified_one(clock, caplog):
    call = Call(clock)
    with caplog.at_level(logging.INFO, logger="meeting_scribe.capture.receiver"):
        rep = real_meeting(call)
    # B1: none of Carla's packets reached Ana; Carla's SPEAKING gave her all of hers, from the first
    assert b"OPUS%d-" % CARLA not in call.audio_of(ANA) + call.audio_of("unidentified-1")
    assert rep.identified[S_CARLA] == (CARLA, "speaking")
    assert call.out[CARLA][0][0] == pytest.approx(1000.0 + 55.8, abs=0.05)  # its first packet
    assert b"OPUS%d-" % ANA not in call.audio_of(CARLA)
    # B2: Ana's voice is kept whole in its own track and inferred to be hers — the only person in the
    # call without a voice once Beto's was known (Carla joined long after it started talking)
    assert rep.unidentified == {"unidentified-1": S_ANA}
    assert rep.inferred == {"unidentified-1": (ANA, "sole")} and rep.owners() == {"unidentified-1": ANA}
    assert call.unidentified["unidentified-1"][0][0] == pytest.approx(1000.0)
    assert ANA not in call.out  # never mixed into a person's track by a guess
    # the label line says whether the frames were DAVE, how many candidates there were and who
    line = next(r.getMessage() for r in caplog.records if "recorded as unidentified-1" in r.getMessage())
    assert "DAVE frames: 0/" in line and "DAVE session none" in line and f"1 candidates [{ANA}]" in line


def test_real_meeting_without_carlas_speaking_leaves_her_voice_unattributed(clock):
    """Without SPEAKING, Carla's SSRC could be Carla's or Ana's (Ana's own voice is not proven): it
    gets its own label and no owner — never Ana, never a guess."""
    call = Call(clock)
    rep = real_meeting(call, carla_speaking=False)
    assert rep.unidentified == {"unidentified-1": S_ANA, "unidentified-2": S_CARLA}
    assert rep.inferred == {"unidentified-1": (ANA, "sole")}
    assert ANA not in call.out and CARLA not in call.out


def test_real_meeting_with_dave_passthrough_frames_follows_the_same_rules(clock):
    """A DAVE session is active but the frames carry no DAVE marker (passthrough)."""
    dave = FakeDave([ANA, BETO, CARLA])
    call = Call(clock)
    call.rx._dave_session = call.rx._vc._connection.dave_session = dave
    rep = real_meeting(call)
    assert rep.inferred == {"unidentified-1": (ANA, "sole")}
    assert rep.identified[S_CARLA] == (CARLA, "speaking")


def test_real_meeting_with_dave_frames_is_proven_by_key(clock):
    call = Call(clock, dave=FakeDave([ANA, BETO, CARLA]))
    rep = real_meeting(call)
    assert rep.identified[S_ANA] == (ANA, "dave") and not rep.unidentified
    assert payloads(call.out[ANA])[:1] and call.out[ANA][0][0] == pytest.approx(1000.0)


def test_a_speaking_contradicting_an_inference_names_the_track_and_warns(clock, caplog):
    rx = make(clock, present=(A, B))
    rx.map_ssrc(400, B)
    for i in range(1, 60):
        send(rx, 500, b"OPUS%d" % i, seq=i)
        clock.t += 0.2
        if i % 3 == 0:
            rx.update_presence([A, B])
            rx.drain()
    assert rx.voice_report().inferred == {"unidentified-1": (A, "sole")}
    with caplog.at_level(logging.WARNING, logger="meeting_scribe.capture.receiver"):
        rx.map_ssrc(500, D)  # SPEAKING: it was D (someone the voice states missed)
    rep = rx.voice_report()
    assert rep.inferred == {} and rep.resolved == {"unidentified-1": D} and rep.owners() == {"unidentified-1": D}
    assert rep.contradicted == {500: (A, D)} and "contradicts" in caplog.text
    send(rx, 500, b"OPUS99", seq=99)
    assert list(rx.drain()) == [D]  # live audio from now on goes to D's own track


def test_two_ssrcs_for_the_same_only_candidate_are_both_left_unnamed(clock):
    rx = make(clock, present=(A, B))
    rx.map_ssrc(400, B)
    for i in range(1, 60):  # two connections, one possible owner: one of them is someone we cannot see
        send(rx, 500, b"OPUS%d" % i, seq=i)
        send(rx, 600, b"OPUS%d" % i, seq=i)
        clock.t += 0.2
        if i % 3 == 0:
            rx.update_presence([A, B])
            rx.drain()
    assert rx.voice_report().inferred == {}


def test_the_inference_follows_the_call(clock):
    """Two people without voice: nobody is named. Once B's SPEAKING arrives, the track is A's."""
    rx = make(clock, present=(A, B))
    for i in range(1, 60):
        send(rx, 500, b"OPUS%d" % i, seq=i)
        clock.t += 0.2
        if i % 3 == 0:
            rx.update_presence([A, B])
            rx.drain()
    assert list(rx.drain_unidentified()) == ["unidentified-1"] and rx.voice_report().inferred == {}
    rx.map_ssrc(600, B)
    rx.drain()
    assert rx.voice_report().inferred == {"unidentified-1": (A, "sole")}


def test_someone_absent_at_two_snapshots_while_it_talked_is_not_its_owner(clock):
    rx = make(clock, present=(A, B))
    for i in range(1, 60):
        send(rx, 500, b"OPUS%d" % i, seq=i)
        clock.t += 0.2
        if i % 3 == 0:
            rx.update_presence([A] if 10 < i < 30 else [A, B])  # B stepped out while it talked
            rx.drain()
    assert rx.voice_report().inferred == {"unidentified-1": (A, "sole")}


def test_a_short_voice_is_labelled_at_the_close(clock):
    rx = make(clock, present=(A, B))
    rx.map_ssrc(400, B)
    for i in range(1, 5):
        send(rx, 500, b"OPUS%d" % i, seq=i)
        clock.t += 0.02
    rx.update_presence([A, B])
    rx.drain(final=True)
    un = rx.drain_unidentified()
    assert len(un["unidentified-1"]) == 4
    assert rx.voice_report().owners() == {"unidentified-1": A}


# -- the same sequence through the recording session (red on the version that failed) --------------
class TouchWriter(FakeWriter):
    def __init__(self, path, t0):
        super().__init__(path, t0)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ogg")


async def test_session_real_meeting_with_the_voice_state_mute_flags(world):
    """The session hands the receiver what discord.py's voice-state cache says, mute flags included.
    Ana is flagged muted while her audio arrives; Carla joins flagged muted and her client sends a
    little audio. Her packets must never reach Ana's track, and Ana's voice ends up as Ana's."""
    import asyncio
    from dataclasses import replace

    from .test_session import run_ticks

    guild, voice, clock = world["guild"], world["voice"], world["clock"]
    ana, luis = world["ana"], guild.members[43]  # Luis plays Beto
    voice.members = [ana, luis]  # no other bot in this call
    ana.voice.self_mute = True  # the stale flag
    deps = replace(world["deps"](), writer_factory=lambda path, t0: TouchWriter(path, t0))
    s = RecordingSession(world["adapter"], voice, deps, started_by="42")
    await s.start()
    rx = s.receiver
    rx._codec = FakeCodec()
    rx.decoder_factory = FakeCodec
    rx.note_voice_op(11, {"user_ids": ["42", "43"]})
    seq = [0]

    async def talk(seconds, talkers):
        for i in range(int(round(seconds / 0.02))):
            for ssrc, owner, every in talkers:
                if i % every == 0:
                    seq[0] += 1
                    send(rx, ssrc, b"OPUS%d-%d" % (owner, seq[0]), seq=seq[0] % 65536)
            clock.t += 0.02
            if i % 25 == 24:
                await run_ticks(1)

    await talk(2.7, [(S_ANA, 42, 1)])
    rx.map_ssrc(S_BETO, 43)
    await talk(5.0, [(S_ANA, 42, 1), (S_BETO, 43, 1)])
    await talk(45.4, [(S_ANA, 42, 1)])
    ana.voice.self_mute = False  # the cache catches up with Ana
    carla = FakeMember(44, "Carla", channel=voice)
    carla.voice.self_mute = True  # joins with the mic flagged muted
    guild.members[44] = carla
    voice.members = [ana, luis, carla]
    await talk(1.6, [(S_ANA, 42, 1)])
    rx.note_voice_op(11, {"user_ids": ["44"]})
    await talk(1.1, [(S_ANA, 42, 1)])
    await talk(8.4, [(S_ANA, 42, 1), (S_CARLA, 44, 10)])
    rx.map_ssrc(S_CARLA, 44)
    carla.voice.self_mute = False
    await talk(5.0, [(S_ANA, 42, 1), (S_CARLA, 44, 1)])
    await s.stop("stopped")
    await asyncio.sleep(0)

    written = {w.path.name: b"".join(pcm for _, pcm in w.frames) for w in FakeWriter.instances}
    carla_bytes = b"OPUS44-"
    assert all(carla_bytes not in audio for name, audio in written.items() if name != "44.ogg")  # B1
    assert carla_bytes in written["44.ogg"]
    tracks = {p.name for p in (world["service"].root / s.meeting.id / "tracks").iterdir()}
    assert "42.ogg" in tracks and "unidentified-1.ogg" not in tracks  # B2: Ana's voice is Ana's
    speakers = {sp.user_id: sp.name for sp in world["service"].finished[0][1]}
    assert "unidentified-1" not in speakers and speakers["42"] == "Ana"
    assert s.missing_audio == ()
