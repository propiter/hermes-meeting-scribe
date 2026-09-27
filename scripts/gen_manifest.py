"""Regenerate ``plugin.yaml`` from ``meeting_scribe.config.SPEC``.

Run after changing settings: ``.venv/bin/python scripts/gen_manifest.py``. The unit test
``test_plugin_yaml_config_schema_in_sync_with_settings`` fails when the two drift.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meeting_scribe.config import schema_for_manifest  # noqa: E402

HEADER = {
    "name": "meeting-scribe",
    "version": "0.1.0",
    "description": (
        "Record Discord voice meetings per speaker, transcribe locally with faster-whisper, "
        "and turn them into notes, decisions and tasks (Kanban, Linear, Obsidian)."
    ),
    "author": "Pedro Rodriguez (propiter)",
    "license": "MIT",
    "kind": "standalone",
    "manifest_version": 2,
    "requires_hermes": ">=0.21",
    "homepage": "https://github.com/propiter/hermes-meeting-scribe",
    "platforms": ["linux", "macos"],
    "tags": ["discord", "voice", "meetings", "transcription", "notes"],
    "python_dependencies": ["faster-whisper>=1.1,<2"],
    "external_dependencies": ["ffmpeg with libopus (PATH, ~/.hermes/tools or audio_ffmpeg_path)"],
    "optional_env": [{
        "name": "LINEAR_API_KEY",
        "description": "Linear personal API key (enables Linear issue creation)",
        "url": "https://linear.app/settings/account/security",
        "secret": True,
    }],
    "provides_tools": ["meeting_search", "meeting_get"],
    "provides_hooks": [],
}


def render() -> str:
    text = "# config_schema is generated from meeting_scribe/config.py (scripts/gen_manifest.py).\n"
    text += yaml.safe_dump(HEADER, sort_keys=False, allow_unicode=True, width=100)
    text += yaml.safe_dump({"config_schema": schema_for_manifest()}, sort_keys=False,
                           allow_unicode=True, width=100)
    return text


if __name__ == "__main__":
    (ROOT / "plugin.yaml").write_text(render(), encoding="utf-8")
    print("plugin.yaml regenerated")
