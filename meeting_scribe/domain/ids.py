"""Identifier helpers.

Short ids are human-typable (``/meeting show k3v7q2ab``); action-item ids are *content derived*
so that re-running analysis on the same transcript yields the same ids and therefore the same
idempotency keys — reprocessing never duplicates a Kanban task or Linear issue.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import unicodedata

from .text import fold

_SLUG_MAX = 60  # room for "google-meet-<date>-<time>-<code>" (41 chars) plus a few words


def short_id() -> str:
    """8 chars of lowercase base32 (40 bits): unambiguous to read aloud, safe in paths."""
    return base64.b32encode(secrets.token_bytes(5)).decode("ascii").lower()


def slugify(text: str, max_len: int = _SLUG_MAX) -> str:
    """ASCII, hyphenated, bounded slug; ``meeting`` when nothing survives normalisation.

    Cut on WORD boundaries (a word is a whitespace/punctuation-separated token of the original
    text, so ``gmj-bcgo-bqf`` counts as one): a meeting code is kept whole or dropped, never
    truncated mid-token. Only a first word longer than ``max_len`` on its own is hard-cut.
    """
    norm = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    words = [w for w in (re.sub(r"[^a-z0-9]+", "-", tok).strip("-") for tok in re.split(r"[^\w-]+", norm)) if w]
    slug = ""
    for word in words:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > max_len:
            break
        slug = candidate
    if not slug and words:
        slug = words[0][:max_len].rstrip("-")
    return slug or "meeting"


def idempotency_key(meeting_id: str, item_id: str, *, sink: str | None = None) -> str:
    """``mtg:<meeting_id>:<item_id>`` (DESIGN §8), optionally sink-qualified for per-sink rows."""
    key = f"mtg:{meeting_id}:{item_id}"
    return f"{key}:{sink}" if sink else key


def _normalize_title(title: str) -> str:
    """Script-preserving (NFKC + casefold) so CJK/Cyrillic titles get distinct ids (finding 4);
    Latin titles fold exactly as before, so existing ids stay stable."""
    return fold(title)


def action_item_id(title: str, owner_speaker_id: str | None) -> str:
    """Deterministic id from normalised title + owner (see module docstring for why)."""
    digest = hashlib.sha1(f"{_normalize_title(title)}|{owner_speaker_id or ''}".encode()).hexdigest()
    return "a" + digest[:10]
