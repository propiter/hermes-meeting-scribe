"""Load the plugin through the REAL Hermes loader in a throwaway HERMES_HOME.

Run with Hermes importable, e.g.::

    HERMES_SRC=~/.hermes/hermes-agent PYTHONPATH=$HERMES_SRC \
      $HERMES_SRC/venv/bin/python -m pytest tests/integration -m integration

Skipped automatically when ``hermes_cli`` cannot be imported (unit runs stay Hermes-free).
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
plugins_mod = pytest.importorskip("hermes_cli.plugins", reason="Hermes is not importable (set PYTHONPATH)")
yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
IGNORE = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "*.pyc", "tests")


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(REPO, home / "plugins" / "meeting-scribe", ignore=IGNORE)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["meeting-scribe"]}}), encoding="utf-8")
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)  # not a gateway unless a test says so
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: bundled)
    return home


@pytest.fixture
def manager(hermes_home):
    from tools.registry import registry

    before = {e.name for e in registry._snapshot_entries()}
    mgr = plugins_mod.PluginManager()
    mgr.discover_and_load()
    yield mgr
    import meeting_scribe.plugin as plugin

    for rt in list(plugin.RUNTIMES.values()):
        rt.close()
    plugin.RUNTIMES.clear()
    with_suppress = [n for n in ("meeting-scribe",) if n in mgr._plugins]
    for name in with_suppress:
        mgr.unload(name)
    assert {e.name for e in registry._snapshot_entries()} - before <= {"meeting_search", "meeting_get"}


def test_plugin_loads_and_registers(manager, hermes_home):
    loaded = manager._plugins["meeting-scribe"]
    assert loaded.enabled is True and loaded.module is not None, getattr(loaded, "error", None)
    assert set(loaded.tools_registered) == {"meeting_search", "meeting_get"}
    assert {"meeting", "meet", "rec"} <= set(manager._plugin_commands)
    assert "meeting-scribe" in manager._cli_commands
    assert "meeting_scribe" in manager._aux_tasks
    assert "meeting-scribe:meeting-scribe" in manager._plugin_skills


def test_tool_dispatch_through_registry(manager, hermes_home):
    from tools.registry import registry

    out = json.loads(registry.dispatch("meeting_search", {"query": "smtp"}))
    assert out == {"results": []}
    assert (hermes_home / "plugin-data" / "meeting-scribe" / "index.sqlite").exists()


def test_slash_command_answers(manager):
    handler = manager._plugin_commands["meeting"]["handler"]
    reply = handler("help")
    assert "/meeting" in reply
    assert "not connected" in handler("start")  # capture installed; no Discord connection here


def test_cli_command_config_roundtrip(manager, hermes_home, capsys):
    entry = manager._cli_commands["meeting-scribe"]
    parser = argparse.ArgumentParser()
    entry["setup_fn"](parser)
    assert entry["handler_fn"](parser.parse_args(["config", "set", "kanban_mode", "off"])) == 0
    cfg = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["plugins"]["entries"]["meeting-scribe"]["settings"]["kanban_mode"] == "off"  # flat
    assert entry["handler_fn"](parser.parse_args(["config", "get", "kanban_mode"])) == 0
    assert capsys.readouterr().out.strip().endswith("off")


def test_desktop_settings_form_shows_saved_values(manager, hermes_home):
    """Review finding 10: Hermes' form reads settings[key] flat; dotted keys always showed defaults."""
    from hermes_cli.plugins_settings import plugin_settings_fields, save_plugin_settings

    plugin_dir = hermes_home / "plugins" / "meeting-scribe"
    save_plugin_settings("meeting-scribe", plugin_dir, {"kanban_mode": "auto", "autojoin_min_humans": 3})
    fields = {f["key"]: f for f in plugin_settings_fields("meeting-scribe", plugin_dir)}
    assert fields["kanban_mode"]["value"] == "auto" and fields["autojoin_min_humans"]["value"] == 3
    import meeting_scribe.plugin as plugin

    rt = next(iter(plugin.RUNTIMES.values()))
    assert rt.settings().kanban_mode == "auto" and rt.settings().autojoin_min_humans == 3


def test_legacy_nested_settings_are_still_honoured(manager, hermes_home):
    cfg = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    cfg.setdefault("plugins", {}).setdefault("entries", {})["meeting-scribe"] = {
        "settings": {"linear": {"mode": "off"}}}
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    import meeting_scribe.plugin as plugin

    rt = next(iter(plugin.RUNTIMES.values()))
    assert rt.settings().linear_mode == "off"
