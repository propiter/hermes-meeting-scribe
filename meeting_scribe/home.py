"""THE resolver for which Hermes profile owns meeting-scribe and where its data lives (DESIGN §1.5).

One installation, one owner. The owner profile runs everything that writes: Discord capture, the
pipeline worker, the Google Meet pollers. Its home holds the data (``plugin-data/meeting-scribe``),
the settings (its ``config.yaml``) and the secrets. Any other profile with the plugin turned on only
serves the Desktop «Meetings» page: it reads the owner's data and queues commands for the owner's
worker. Nothing here follows a request's ``?profile=`` scope.

Which profile is the owner:

1. Declared: ``plugins.entries.meeting-scribe.owner_profile`` in the Hermes ROOT ``config.yaml`` (the
   default profile's file). Every profile can find the root, just as the Desktop backend scans the
   root ``plugins/`` folder whatever profile it was launched with. Required when the plugin is used
   from several profiles.
2. Otherwise, where the plugin is installed: ``<home>/plugins/meeting-scribe`` → ``<home>``. When the
   package runs from anywhere else (a development checkout, tests) the owner is the home the PROCESS
   was started with (``get_process_hermes_home``, which ignores per-request overrides).

A declaration that cannot be used raises :class:`OwnerError`; it never falls back to another profile.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

PLUGIN_ID = "meeting-scribe"
OWNER_KEY = "owner_profile"
OWNER_SETTING = f"plugins.entries.{PLUGIN_ID}.{OWNER_KEY}"

_ROOT_CONFIG_CACHE: dict[Path, tuple[tuple[int, int], Any]] = {}


class OwnerError(RuntimeError):
    """The declared owner cannot be used (not a profile name, missing profile, unreadable file)."""


@dataclass(frozen=True)
class Owner:
    profile: Optional[str]  # Hermes' profile id; None only for a checkout run from a non-profile home
    home: Path
    declared: bool  # True: set in the root config.yaml; False: taken from where the plugin is installed

    def describe(self) -> str:
        how = f"set by {OWNER_SETTING} in the Hermes root config.yaml" if self.declared else \
            "the profile where the plugin is installed"
        return f"profile '{self.profile or self.home}' ({how})"


def plugin_root() -> Path:
    """The plugin's install directory (the parent of the ``meeting_scribe`` package)."""
    return Path(__file__).resolve().parents[1]


def _root_config(root: Path) -> Any:
    """``<root>/config.yaml`` parsed; read again only when the file changes (the worker asks often)."""
    path = root / "config.yaml"
    try:
        st = path.stat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise OwnerError(f"cannot read {path}: {exc}") from exc
    stamp = (st.st_mtime_ns, st.st_size)
    cached = _ROOT_CONFIG_CACHE.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise OwnerError(f"cannot read {path}: {exc}") from exc
    _ROOT_CONFIG_CACHE[path] = (stamp, data)
    return data


def declared_owner() -> Optional[str]:
    """The profile named by ``owner_profile`` in the root config.yaml; None when it is not set."""
    from hermes_constants import get_default_hermes_root

    data = _root_config(get_default_hermes_root())
    value: Any = data
    for key in ("plugins", "entries", PLUGIN_ID, OWNER_KEY):
        value = value.get(key) if isinstance(value, dict) else None
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise OwnerError(f"{OWNER_SETTING} must be a profile name")
    return value.strip()


def _profile_home(name: str) -> Path:
    from hermes_constants import PROFILE_ID_RE, get_default_hermes_root

    root = get_default_hermes_root()
    if name == "default":
        return root
    if not PROFILE_ID_RE.match(name):
        raise OwnerError(f"{OWNER_SETTING} is {name!r}, which is not a profile name")
    home = root / "profiles" / name
    if not home.is_dir():
        raise OwnerError(f"{OWNER_SETTING} names the profile {name!r}, which does not exist ({home})")
    return home


def _install_home(root: Optional[Path]) -> Path:
    root = Path(root or plugin_root())
    if root.parent.name == "plugins":
        return root.parent.parent
    from hermes_constants import get_process_hermes_home

    return get_process_hermes_home()


def owner(root: Optional[Path] = None) -> Owner:
    """The owner: the declared profile, else the one where the plugin is installed."""
    name = declared_owner()
    if name:
        return Owner(name, _profile_home(name), True)
    from hermes_constants import profile_name_for_home

    home = _install_home(root)
    return Owner(profile_name_for_home(home), home, False)


def owner_home(root: Optional[Path] = None) -> Path:
    return owner(root).home


def data_dir(root: Optional[Path] = None) -> Path:
    """``<owner home>/plugin-data/meeting-scribe`` (created on demand)."""
    path = owner_home(root) / "plugin-data" / PLUGIN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def owner_profile(root: Optional[Path] = None) -> str:
    """Hermes' profile id of the owner (``default`` for the root home)."""
    found = owner(root)
    if not found.profile:
        raise OwnerError(f"meeting-scribe is installed under {found.home}, which is not a Hermes profile home")
    return found.profile


@dataclass(frozen=True)
class Role:
    owner: bool  # True: this profile runs capture, the worker and the pollers
    detail: str  # plain sentence for logs, the CLI and doctor


def role(home: Path, root: Optional[Path] = None) -> Role:
    """What the Hermes profile at ``home`` does with the plugin. An owner that cannot be resolved
    makes every profile a non-owner: nothing records until the declaration is fixed."""
    try:
        found = owner(root)
    except OwnerError as exc:
        return Role(False, f"{exc}; nothing records until it is fixed")
    if Path(home).resolve() == found.home.resolve():
        return Role(True, f"this is the owner: {found.describe()}")
    return Role(False, f"the owner is {found.describe()}; this profile only serves the Desktop page with "
                       f"the owner's meetings. Run meeting-scribe commands with "
                       f"`hermes -p {found.profile or 'default'} meeting-scribe …`")


@contextlib.contextmanager
def owner_scope(root: Optional[Path] = None) -> Iterator[str]:
    """Run the block as the owner profile: its config.yaml, its secrets (Hermes' own request scope,
    the same the core ``?profile=`` routes use). Nested scopes are replaced, not inherited."""
    from hermes_cli.web_server_profiles import _config_profile_scope

    name = owner_profile(root)
    with _config_profile_scope(name):
        yield name
