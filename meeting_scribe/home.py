"""THE resolver for where meeting-scribe keeps its data and reads its settings (DESIGN §1.5).

The plugin's data and configuration belong to the Hermes home that OWNS the plugin installation,
never to whichever profile a request happens to be scoped to. Hermes Desktop serves every profile
through one backend and passes the page's active profile as ``?profile=<name>``; resolving through
``get_hermes_home()`` under that scope made the «Meetings» page show an empty library as soon as
another profile was active. Anchoring to the install location gives the same spaces and meetings
whatever profile is active in Desktop.

Rule: the plugin lives at ``<home>/plugins/meeting-scribe`` → the owner home is ``<home>``. When the
package runs from anywhere else (a development checkout, tests) the owner is the home the PROCESS
was started with (``get_process_hermes_home``, which ignores per-request overrides), so it is
equally independent of request scoping.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Iterator, Optional

PLUGIN_ID = "meeting-scribe"


def plugin_root() -> Path:
    """The plugin's install directory (the parent of the ``meeting_scribe`` package)."""
    return Path(__file__).resolve().parents[1]


def owner_home(root: Optional[Path] = None) -> Path:
    """The Hermes home owning the installation at ``root`` (default: this package's install)."""
    root = Path(root or plugin_root())
    if root.parent.name == "plugins":
        return root.parent.parent
    from hermes_constants import get_process_hermes_home

    return get_process_hermes_home()


def data_dir(root: Optional[Path] = None) -> Path:
    """``<owner home>/plugin-data/meeting-scribe`` (created on demand)."""
    path = owner_home(root) / "plugin-data" / PLUGIN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def owner_profile(root: Optional[Path] = None) -> str:
    """Hermes' profile id of the owner home (``default`` for the root home)."""
    from hermes_constants import profile_name_for_home

    home = owner_home(root)
    name = profile_name_for_home(home)
    if not name:
        raise RuntimeError(f"meeting-scribe is installed under {home}, which is not a Hermes profile home")
    return name


@contextlib.contextmanager
def owner_scope(root: Optional[Path] = None) -> Iterator[str]:
    """Run the block as the owner profile: its config.yaml, its secrets (Hermes' own request scope,
    the same the core ``?profile=`` routes use). Nested scopes are replaced, not inherited."""
    from hermes_cli.web_server_profiles import _config_profile_scope

    name = owner_profile(root)
    with _config_profile_scope(name):
        yield name
