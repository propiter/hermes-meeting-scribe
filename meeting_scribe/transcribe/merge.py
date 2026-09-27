"""Per-speaker track results -> one ordered transcript (DESIGN §6).

Each speaker is transcribed on their own track, so there is no diarization error; merging only
needs offsets (track start relative to meeting t0) and a stable ordering. Consecutive segments of
the same speaker closer than ``MERGE_GAP`` are joined so the transcript reads as sentences, but
only when nobody else spoke in between.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Sequence

from ..domain.models import Speaker, Utterance, Word
from .filters import RawSegment, RawWord, keep_segments

MERGE_GAP = 1.0
SPLIT_GAP = 1.5  # a pause longer than this inside one whisper segment starts a new utterance


@dataclass(frozen=True)
class TrackResult:
    speaker: Speaker
    offset: float
    segments: Sequence[RawSegment] = field(default_factory=tuple)


def _to_utterance(track: TrackResult, seg: RawSegment) -> Utterance:
    off = track.offset
    words = tuple(Word(round(w.start + off, 3), round(w.end + off, 3), w.word.strip(), round(w.probability, 4))
                  for w in seg.words)
    return Utterance(t0=round(seg.start + off, 3), t1=round(seg.end + off, 3), speaker_id=track.speaker.user_id,
                     speaker=track.speaker.name, text=seg.text.strip(), words=words,
                     confidence=round(seg.avg_logprob, 4))


def split_on_word_gaps(seg: RawSegment, gap: float = SPLIT_GAP) -> list[RawSegment]:
    """Split a segment wherever its words are more than ``gap`` seconds apart.

    On a timeline-aligned per-speaker track the other speakers' turns are silence, and whisper
    often returns ONE segment across it (seen in the E2E run: 0.4→39.9 s holding speech at 0.4 s
    and at 36.6 s). Left whole, that sentence would be ordered before everything said in between.
    Needs word timestamps; segments without words are returned unchanged.
    """
    words = seg.words
    if len(words) < 2:
        return [seg]
    groups: list[list[RawWord]] = [[words[0]]]
    for prev, word in zip(words, words[1:]):
        if word.start - prev.end > gap:
            groups.append([])
        groups[-1].append(word)
    if len(groups) == 1:
        return [seg]
    return [replace(seg, start=g[0].start if i else seg.start, end=g[-1].end if i < len(groups) - 1 else seg.end,
                    text="".join(w.word for w in g), words=tuple(g))
            for i, g in enumerate(groups)]


def merge_tracks(tracks: Sequence[TrackResult], gap: float = MERGE_GAP) -> list[Utterance]:
    utts = sorted((_to_utterance(tr, part) for tr in tracks for s in keep_segments(tr.segments)
                   for part in split_on_word_gaps(s)),
                  key=lambda u: (u.t0, u.speaker_id))
    merged: list[Utterance] = []
    for u in utts:
        prev = merged[-1] if merged else None
        if prev and prev.speaker_id == u.speaker_id and u.t0 - prev.t1 <= gap:
            merged[-1] = replace(prev, t1=max(prev.t1, u.t1), text=f"{prev.text} {u.text}",
                                 words=prev.words + u.words,
                                 confidence=round(min(prev.confidence, u.confidence), 4))
        else:
            merged.append(u)
    return merged
