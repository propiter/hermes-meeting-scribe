from meeting_scribe.analyze.chunking import chunk_utterances, render_lines
from meeting_scribe.domain.models import Utterance


def U(i, text="x" * 50):
    return Utterance(float(i), float(i) + 0.5, str(i % 2), f"S{i % 2}", text)


def test_render_lines_include_speaker_id_and_timestamp():
    lines = render_lines([Utterance(65, 66, "10", "Ana", "hola")])
    assert lines == ["[01:05] Ana (id=10): hola"]


def test_single_chunk_when_small():
    utts = [U(i) for i in range(5)]
    assert chunk_utterances(utts, max_chars=10_000) == [utts]


def test_chunks_respect_boundaries_and_overlap():
    utts = [U(i) for i in range(40)]
    chunks = chunk_utterances(utts, max_chars=700, overlap=2)
    assert len(chunks) > 2
    assert all(sum(len(line) + 1 for line in render_lines(c)) <= 700 for c in chunks)
    for a, b in zip(chunks, chunks[1:]):
        assert a[-2:] == b[:2]
    covered = {u.t0 for c in chunks for u in c}
    assert covered == {u.t0 for u in utts}


def test_oversized_single_utterance_is_its_own_chunk():
    utts = [U(0, "y" * 5000), U(1)]
    chunks = chunk_utterances(utts, max_chars=1000, overlap=1)
    assert chunks[0] == [utts[0]] and utts[1] in chunks[-1]


def test_empty():
    assert chunk_utterances([], 1000) == []
