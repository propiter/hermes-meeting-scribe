"""Obsidian sink: copies notes.md into ``<vault>/<obsidian.folder>/<meeting folder name>.md``.

Overwriting the same file name makes it idempotent and lets reprocess refresh the note."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..config import Settings
from ..domain.models import Meeting, Notes, SinkResult
from ..storage.artifacts import atomic_write_text


class ObsidianSink:
    name = "obsidian"

    def __init__(self, settings: Callable[[], Settings]) -> None:
        self._settings = settings

    def enabled(self) -> bool:
        return bool(self._settings().obsidian_vault_path.strip())

    def deliver(self, meeting: Meeting, notes: Notes, folder: Path) -> SinkResult:
        s = self._settings()
        vault = Path(s.obsidian_vault_path).expanduser()
        if not vault.is_dir():
            return SinkResult(self.name, False, errors=(f"obsidian vault not found: {vault}",))
        target_dir = (vault / s.obsidian_folder).resolve()
        if vault.resolve() not in (target_dir, *target_dir.parents):
            return SinkResult(self.name, False, errors=(f"obsidian.folder escapes the vault: {s.obsidian_folder}",))
        name = Path(meeting.folder).name if meeting.folder else folder.name
        target = target_dir / f"{name}.md"
        atomic_write_text(target, (folder / "notes.md").read_text(encoding="utf-8"))
        return SinkResult(self.name, True, (str(target),))
