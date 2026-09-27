"""Dashboard backend of meeting-scribe: the REST API behind the Desktop «Meetings» page.

Hermes' web server imports this file (only for an enabled user plugin) and mounts ``router`` under
``/api/plugins/meeting-scribe``. The implementation lives in ``meeting_scribe.desktop.api`` so it is
unit-tested with the rest of the package; this loader only makes that package importable by its
absolute name, exactly like the plugin's ``__init__.py`` does for the agent side.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from meeting_scribe.desktop.api import router  # noqa: E402

__all__ = ["router"]
