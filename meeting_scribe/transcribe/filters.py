"""Whisper hallucination filters (DESIGN §6).

Meetings differ from Hermes voice mode: "gracias" or "thank you" are real utterances there, so
ambiguous short phrases are only dropped when whisper itself was unsure. Phrases that never
occur in a real meeting (subtitle credits, "thanks for watching") are always dropped.

Text is compared after a script-preserving fold (review finding 3): casefold, Latin accents
removed, every other script kept — the old ASCII folding turned Russian/Chinese/Japanese/Arabic
speech into an empty string and dropped it. Repetition ("no, no, no, no") is real speech in a
meeting, so it is only dropped when whisper was also unsure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ..domain.text import _strip_ascii_accents

NO_SPEECH_PROB = 0.6
LOW_LOGPROB = -1.0

ALWAYS_DROP = (
    "thanks for watching", "thank you for watching", "please subscribe", "like and subscribe",
    "subscribe to my channel", "subtitulos realizados por la comunidad de amara.org",
    "subtitulos por la comunidad de amara.org", "amara.org", "gracias por ver", "gracias por ver el video",
    "suscribete", "suscribete al canal", "sous-titres realises par la communaute d'amara.org",
    "sottotitoli creati dalla comunita amara.org", "www.mooji.org",
)
AMBIGUOUS = ("thank you", "thanks", "you", "bye", "gracias", "adios", "hasta luego", "chao", "ok", "okay")
_REPEAT_RE = re.compile(r"^(\w+)(?:[\s,.!?，、。]+\1){3,}[\s,.!?，、。]*$")
RUNAWAY_REPEATS = 8  # a word said 8+ times in a row is a decoder loop even at high confidence


@dataclass(frozen=True)
class RawWord:
    start: float
    end: float
    word: str
    probability: float


@dataclass(frozen=True)
class RawSegment:
    """Worker-side representation of a faster-whisper segment (seconds relative to the track)."""

    start: float
    end: float
    text: str
    avg_logprob: float
    no_speech_prob: float
    words: tuple[RawWord, ...] = ()


def _norm(text: str) -> str:
    folded = _strip_ascii_accents(text or "").casefold()
    return " ".join(folded.strip(" \t\n.!¡?¿,;:。！？、，…").split())


def is_hallucination(seg: RawSegment) -> bool:
    text = _norm(seg.text)
    if not text or not re.search(r"\w", text):
        return True
    if seg.no_speech_prob > NO_SPEECH_PROB and seg.avg_logprob < LOW_LOGPROB:
        return True
    if text in ALWAYS_DROP or "amara.org" in text:
        return True
    unsure = seg.no_speech_prob > 0.4 or seg.avg_logprob < -0.8
    if _REPEAT_RE.match(text):  # looping decoder output vs. a real "no, no, no, no"
        return unsure or len(text.split()) >= RUNAWAY_REPEATS
    return text in AMBIGUOUS and unsure


def keep_segments(segments: Iterable[RawSegment]) -> list[RawSegment]:
    return [s for s in segments if not is_hallucination(s)]
