"""The Desktop page (desktop/plugin.js): syntax, SDK surface and the node:test suite.

Node is optional for a Python-only checkout: the tests skip when ``node`` is not on PATH. When a
Hermes checkout is present, every name imported from ``@hermes/plugin-sdk`` must be exported by its
``apps/desktop/src/sdk/index.ts`` (a renamed/removed SDK symbol breaks the page at load time).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_JS = ROOT / "desktop" / "plugin.js"
NODE = shutil.which("node") or ""
HERMES_SRC = Path(os.environ.get("HERMES_SRC") or Path.home() / ".hermes" / "hermes-agent")
SDK_INDEX = HERMES_SRC / "apps" / "desktop" / "src" / "sdk" / "index.ts"


def _sdk_imports() -> list[str]:
    source = PLUGIN_JS.read_text(encoding="utf-8")
    block = re.search(r"import\s*\{([^}]*)\}\s*from\s*'@hermes/plugin-sdk'", source)
    assert block, "plugin.js must import from @hermes/plugin-sdk"
    return [name.strip() for name in block.group(1).split(",") if name.strip()]


def test_plugin_js_exists_with_default_export():
    source = PLUGIN_JS.read_text(encoding="utf-8")
    assert "export default plugin" in source
    assert "id: ID" in source and "export const ID = 'meeting-scribe'" in source


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_node_check():
    subprocess.run([NODE, "--check", str(PLUGIN_JS)], check=True, capture_output=True, text=True)


@pytest.mark.skipif(not SDK_INDEX.is_file(), reason="no Hermes checkout with the Desktop SDK")
def test_sdk_imports_exist_in_hermes_sdk():
    sdk = SDK_INDEX.read_text(encoding="utf-8")
    missing = [name for name in _sdk_imports()
               if not re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", sdk)]
    assert missing == []


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_node_suite():
    out = subprocess.run([NODE, "--no-deprecation", str(ROOT / "tests" / "desktop" / "run.mjs")],
                         capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert out.returncode == 0, out.stdout[-4000:] + out.stderr[-4000:]
    assert re.search(r"# fail 0|ℹ fail 0", out.stdout), out.stdout[-2000:]
