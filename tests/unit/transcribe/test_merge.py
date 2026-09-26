from meeting_scribe.domain.models import Speaker, Word
from meeting_scribe.transcribe.filters import RawSegment, RawWord
from meeting_scribe.transcribe.merge import TrackResult, merge_tracks


def s(start, end, text, words=()):
    return RawSegment(start=start, end=end, text=text, avg_logprob=-0.2, no_speech_prob=0.1, words=words)


def test_merge_sorts_across_speakers_and_applies_offset():
    ana = TrackResult(Speaker("10", "Ana"), offset=0.0, segments=[s(0, 2, "Hola"), s(10, 12, "Chao")])
    luis = TrackResult(Speaker("11", "Luis"), offset=5.0, segments=[s(0, 2, "Buenas", (RawWord(0.1, 0.5, " Buenas", 0.9),))])
    utts = merge_tracks([ana, luis])
    assert [(u.speaker, u.t0, u.text) for u in utts] == [("Ana", 0, "Hola"), ("Luis", 5.0, "Buenas"),
                                                         ("Ana", 10, "Chao")]
    assert utts[1].words == (Word(5.1, 5.5, "Buenas", 0.9),)


def test_adjacent_same_speaker_segments_within_gap_merge():
    ana = TrackResult(Speaker("10", "Ana"), 0.0, [s(0, 2, "Primero"), s(2.8, 4, "segundo"), s(6, 7, "tercero")])
    utts = merge_tracks([ana])
    assert [(u.t0, u.t1, u.text) for u in utts] == [(0, 4, "Primero segundo"), (6, 7, "tercero")]


def test_interleaved_speaker_prevents_merge():
    ana = TrackResult(Speaker("10", "Ana"), 0.0, [s(0, 2, "uno"), s(2.5, 3, "dos")])
    luis = TrackResult(Speaker("11", "Luis"), 0.0, [s(2.1, 2.4, "sí")])
    assert [u.text for u in merge_tracks([ana, luis])] == ["uno", "sí", "dos"]


def test_hallucinations_filtered_during_merge_and_text_stripped():
    ana = TrackResult(Speaker("10", "Ana"), 0.0, [s(0, 1, "  hola  "), s(1, 2, "Thanks for watching!")])
    utts = merge_tracks([ana])
    assert [u.text for u in utts] == ["hola"]
    assert utts[0].confidence == -0.2
