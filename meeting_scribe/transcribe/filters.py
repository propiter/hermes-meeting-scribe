"""Whisper hallucination filters (DESIGN §6).

Meetings differ from Hermes voice mode: "gracias" or "thank you" are real utterances there, so
ambiguous short phrases are only dropped when whisper itself was unsure. Phrases that never
occur in a real meeting (subtitle credits, "thanks for watching") are always dropped.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

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
_REPEAT_RE = re.compile(r"^(\w+)(?:[\s,.!?]+\1){3,}[\s,.!?]*$")


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
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(ascii_text.strip(" \t\n.!¡?¿,;:").split())


def is_hallucination(seg: RawSegment) -> bool:
    text = _norm(seg.text)
    if not text or not re.search(r"\w", text):
        return True
    if seg.no_speech_prob > NO_SPEECH_PROB and seg.avg_logprob < LOW_LOGPROB:
        return True
    if text in ALWAYS_DROP or "amara.org" in text:
        return True
    if _REPEAT_RE.match(text):
        return True
    unsure = seg.no_speech_prob > 0.4 or seg.avg_logprob < -0.8
    return text in AMBIGUOUS and unsure


def keep_segments(segments: Iterable[RawSegment]) -> list[RawSegment]:
    return [s for s in segments if not is_hallucination(s)]
