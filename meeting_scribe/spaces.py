"""Spaces (DESIGN §23): one team or client inside one installation, like the boards of Hermes Kanban.

A space owns its Discord servers, its Google Meet connection, its delivery destinations, projects,
language and task-sink settings, and its meetings. Nothing crosses spaces: a meeting is published
only to channels of its own space's servers, and every list, search, facet, task and transcript is
read within one space. The Discord bot is shared by every space; a server belongs to at most one
space, and a server that belongs to none is not recorded.

Settings are layered: the global plugin settings (``plugins.entries.meeting-scribe.settings``) are
the defaults, and a space stores overrides for the keys whose ``Opt.scope`` is ``space``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .config import SPEC, Getter, Settings, canonical_key, validate_value
from .filelock import file_lock
from .storage.spaces import SpaceRow

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
FIRST_SLUG = "main"
_NAME_MAX = 60


class SpaceError(ValueError):
    """A space operation that cannot be done (unknown slug, taken server, not empty...)."""


def check_slug(slug: str) -> str:
    value = (slug or "").strip().lower()
    if not SLUG_RE.match(value):
        raise SpaceError(f"invalid space id {slug!r}: use 1-32 lowercase letters, digits or '-'")
    return value


def check_name(name: str) -> str:
    value = " ".join((name or "").split())
    if not value or len(value) > _NAME_MAX:
        raise SpaceError(f"a space name needs 1-{_NAME_MAX} characters")
    return value


def slug_for(name: str) -> str:
    """A slug derived from a display name (``Acme Corp`` → ``acme-corp``)."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32].strip("-")
    return base or "space"


@dataclass(frozen=True)
class Space:
    slug: str
    name: str
    guilds: tuple[tuple[str, str], ...]
    overrides: dict[str, Any]
    adopt_guilds: bool = False

    @property
    def guild_ids(self) -> tuple[str, ...]:
        return tuple(g for g, _ in self.guilds)

    def to_dict(self) -> dict[str, Any]:
        return {"slug": self.slug, "name": self.name, "adopt_guilds": self.adopt_guilds,
                "guilds": [{"id": g, "name": n} for g, n in self.guilds], "overrides": dict(self.overrides)}

    @classmethod
    def of(cls, row: SpaceRow) -> "Space":
        return cls(row.slug, row.name, row.guilds, dict(row.overrides), row.adopt_guilds)


class Spaces:
    """Space operations over the index (``repo``) and the global settings (``getter``)."""

    def __init__(self, repo: Callable[[], Any], getter: Getter) -> None:
        self._repo = repo
        self._getter = getter

    @property
    def repo(self) -> Any:
        return self._repo()

    # -- reading --------------------------------------------------------------------------------
    def all(self) -> list[Space]:
        return [Space.of(r) for r in self.repo.list_spaces()]

    def get(self, slug: str) -> Optional[Space]:
        row = self.repo.get_space(slug)
        return Space.of(row) if row else None

    def require(self, slug: str) -> Space:
        space = self.get(slug)
        if space is None:
            raise SpaceError(f"unknown space {slug!r}")
        return space

    def resolve(self, slug: Optional[str]) -> Space:
        """``slug``'s space; without one, the only space (an install with several must say which)."""
        if slug:
            return self.require(check_slug(slug))
        spaces = self.all()
        if len(spaces) == 1:
            return spaces[0]
        if not spaces:
            raise SpaceError("no space exists yet")
        raise SpaceError("several spaces exist; choose one with --space (" +
                         ", ".join(s.slug for s in spaces) + ")")

    def for_guild(self, guild_id: Any) -> Optional[Space]:
        slug = self.repo.space_of_guild(str(guild_id)) if guild_id not in (None, "") else None
        return self.get(slug) if slug else None

    def settings(self, space: Optional[str] = None) -> Settings:
        """The global settings, with ``space``'s overrides on top (``None``/"": the global values)."""
        if not space:
            return Settings.load(self._getter)
        row = self.get(space)
        return Settings.load(self._getter, space, row.overrides if row else None)

    # -- writing --------------------------------------------------------------------------------
    def ensure_first(self, name: str) -> Optional[Space]:
        """Create the first space when there is none (an existing setup keeps working: it inherits the
        global settings and adopts the bot's servers on the next connect). The space created, if any."""
        with self.repo.transaction():
            if self.repo.list_spaces():
                return None
            self.repo.insert_space(FIRST_SLUG, check_name(name), adopt_guilds=True)
            return self.get(FIRST_SLUG)

    def create(self, name: str, slug: Optional[str] = None) -> Space:
        name = check_name(name)
        slug = check_slug(slug or slug_for(name))
        if not self.repo.insert_space(slug, name):
            raise SpaceError(f"a space {slug!r} already exists")
        return self.require(slug)

    def rename(self, slug: str, name: str) -> Space:
        if not self.repo.rename_space(check_slug(slug), check_name(name)):
            raise SpaceError(f"unknown space {slug!r}")
        return self.require(slug)

    def delete(self, slug: str) -> None:
        """Only an empty space can be deleted: meetings are never orphaned or moved implicitly."""
        space = self.require(check_slug(slug))
        count = self.repo.space_meeting_count(space.slug)
        if count:
            raise SpaceError(f"space {slug!r} still has {count} meeting(s); it can only be deleted when empty")
        self.repo.delete_space_row(space.slug)

    def add_guild(self, slug: str, guild_id: str, name: str = "") -> Space:
        gid = str(guild_id).strip()
        if not gid.isdigit():
            raise SpaceError(f"expected a Discord server id, got {guild_id!r}")
        space = self.require(check_slug(slug))
        owner = self.repo.assign_guild(space.slug, gid, name)
        if owner is not None:
            raise SpaceError(f"server {gid} already belongs to space {owner!r}; remove it there first")
        return self.require(space.slug)

    def remove_guild(self, slug: str, guild_id: str) -> Space:
        space = self.require(check_slug(slug))
        if not self.repo.release_guild(space.slug, str(guild_id).strip()):
            raise SpaceError(f"server {guild_id} is not in space {slug!r}")
        return self.require(space.slug)

    def set_override(self, slug: str, key: str, raw: Any) -> tuple[str, Any]:
        """Validate with the global rules and store ``slug``'s override; ``raw=None`` clears it."""
        key = canonical_key(key)
        if SPEC[key].scope != "space":
            raise SpaceError(f"{key} is a machine-wide setting; it cannot differ per space")
        value = None if raw is None else validate_value(key, raw)
        self.repo.set_space_override(self.require(check_slug(slug)).slug, key, value)
        return key, value


def first_space_name(language: str) -> str:
    from .i18n import t

    return t("space.first_name", language)


def bootstrap(repo: Any, settings: Settings, data_dir: Path) -> Optional[Space]:
    """First start with spaces: create ``main`` from the current setup (it inherits the global settings
    and adopts the bot's servers on its first connect) and move the Google files kept directly under
    ``<data>/google/`` into ``google/main/``. Idempotent; the space created, if any."""
    created = Spaces(lambda: repo, lambda key, default=None: default).ensure_first(
        first_space_name(settings.ui_language))
    move_google_files(data_dir / "google", FIRST_SLUG)
    return created


def move_google_files(google_dir: Path, slug: str) -> None:
    """Move the loose files of ``google_dir`` into ``google_dir/<slug>/`` as one step: they are gathered
    in a staging folder first, which is then renamed (a crash leaves the staging folder, finished on the
    next start). Nothing happens when the space already has its folder."""
    target = google_dir / slug
    staging = google_dir / f".{slug}.moving"
    if target.exists() or not google_dir.is_dir():
        return
    loose = [p for p in google_dir.iterdir() if p.is_file()]
    if not loose and not staging.is_dir():
        return
    with file_lock(google_dir.parent / "google.move.lock"):
        if target.exists():
            return
        staging.mkdir(mode=0o700, exist_ok=True)
        for path in google_dir.iterdir():
            if path.is_file():
                path.rename(staging / path.name)
        staging.rename(target)
