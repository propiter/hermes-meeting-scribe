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

_SLUG_MAX = 40


def short_id() -> str:
    """8 chars of lowercase base32 (40 bits): unambiguous to read aloud, safe in paths."""
    return base64.b32encode(secrets.token_bytes(5)).decode("ascii").lower()


def slugify(text: str, max_len: int = _SLUG_MAX) -> str:
    """ASCII, hyphenated, bounded slug; ``meeting`` when nothing survives normalisation."""
    norm = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
    slug = slug[:max_len].rstrip("-")
    return slug or "meeting"


def idempotency_key(meeting_id: str, item_id: str, *, sink: str | None = None) -> str:
    """``mtg:<meeting_id>:<item_id>`` (DESIGN §8), optionally sink-qualified for per-sink rows."""
    key = f"mtg:{meeting_id}:{item_id}"
    return f"{key}:{sink}" if sink else key


def _normalize_title(title: str) -> str:
    norm = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", norm).split())


def action_item_id(title: str, owner_speaker_id: str | None) -> str:
    """Deterministic id from normalised title + owner (see module docstring for why)."""
    digest = hashlib.sha1(f"{_normalize_title(title)}|{owner_speaker_id or ''}".encode()).hexdigest()
    return "a" + digest[:10]
