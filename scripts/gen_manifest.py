"""Regenerate ``plugin.yaml`` and the README configuration tables from ``meeting_scribe.config.SPEC``.

Run after changing settings: ``.venv/bin/python scripts/gen_manifest.py``. Unit tests fail when
the manifest or the README tables (between the ``config-table`` markers) drift from ``SPEC``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meeting_scribe.config import GROUPS, SPEC, schema_for_manifest  # noqa: E402
from meeting_scribe.i18n import t  # noqa: E402

START, END = "<!-- config-table:start -->", "<!-- config-table:end -->"
READMES = {"README.md": "en", "README.es.md": "es"}

HEADER = {
    "name": "meeting-scribe",
    "version": "0.2.0",
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


def _default(value) -> str:
    if isinstance(value, tuple):
        return "`[" + ", ".join(value) + "]`"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if value == "" or (isinstance(value, str) and value != value.strip()):
        return f'`"{value}"`'
    return f"`{value}`"


def config_tables(lang: str) -> str:
    head = {"en": "| Key | Type | Default | Description |", "es": "| Clave | Tipo | Por defecto | Descripción |"}[lang]
    out: list[str] = []
    for group in GROUPS:
        keys = [k for k, o in SPEC.items() if o.group == group]
        if not keys:
            continue
        out += [f"#### {t(f'cfg.group.{group}', lang)}", "", head, "|---|---|---|---|"]
        for key in keys:
            opt = SPEC[key]
            extra = f" ({' / '.join(f'`{c}`' for c in opt.choices)})" if opt.choices else ""
            out.append(f"| `{key}` | {opt.kind} | {_default(opt.default)} | {t(f'cfg.{key}.help', lang)}{extra} |")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_readme(text: str, lang: str) -> str:
    before, _, rest = text.partition(START)
    _, _, after = rest.partition(END)
    return f"{before}{START}\n{config_tables(lang)}{END}{after}"


if __name__ == "__main__":
    (ROOT / "plugin.yaml").write_text(render(), encoding="utf-8")
    for name, lang in READMES.items():
        path = ROOT / name
        text = path.read_text(encoding="utf-8")
        if START in text:
            path.write_text(render_readme(text, lang), encoding="utf-8")
    print("plugin.yaml and README configuration tables regenerated")
