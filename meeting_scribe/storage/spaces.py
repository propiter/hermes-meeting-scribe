"""Spaces in the index (DESIGN §23): one row per space, and the Discord servers each one owns.

A server belongs to at most one space (``space_guilds.guild_id`` is the primary key), so a voice
channel or a notes channel can never be claimed by two teams. ``settings`` holds the space's
overrides of the global plugin settings as JSON. ``adopt_guilds`` marks the space created from an
existing single-team setup: the first time the bot connects it takes the servers the bot is in.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from .result import Result


@dataclass(frozen=True)
class SpaceRow:
    slug: str
    name: str
    guilds: tuple[tuple[str, str], ...] = ()  # (guild id, last known server name)
    overrides: Mapping[str, Any] = field(default_factory=dict)
    adopt_guilds: bool = False

    @property
    def guild_ids(self) -> tuple[str, ...]:
        return tuple(g for g, _ in self.guilds)


class SpacesMixin:
    """Mixed into :class:`~meeting_scribe.storage.repo.Repository` (needs ``_x`` and ``transaction``)."""

    def _x(self, sql: str, params: Sequence[Any] = ()) -> Result:  # pragma: no cover - provided
        raise NotImplementedError

    def transaction(self) -> Any:  # pragma: no cover - provided
        raise NotImplementedError

    def _space(self, row: Any) -> SpaceRow:
        guilds = self._x("SELECT guild_id, name FROM space_guilds WHERE space=? ORDER BY name, guild_id",
                         (row["slug"],)).fetchall()
        return SpaceRow(row["slug"], row["name"], tuple((g["guild_id"], g["name"]) for g in guilds),
                        json.loads(row["settings"] or "{}"), bool(row["adopt_guilds"]))

    def list_spaces(self) -> list[SpaceRow]:
        return [self._space(r) for r in self._x("SELECT * FROM spaces ORDER BY created_at, slug").fetchall()]

    def get_space(self, slug: str) -> Optional[SpaceRow]:
        row = self._x("SELECT * FROM spaces WHERE slug=?", (slug,)).fetchone()
        return self._space(row) if row else None

    def insert_space(self, slug: str, name: str, *, adopt_guilds: bool = False) -> bool:
        """``False`` when the slug is taken."""
        cur = self._x("INSERT INTO spaces (slug, name, adopt_guilds, created_at) VALUES (?,?,?,?)"
                      " ON CONFLICT(slug) DO NOTHING", (slug, name, int(adopt_guilds), time.time()))
        return cur.rowcount == 1

    def rename_space(self, slug: str, name: str) -> bool:
        return self._x("UPDATE spaces SET name=? WHERE slug=?", (name, slug)).rowcount == 1

    def delete_space_row(self, slug: str) -> bool:
        return self._x("DELETE FROM spaces WHERE slug=?", (slug,)).rowcount == 1

    def space_of_guild(self, guild_id: str) -> Optional[str]:
        row = self._x("SELECT space FROM space_guilds WHERE guild_id=?", (str(guild_id),)).fetchone()
        return str(row["space"]) if row else None

    def assign_guild(self, slug: str, guild_id: str, name: str = "") -> Optional[str]:
        """Give server ``guild_id`` to space ``slug``; the space that already owns it (unchanged), or
        ``None`` when the assignment was made (or it was already ``slug``'s)."""
        with self.transaction():
            owner = self.space_of_guild(guild_id)
            if owner is not None and owner != slug:
                return owner
            self._x("INSERT INTO space_guilds (guild_id, space, name) VALUES (?,?,?) ON CONFLICT(guild_id)"
                    " DO UPDATE SET name=CASE WHEN excluded.name != '' THEN excluded.name ELSE name END",
                    (str(guild_id), slug, name))
            return None

    def release_guild(self, slug: str, guild_id: str) -> bool:
        return self._x("DELETE FROM space_guilds WHERE guild_id=? AND space=?", (str(guild_id), slug)).rowcount == 1

    def adopt_guilds(self, slug: str, guilds: Sequence[tuple[str, str]]) -> list[str]:
        """One-time adoption for a space created from an existing setup: take every listed server no
        other space owns, then clear the flag. The ids taken."""
        taken: list[str] = []
        with self.transaction():
            row = self._x("SELECT adopt_guilds FROM spaces WHERE slug=?", (slug,)).fetchone()
            if row is None or not row["adopt_guilds"]:
                return taken
            self._x("UPDATE spaces SET adopt_guilds=0 WHERE slug=?", (slug,))
            count = self._x("SELECT COUNT(*) FROM spaces").fetchone()[0]
            if slug != "main" or count != 1:
                return taken
            for gid, name in guilds:
                if self.space_of_guild(gid) is None:
                    self._x("INSERT INTO space_guilds (guild_id, space, name) VALUES (?,?,?)", (str(gid), slug, name))
                    taken.append(str(gid))
        return taken

    def set_space_override(self, slug: str, key: str, value: Any) -> None:
        """Set (or, with ``None``, clear) one override, read-modify-write under the write lock."""
        with self.transaction():
            row = self._x("SELECT settings FROM spaces WHERE slug=?", (slug,)).fetchone()
            if row is None:
                raise KeyError(slug)
            data = json.loads(row["settings"] or "{}")
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
            self._x("UPDATE spaces SET settings=? WHERE slug=?", (json.dumps(data, ensure_ascii=False), slug))
