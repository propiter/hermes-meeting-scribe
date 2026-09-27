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


def test_segment_spanning_a_long_silence_is_split_on_word_gaps():
    """E2E finding: on a per-speaker track whisper returns ONE segment across the silence while
    others talk (0.4→39.9 s with speech at 0.4 and 36.6 s). Split on word gaps so the later
    sentence is ordered after the other speakers' turns."""
    w = lambda a, b, t: RawWord(a, b, t, 0.9)  # noqa: E731
    pedro = TrackResult(Speaker("1", "Pedro"), 0.0, [
        s(0.4, 39.9, " Buenos días. Me parece bien.",
          (w(0.4, 1.0, " Buenos"), w(1.0, 1.7, " días."), w(36.6, 37.0, " Me"), w(37.0, 37.4, " parece"),
           w(37.4, 39.9, " bien.")))])
    laura = TrackResult(Speaker("2", "Laura"), 0.0, [s(12.0, 20.0, "Terminé las pruebas.")])
    utts = merge_tracks([pedro, laura])
    assert [(u.speaker, u.t0, u.t1, u.text) for u in utts] == [
        ("Pedro", 0.4, 1.7, "Buenos días."), ("Laura", 12.0, 20.0, "Terminé las pruebas."),
        ("Pedro", 36.6, 39.9, "Me parece bien.")]
    assert [w.text for w in utts[2].words] == ["Me", "parece", "bien."]


def test_short_word_gaps_do_not_split():
    w = lambda a, b, t: RawWord(a, b, t, 0.9)  # noqa: E731
    ana = TrackResult(Speaker("1", "Ana"), 0.0, [s(0, 3, "uno dos", (w(0, 1, " uno"), w(2.0, 3, " dos")))])
    assert [u.text for u in merge_tracks([ana])] == ["uno dos"]
