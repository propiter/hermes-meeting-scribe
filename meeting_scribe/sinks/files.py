"""Files sink (always on): writes notes.md/notes.json/tasks.json into the meeting folder."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..config import Settings
from ..domain.models import Meeting, Notes, SinkResult
from ..storage.artifacts import write_notes


class FilesSink:
    name = "files"

    def __init__(self, settings: Callable[[str], Settings]) -> None:
        self._settings = settings

    def enabled(self, meeting: Meeting) -> bool:
        return True

    def deliver(self, meeting: Meeting, notes: Notes, folder: Path) -> SinkResult:
        write_notes(folder, meeting, notes, notes.language or self._settings(meeting.space).ui_language)
        return SinkResult(self.name, True, ("notes.md",))
