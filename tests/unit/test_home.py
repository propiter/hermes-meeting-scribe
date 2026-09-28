"""The single owner resolver: one profile owns the installation (records, processes, keeps the data)
and every other profile only serves the Desktop page with the owner's data (DESIGN §1.5)."""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest
import yaml

from meeting_scribe import home


@pytest.fixture
def hermes(monkeypatch, tmp_path):
    """A stand-in ``hermes_constants`` over a throwaway Hermes root with profiles ``team`` and ``other``."""
    root = tmp_path / "root"
    for name in ("team", "other"):
        (root / "profiles" / name).mkdir(parents=True)
    state = {"process": root, "override": None, "root": root}
    mod = types.ModuleType("hermes_constants")
    mod.PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
    mod.get_default_hermes_root = lambda: root
    mod.get_process_hermes_home = lambda: state["process"]
    mod.get_hermes_home = lambda: state["override"] or state["process"]

    def profile_name_for_home(path):
        path = Path(path)
        if path == root:
            return "default"
        return path.name if path.parent == root / "profiles" else None
    mod.profile_name_for_home = profile_name_for_home
    monkeypatch.setitem(sys.modules, "hermes_constants", mod)
    home._ROOT_CONFIG_CACHE.clear()
    return state


def _declare(root: Path, value, **extra) -> None:
    (root / "config.yaml").write_text(yaml.safe_dump(
        {"plugins": {"enabled": ["meeting-scribe"], "entries": {"meeting-scribe": {"owner_profile": value}}},
         **extra}), encoding="utf-8")


def test_installed_plugin_belongs_to_its_home_whatever_the_scoped_home(hermes):
    root = hermes["root"]
    install = root / "profiles" / "team" / "plugins" / "meeting-scribe"
    install.mkdir(parents=True)
    seen = set()
    for scoped in (root, root / "profiles" / "other"):
        hermes["override"] = scoped  # what Desktop's ?profile= scope does
        seen.add(home.data_dir(install))
    assert seen == {root / "profiles" / "team" / "plugin-data" / "meeting-scribe"}
    assert home.owner(install) == home.Owner("team", root / "profiles" / "team", False)


def test_a_checkout_outside_plugins_uses_the_process_home_not_the_request_override(hermes, tmp_path):
    checkout = tmp_path / "src" / "hermes-meeting-scribe"
    checkout.mkdir(parents=True)
    hermes["override"] = tmp_path / "someone-else"
    assert home.owner_home(checkout) == hermes["process"]
    assert home.data_dir(checkout) == hermes["process"] / "plugin-data" / "meeting-scribe"


def test_one_root_install_with_a_declared_owner_gives_every_profile_the_owners_data(hermes):
    """The multi-profile shape: installed ONCE in <root>/plugins, owner named in the root config."""
    root = hermes["root"]
    install = root / "plugins" / "meeting-scribe"
    install.mkdir(parents=True)
    _declare(root, "team")
    for launched in (root, root / "profiles" / "team", root / "profiles" / "other"):
        hermes["process"] = launched  # a gateway or Desktop backend started with that profile
        assert home.data_dir(install) == root / "profiles" / "team" / "plugin-data" / "meeting-scribe"
        assert home.owner_profile(install) == "team"
    assert home.owner(install).declared is True


def test_without_a_declaration_a_root_install_is_owned_by_the_default_profile(hermes):
    root = hermes["root"]
    install = root / "plugins" / "meeting-scribe"
    install.mkdir(parents=True)
    assert home.owner(install) == home.Owner("default", root, False)
    _declare(root, "")
    assert home.owner_profile(install) == "default"


def test_a_declaration_can_name_the_default_profile(hermes):
    root = hermes["root"]
    _declare(root, "default")
    assert home.owner_home(root / "profiles" / "team" / "plugins" / "meeting-scribe") == root


@pytest.mark.parametrize("value, message", [("ghost", "does not exist"), ("../team", "not a profile name"),
                                            (["team"], "must be a profile name")])
def test_an_unusable_declaration_is_an_error_never_another_profiles_data(hermes, value, message):
    _declare(hermes["root"], value)
    with pytest.raises(home.OwnerError, match=message):
        home.owner()
    role = home.role(hermes["root"])
    assert role.owner is False and "nothing records until it is fixed" in role.detail


def test_role_says_who_records_and_where_to_run_commands(hermes):
    root = hermes["root"]
    install = root / "plugins" / "meeting-scribe"
    install.mkdir(parents=True)
    _declare(root, "team")
    owner = home.role(root / "profiles" / "team", install)
    assert owner.owner is True and "this is the owner" in owner.detail
    guest = home.role(root / "profiles" / "other", install)
    assert guest.owner is False
    assert "only serves the Desktop page" in guest.detail and "hermes -p team meeting-scribe" in guest.detail
    assert home.role(root, install).owner is False


def test_the_root_config_is_read_again_when_it_changes(hermes):
    root = hermes["root"]
    _declare(root, "team")
    assert home.owner_profile() == "team"
    _declare(root, "other", padding="x" * 10)
    assert home.owner_profile() == "other"


def test_the_package_resolves_its_own_install_root():
    assert (home.plugin_root() / "meeting_scribe" / "home.py").is_file()
