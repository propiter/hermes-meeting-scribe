"""The fully read outcome of one SQLite statement (see ``Repository._x``)."""
from __future__ import annotations

import sqlite3
from typing import Iterator, Optional


class Result:
    """Rows + affected count, read while the repository lock was held; safe to use unlocked."""

    __slots__ = ("_rows", "rowcount")

    def __init__(self, rows: list[sqlite3.Row], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self) -> Optional[sqlite3.Row]:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[sqlite3.Row]:
        return list(self._rows)

    def __iter__(self) -> Iterator[sqlite3.Row]:
        return iter(self._rows)
