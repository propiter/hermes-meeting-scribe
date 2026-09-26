"""Hermes plugin entry point for ``meeting-scribe``.

Hermes imports this directory as ``hermes_plugins.meeting_scribe`` (not as a top-level package), so
the plugin root is put on ``sys.path`` to make the inner ``meeting_scribe`` package importable by
absolute name — the transcription subprocess (``python -m meeting_scribe.transcribe.worker``)
relies on the same name.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent


def register(ctx: Any) -> None:
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from meeting_scribe.plugin import register as _register

    _register(ctx, _ROOT)
