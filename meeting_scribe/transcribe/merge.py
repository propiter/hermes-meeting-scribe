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
from .filters import RawSegment, keep_segments

MERGE_GAP = 1.0


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


def merge_tracks(tracks: Sequence[TrackResult], gap: float = MERGE_GAP) -> list[Utterance]:
    utts = sorted((_to_utterance(tr, s) for tr in tracks for s in keep_segments(tr.segments)),
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
