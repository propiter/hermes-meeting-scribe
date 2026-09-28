"""Filesystem layout under ``<HERMES_HOME>/plugin-data/meeting-scribe/`` (DESIGN §9).

The data root is obtained from a callable on *every* call (``plugin_data_dir`` in production)
because one gateway process can serve several profiles; caching the path at import would write
one profile's meetings into another's home.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from ..domain.ids import slugify
from ..domain.models import Meeting

RootGetter = Callable[[], Path]


class Layout:
    def __init__(self, root: RootGetter) -> None:
        self._root = root

    def root(self) -> Path:
        return Path(self._root())

    def db_path(self) -> Path:
        return self.root() / "index.sqlite"

    def meetings_dir(self) -> Path:
        return self.root() / "meetings"

    def folder_name(self, meeting: Meeting) -> str:
        ts = meeting.started_at
        return f"{ts:%Y-%m-%d_%H%M}_{slugify(meeting.title or meeting.channel_name)}_{meeting.id}"

    def meeting_folder(self, meeting: Meeting) -> Path:
        """Stored folder when known (titles change after analysis), else the canonical name."""
        if meeting.folder:
            return self.resolve(meeting.folder)
        ts = meeting.started_at
        if not meeting.space:
            raise ValueError(f"meeting {meeting.id} has no space")
        return self.meetings_dir() / meeting.space / f"{ts:%Y}" / f"{ts:%m}" / self.folder_name(meeting)

    def relative(self, folder: Path) -> str:
        return folder.resolve().relative_to(self.root().resolve()).as_posix()

    def resolve(self, relative: str) -> Path:
        root = self.root().resolve()
        path = (root / relative).resolve()
        if root != path and root not in path.parents:
            raise ValueError(f"path {relative!r} escapes the plugin data directory")
        return path

    @staticmethod
    def tracks_dir(folder: Path) -> Path:
        return folder / "tracks"

    @staticmethod
    def track_path(folder: Path, user_id: str) -> Path:
        return folder / "tracks" / f"{user_id}.ogg"

    @staticmethod
    def archive_path(folder: Path, retention: str) -> Optional[Path]:
        return {"multitrack": folder / "recording.mka", "mixed": folder / "recording.ogg"}.get(retention)

    @staticmethod
    def work_dir(folder: Path) -> Path:
        """Scratch for decoded wavs / worker job files; deleted after a successful transcription."""
        return folder / ".work"
