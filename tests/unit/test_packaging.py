"""Packaging relationships: plugin root entry point and the bundled skill meet Hermes standards."""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "meeting-scribe" / "SKILL.md"
SECTIONS = ("When to Use", "Prerequisites", "How to Run", "Quick Reference", "Procedure", "Pitfalls",
            "Verification")


def _frontmatter(text: str) -> dict:
    assert text.startswith("---\n")
    end = text.index("\n---\n", 4)
    return yaml.safe_load(text[4:end])


def test_skill_frontmatter():
    text = SKILL.read_text(encoding="utf-8")
    fm = _frontmatter(text)
    assert fm["name"] == "meeting-scribe"
    assert len(fm["description"]) <= 60 and fm["description"].endswith(".")
    assert fm["author"].startswith("Pedro Rodriguez (propiter)")
    assert set(fm["platforms"]) == {"linux", "macos"}
    assert "tags" in fm["metadata"]["hermes"]


def test_skill_sections_and_tools():
    text = SKILL.read_text(encoding="utf-8")
    headings = re.findall(r"^## (.+)$", text, re.MULTILINE)
    assert [h for h in headings if h in SECTIONS] == list(SECTIONS)
    assert "meeting_search" in text and "meeting_get" in text
    assert "/home/" not in text


def test_manifest_tools_match_registered_names():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    from meeting_scribe.tools import SCHEMAS
    assert set(manifest["provides_tools"]) == set(SCHEMAS)
    assert manifest["name"] == "meeting-scribe" and manifest["kind"] == "standalone"


def test_root_init_exposes_register():
    text = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert "def register(ctx" in text


@pytest.mark.parametrize("readme", ["README.md", "README.es.md"])
def test_readme_config_table_lists_every_setting(readme):
    """The docs table must cover exactly the settings in config.SPEC (no stale or missing keys)."""
    from meeting_scribe.config import SPEC

    text = (ROOT / readme).read_text(encoding="utf-8")
    keys = set(re.findall(r"^\| `([a-z_]+)` \| (?:str|int|bool|float|list) \|", text, re.M))
    assert keys == set(SPEC)
