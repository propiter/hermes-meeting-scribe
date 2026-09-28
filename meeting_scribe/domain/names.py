"""Generic matching of spoken project names against Discord channel/category names (DESIGN §16).

The plugin runs on any server, so nothing here knows a particular naming scheme:

* :func:`clean_channel_name` removes decoration by Unicode CATEGORY only — emoji and symbols
  (``So``/``Sk``/``Sm``/``Cs``), variation selectors, ZWJ and keycap sequences, bracket and quote
  punctuation (``Ps``/``Pe``/``Pi``/``Pf``) — then trims leftover separators/whitespace at both ends.
  Letters are kept exactly as written (case included). Word prefixes some servers use as
  decoration are configuration (``channel_name_ignore_prefixes``), removed only as whole words.
* :func:`similarity` compares folded text (NFKC, casefold, accents stripped for Latin, split on
  ``-``/``_``/space/punctuation). Score = max of the full-string ratio and the best token-window
  ratio (the shorter token sequence against every same-length window of the longer one, which also
  covers exact containment). The window path is scaled below 1.0, so the channel that IS the name
  beats one that merely contains it, and it only counts when the shorter side has a token of 4+
  characters or several tokens — ``app`` must not match every ``*-app`` channel.
* Ratios use a normalised Levenshtein distance on the text and on a light phonetic key (doubled
  letters collapsed, ``c/q/k``, ``v/b``, ``z/s``, ``y/i``, silent ``h``) to absorb transcription errors.

* :func:`match_person` resolves a person's name as SPOKEN (a task owner the LLM wrote down) to one
  participant: exact name, then a whole-word part of it (given name, surname), then a phonetic
  match for Spanish/English spellings (``Yoana``/``Johanna``, ``Cristofer``/``Christopher``). Each tier must give ONE participant; two is ambiguous and stops there.

Standard library only: ``rapidfuzz`` is not in Hermes' venv and a guild has at most 500 channels.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from .text import fold

MIN_TOKEN_LEN = 4
UNCERTAIN_MARGIN = 0.05
_KEYCAP_RE = re.compile("[0-9#*]\ufe0f?\u20e3")
_REMOVE_CATEGORIES = frozenset({"So", "Sk", "Sm", "Cs", "Co", "Cf", "Ps", "Pe", "Pi", "Pf", "Me"})
_SPACES_RE = re.compile(r"\s{2,}")
_WORD_SEP_RE = re.compile(r"[-_\s]+")


def _is_variation_selector(ch: str) -> bool:
    cp = ord(ch)
    return 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF


def _edge(ch: str) -> bool:
    return not (ch.isalnum() or unicodedata.category(ch).startswith("M"))


def _strip_prefixes(text: str, prefixes: Sequence[str]) -> str:
    wanted = {fold(p) for p in prefixes if fold(p)}
    changed = True
    while wanted and changed:
        changed = False
        parts = _WORD_SEP_RE.split(text, maxsplit=1)
        if len(parts) == 2 and parts[1] and fold(parts[0]) in wanted:
            text, changed = parts[1], True
    return text


def clean_channel_name(name: str, ignore_prefixes: Sequence[str] = ()) -> str:
    text = _KEYCAP_RE.sub(" ", name or "")
    text = "".join(" " if unicodedata.category(ch) in _REMOVE_CATEGORIES or _is_variation_selector(ch) else ch
                   for ch in text)
    text = _SPACES_RE.sub(" ", text)
    start, end = 0, len(text)
    while start < end and _edge(text[start]):
        start += 1
    while end > start and _edge(text[end - 1]):
        end -= 1
    return _strip_prefixes(text[start:end], ignore_prefixes).strip()


def levenshtein_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return 1.0 - prev[-1] / max(len(a), len(b))


def _phonetic(word: str) -> str:
    w = word.replace("ph", "f").replace("qu", "k").replace("h", "")
    w = w.translate(str.maketrans({"c": "k", "q": "k", "v": "b", "z": "s", "y": "i"}))
    return re.sub(r"(.)\1+", r"\1", w)


def _ratio(a: str, b: str) -> float:
    return max(levenshtein_ratio(a, b), levenshtein_ratio(_phonetic(a), _phonetic(b)))


def _window_score(short: list[str], long: list[str]) -> float:
    if not (len(short) > 1 or any(len(t) >= MIN_TOKEN_LEN for t in short)):
        return 0.0
    n = len(short)
    joined = " ".join(short)
    best = max(_ratio(joined, " ".join(long[i:i + n])) for i in range(len(long) - n + 1))
    coverage = len(joined) / max(1, len(" ".join(long)))
    return best * (0.9 + 0.08 * coverage)


def similarity(spoken: str, channel_name: str, *, ignore_prefixes: Sequence[str] = ()) -> float:
    """0..1: how likely ``spoken`` (a project name as the LLM wrote it) names ``channel_name``."""
    a, b = fold(spoken), fold(clean_channel_name(channel_name, ignore_prefixes))
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta = a.split()
    if len(ta) == 1 and len(ta[0]) < MIN_TOKEN_LEN:
        return 0.0  # "dev", "ux", "api": too ambiguous for fuzzy matching; only exact names count
    best = _ratio(a, b)
    tb = b.split()
    if len(ta) != len(tb):
        short, long = (ta, tb) if len(ta) < len(tb) else (tb, ta)
        best = max(best, _window_score(short, long))
    return best


def rank_names(spoken: str, names: Iterable[str], threshold: float, *,
               ignore_prefixes: Sequence[str] = ()) -> list[tuple[str, float]]:
    """Names scoring at or above ``threshold``, best first (stable for ties)."""
    scored = [(n, similarity(spoken, n, ignore_prefixes=ignore_prefixes)) for n in names]
    return sorted([s for s in scored if s[1] >= threshold], key=lambda s: -s[1])


@dataclass(frozen=True)
class NameMatch:
    name: str
    score: float
    uncertain: bool  # a runner-up scored within UNCERTAIN_MARGIN: post it, but ask to confirm


def match_name(spoken: str, names: Iterable[str], threshold: float, *,
               ignore_prefixes: Sequence[str] = ()) -> Optional[NameMatch]:
    ranked = rank_names(spoken, names, threshold, ignore_prefixes=ignore_prefixes)
    if not ranked:
        return None
    (name, score), rest = ranked[0], ranked[1:]
    return NameMatch(name, score, bool(rest) and score - rest[0][1] < UNCERTAIN_MARGIN)


PERSON_THRESHOLD = 0.85


def _person_key(word: str) -> str:
    """The phonetic key of a person's name: :func:`_phonetic` plus Spanish ``ll``≈``y`` (yeísmo) and
    ``j``≈``y`` (``Johanna`` is said "Yohana")."""
    return _phonetic(word.replace("ll", "y").replace("j", "y"))


def _gendered_pair(a: str, b: str) -> bool:
    """``luis``/``luisa``, ``mario``/``maria``, ``daniel``/``daniela``: different people, one letter apart."""
    def stem(w: str) -> str:
        return w[:-1] if w[-1:] in "aeo" else w
    return a != b and stem(a) == stem(b)


def _person_ratio(a: str, b: str) -> float:
    if len(a) < MIN_TOKEN_LEN or len(b) < MIN_TOKEN_LEN:
        return 1.0 if a == b else 0.0
    ka, kb = _person_key(a), _person_key(b)
    if _gendered_pair(a, b) or _gendered_pair(ka, kb):
        return 0.0
    return max(levenshtein_ratio(a, b), levenshtein_ratio(ka, kb))


def _contains(words: list[str], name: list[str]) -> bool:
    n = len(words)
    return any(name[i:i + n] == words for i in range(len(name) - n + 1))


def _fuzzy(words: list[str], name: list[str]) -> float:
    whole = _person_ratio(" ".join(words), " ".join(name)) if len(words) > 1 else 0.0
    if len(words) == 1:
        return max([whole, *(_person_ratio(words[0], w) for w in name)])
    if len(words) == len(name):  # "Kristofer Rios" ~ "Cristofer Ríos": every word must match
        return max(whole, min(_person_ratio(a, b) for a, b in zip(words, name)))
    return whole


def match_person(spoken: str, people: Sequence[tuple[str, Sequence[str]]]) -> Optional[str]:
    """The key of the ONE person ``spoken`` names, or ``None`` (nobody, or several).

    ``people`` is ``(key, names)``: display name first, then aliases (username, nickname…). Emoji,
    accents and case are ignored. Tiers, each needing a single person: the exact name; a whole-word
    part of it (``Ana`` of ``Ana María``); a phonetic/edit match of ``PERSON_THRESHOLD``. Two people
    in a tier (two ``Ana``) leave the owner unassigned rather than guess."""
    words = fold(spoken).split()
    if not words:
        return None
    forms = [(key, [fold(n).split() for n in names if fold(n)]) for key, names in people]
    for tier in (lambda n: n == words, lambda n: _contains(words, n),
                 lambda n: _fuzzy(words, n) >= PERSON_THRESHOLD):
        hits = {key for key, names in forms if any(tier(n) for n in names)}
        if hits:
            return next(iter(hits)) if len(hits) == 1 else None
    return None
