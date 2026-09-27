"""Script-agnostic text folding for matching and content-derived ids (review findings 3/4).

The original helpers folded text to ASCII, which erased every Cyrillic, CJK, Arabic, Hebrew,
Devanagari… character: two different Chinese tasks got the same id and a Russian sentence looked
empty to the hallucination filter. :func:`fold` keeps letters of every script:

* NFKC compatibility normalisation (full-width Latin, ligatures) + ``casefold``;
* combining marks are removed only when they sit on an ASCII base letter, so Spanish/French
  accents keep matching their unaccented spelling (``adiós`` == ``adios``, and ids of Latin titles
  are unchanged from earlier releases), while marks that are part of a letter elsewhere (Japanese
  dakuten ``が`` ≠ ``か``, Devanagari vowel signs) are preserved;
* punctuation/symbols become spaces and whitespace is collapsed.
"""
from __future__ import annotations

import unicodedata


def _strip_ascii_accents(text: str) -> str:
    out: list[str] = []
    for ch in unicodedata.normalize("NFKD", text):
        if unicodedata.combining(ch) and out and out[-1].isascii():
            continue
        out.append(ch)
    return unicodedata.normalize("NFKC", "".join(out))


def fold(text: str) -> str:
    """Casefolded, accent-insensitive (Latin), punctuation-free, space-collapsed text."""
    base = _strip_ascii_accents(unicodedata.normalize("NFKC", text or "")).casefold()
    kept = [ch if (ch.isalnum() or unicodedata.category(ch).startswith("M")) else " " for ch in base]
    return " ".join("".join(kept).split())
