"""The single owner-home resolver: data and settings belong to the home where the plugin is
installed, never to the profile a request is scoped to (DESIGN §1.5)."""
from __future__ import annotations

import sys
import types

import pytest

from meeting_scribe import home


@pytest.fixture
def constants(monkeypatch, tmp_path):
    """A stand-in ``hermes_constants`` whose process home and per-request override are controllable."""
    state = {"process": tmp_path / "process-home", "override": None}
    mod = types.ModuleType("hermes_constants")
    mod.get_process_hermes_home = lambda: state["process"]
    mod.get_hermes_home = lambda: state["override"] or state["process"]
    monkeypatch.setitem(sys.modules, "hermes_constants", mod)
    return state


def test_installed_plugin_belongs_to_its_home_whatever_the_scoped_home(constants, tmp_path):
    owner = tmp_path / "root" / "profiles" / "team"
    install = owner / "plugins" / "meeting-scribe"
    install.mkdir(parents=True)
    seen = set()
    for scoped in (tmp_path / "root", tmp_path / "root" / "profiles" / "other"):
        constants["override"] = scoped  # what Desktop's ?profile= scope does
        seen.add(home.data_dir(install))
    assert seen == {owner / "plugin-data" / "meeting-scribe"}
    assert (owner / "plugin-data" / "meeting-scribe").is_dir()


def test_a_checkout_outside_plugins_uses_the_process_home_not_the_request_override(constants, tmp_path):
    checkout = tmp_path / "src" / "hermes-meeting-scribe"
    checkout.mkdir(parents=True)
    constants["override"] = tmp_path / "someone-else"
    assert home.owner_home(checkout) == constants["process"]
    assert home.data_dir(checkout) == constants["process"] / "plugin-data" / "meeting-scribe"


def test_the_package_resolves_its_own_install_root():
    assert (home.plugin_root() / "meeting_scribe" / "home.py").is_file()
