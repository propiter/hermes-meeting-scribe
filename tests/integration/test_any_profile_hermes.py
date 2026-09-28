"""One installation used from every profile, against the REAL Hermes (DESIGN §1.5).

A throwaway Hermes root (``$HOME/.hermes``) with profiles ``owner`` (the Discord bot), ``guest`` and
``bare``. ONE real copy of the plugin, either in the root ``plugins/`` (the recommended shape) or in
the owner profile; every other home that uses it has a symlink to it in its own ``plugins/`` (each
gateway's loader scans only ``<its home>/plugins``, ``plugins_discovery.collect_directory_manifests``;
``hermes plugins enable`` too, ``plugins_cmd._discover_all_plugins``). ``owner_profile`` is declared
in the root ``config.yaml`` except in the layout that relies on where the real files are.

Proves: the owner's loader builds the runtime and writes to the owner's data; another profile's loader
registers nothing that writes; a Desktop backend launched with another profile serves the owner's
meetings; a profile without the plugin enabled still gets Hermes' 404; Hermes' dependency environment
sees one member, not two.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
plugins_mod = pytest.importorskip("hermes_cli.plugins", reason="Hermes is not importable (set PYTHONPATH)")
yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
IGNORE = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "*.pyc", "tests")
PREFIX = "/api/plugins/meeting-scribe"


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


LAYOUTS = {  # where the real copy is, whether owner_profile is declared
    "root-copy": ("root", True),               # the recommended shape (README «Use Meetings from any profile»)
    "owner-copy": ("owner", True),
    "owner-copy-undeclared": ("owner", False),  # the owner is where the real files are
}


@pytest.fixture(params=sorted(LAYOUTS))
def root(request, tmp_path, monkeypatch):
    os_home = tmp_path / "os-home"
    root = os_home / ".hermes"
    where, declared = LAYOUTS[request.param]
    homes = {"root": root, "owner": root / "profiles" / "owner", "guest": root / "profiles" / "guest"}
    real = homes[where] / "plugins" / "meeting-scribe"
    shutil.copytree(REPO, real, ignore=IGNORE)
    for name, profile_home in homes.items():
        if name != where:
            (profile_home / "plugins").mkdir(parents=True)
            (profile_home / "plugins" / "meeting-scribe").symlink_to(real)
    entries = {"entries": {"meeting-scribe": {"owner_profile": "owner"}}} if declared else {}
    _write(root / "config.yaml", {"plugins": {"enabled": ["meeting-scribe"], **entries}})
    _write(root / "profiles" / "owner" / "config.yaml", {"plugins": {
        "enabled": ["meeting-scribe"], "entries": {"meeting-scribe": {"settings": {"transcribe_language": "es"}}}}})
    _write(root / "profiles" / "guest" / "config.yaml", {"plugins": {"enabled": ["meeting-scribe"]}})
    _write(root / "profiles" / "bare" / "config.yaml", {"model": {"default": "x"}})
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: bundled)
    from meeting_scribe import home

    home._ROOT_CONFIG_CACHE.clear()
    # This test process imported ``meeting_scribe`` from the checkout, and every loader below reuses
    # that module; point it at the install, which is what the installed package computes itself.
    monkeypatch.setattr(home, "plugin_root", lambda: real.resolve())
    return root


@pytest.fixture
def load(monkeypatch):
    """Load the plugins of one profile through the real PluginManager, as its gateway would."""
    from tools.registry import registry

    before = {e.name for e in registry._snapshot_entries()}
    managers = []

    def _load(profile_home: Path):
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        mgr = plugins_mod.PluginManager()
        mgr.discover_and_load()
        managers.append(mgr)
        return mgr
    yield _load
    import meeting_scribe.plugin as plugin

    for rt in list(plugin.RUNTIMES.values()):
        rt.close()
    plugin.RUNTIMES.clear()
    for mgr in managers:
        if "meeting-scribe" in mgr._plugins:
            mgr.unload("meeting-scribe")
    assert {e.name for e in registry._snapshot_entries()} - before <= {"meeting_search", "meeting_get"}


def test_the_owner_profile_runs_the_plugin_and_writes_to_its_own_data(root, load):
    import json

    from tools.registry import registry

    mgr = load(root / "profiles" / "owner")
    loaded = mgr._plugins["meeting-scribe"]
    assert loaded.enabled and loaded.module is not None, loaded.error
    assert set(loaded.tools_registered) == {"meeting_search", "meeting_get"}
    assert {"meeting", "meet", "rec"} <= set(mgr._plugin_commands)
    assert json.loads(registry.dispatch("meeting_search", {"query": "x"})) == {"results": []}
    assert (root / "profiles" / "owner" / "plugin-data" / "meeting-scribe" / "index.sqlite").exists()
    assert not (root / "plugin-data").exists()


@pytest.mark.parametrize("profile", ["default", "guest"])
def test_another_profile_with_the_plugin_enabled_registers_nothing_that_writes(root, load, capsys, profile):
    import argparse

    import meeting_scribe.plugin as plugin

    mgr = load(root if profile == "default" else root / "profiles" / profile)  # a linked, enabled profile
    loaded = mgr._plugins["meeting-scribe"]
    assert loaded.enabled and loaded.module is not None, loaded.error
    assert not loaded.tools_registered and not plugin.RUNTIMES
    assert not {"meeting", "meet", "rec"} & set(mgr._plugin_commands)
    entry = mgr._cli_commands["meeting-scribe"]
    parser = argparse.ArgumentParser()
    entry["setup_fn"](parser)
    assert entry["handler_fn"](parser.parse_args(["doctor"])) == 0
    assert "hermes -p owner meeting-scribe" in capsys.readouterr().out
    assert not list(root.glob("**/plugin-data"))


@pytest.fixture
def backend(root, monkeypatch):
    """The Desktop backend's plugin API, as mounted in a process launched with ``profile``."""
    from fastapi.testclient import TestClient
    from hermes_cli import web_server, web_server_dashboard

    def _serve(profile_home: Path):
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        plugins = web_server._get_dashboard_plugins(force_rescan=True)
        assert any(p["name"] == "meeting-scribe" and p["has_api"] for p in plugins), plugins
        before = len(web_server.app.router.routes)
        web_server_dashboard._mount_plugin_api_routes()
        mounted = [r for r in web_server.app.router.routes[before:] if getattr(r, "path", "").startswith(PREFIX)]
        routes = web_server.app.router.routes
        routes[:] = mounted + [r for r in routes if r not in mounted]
        client = TestClient(web_server.app)
        client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        return client, bool(mounted)
    yield _serve
    # The owner scope of a request made the process a multi-profile host (a process-wide switch in
    # Hermes); switch it back so later tests start from a single-profile process again.
    from agent.secret_scope import set_multiplex_active
    from tui_gateway import launch_profile_policy

    set_multiplex_active(False)
    launch_profile_policy._snapshot = None
    web_server.app.router.routes[:] = [r for r in web_server.app.router.routes
                                       if not getattr(r, "path", "").startswith(PREFIX)]
    web_server._get_dashboard_plugins(force_rescan=True)


def _seed_owner(root: Path) -> str:
    from datetime import datetime, timezone

    from meeting_scribe.config import Settings
    from meeting_scribe.domain.models import Meeting, MeetingState
    from meeting_scribe.spaces import bootstrap
    from meeting_scribe.storage.repo import Repository

    data = root / "profiles" / "owner" / "plugin-data" / "meeting-scribe"
    data.mkdir(parents=True)
    repo = Repository(data / "index.sqlite")
    bootstrap(repo, Settings.defaults(), data)
    repo.save_meeting(Meeting(id="any0001a", guild_id="100", channel_id="200", channel_name="Daily",
                              started_at=datetime(2026, 9, 26, 15, 4, tzinfo=timezone.utc),
                              state=MeetingState.DONE, title="Daily", space="main"))
    repo.close()
    return "any0001a"


def test_a_desktop_backend_launched_with_another_profile_serves_the_owners_meetings(root, backend):
    mid = _seed_owner(root)
    client, mounted = backend(root / "profiles" / "guest")
    assert mounted
    for params in ({}, {"profile": "guest"}, {"profile": "owner"}):
        r = client.get(f"{PREFIX}/v1/meetings", params=params)
        assert r.status_code == 200, (params, r.text)
        assert [m["id"] for m in r.json()["items"]] == [mid]
    s = client.get(f"{PREFIX}/v1/settings").json()
    assert s["values"]["transcribe_language"] == {"value": "es", "origin": "configured"}  # the owner's settings
    q = client.post(f"{PREFIX}/v1/meetings/{mid}/commands",
                    json={"request_id": "any-1", "action": "reprocess", "stage": "deliver", "confirm": True})
    assert q.status_code == 200 and q.json()["state"] == "queued"  # for the owner's worker
    owner = next(c for c in client.get(f"{PREFIX}/v1/doctor").json()["checks"] if c["name"] == "owner")
    assert "profile 'owner'" in owner["detail"]
    assert not (root / "profiles" / "guest" / "plugin-data").exists()


def test_a_profile_that_has_not_enabled_the_plugin_still_gets_hermes_404(root, backend):
    client, mounted = backend(root / "profiles" / "bare")
    assert not mounted  # Hermes' own gate: enabling is the operator's choice per profile
    assert client.get(f"{PREFIX}/v1/status").status_code == 404


def test_the_dependency_environment_sees_one_member_for_the_copy_and_its_links(root, monkeypatch):
    """Two real copies are two workspace members with the same package name (uv refuses); a symlink
    resolves to the same identity (``pm.workspace.member_sources`` keys by resolved path)."""
    from pm.workspace import enabled_member_dirs, member_sources

    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "guest"))
    selected = enabled_member_dirs()
    assert len(selected) == 3  # default, owner and guest: one real copy, two links
    assert len(member_sources(selected)) == 1
