"""The listening copy of a meeting (what the Desktop player streams).

``audio.retention = multitrack`` keeps ``recording.mka`` (a Matroska file: the mix + one stream per
speaker, needed to reprocess from transcription). Browsers do not reliably play Matroska audio and
Hermes' generic media endpoint does not serve it, so the pipeline also writes ``playback.ogg``: the
archive's mix stream copied into an Ogg/Opus file (no re-encode; plays and seeks in Chromium). The
original stays untouched for download / reprocess. ``mixed`` retention's ``recording.ogg`` is already
that file. ``none`` keeps nothing, so there is nothing to play.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .ffmpeg import Ffmpeg, FfmpegError, run, stream_count

PLAYBACK = "playback.ogg"
MIXED = "recording.ogg"
MULTITRACK = "recording.mka"


class PlaybackError(RuntimeError):
    pass


def playable(folder: Path) -> Optional[Path]:
    """The file the player should stream, if the meeting has one (regular files only)."""
    for name in (PLAYBACK, MIXED):
        path = folder / name
        if path.is_file() and not path.is_symlink():
            return path
    return None


def build_playback(ff: Ffmpeg, folder: Path, *, timeout: float = 3600) -> Path:
    """Write ``playback.ogg`` from ``recording.mka`` (idempotent; atomic through a temp file)."""
    existing = playable(folder)
    if existing is not None:
        return existing
    source = folder / MULTITRACK
    if not source.is_file() or source.is_symlink():
        raise PlaybackError("no recording to prepare for listening")
    out = folder / PLAYBACK
    tmp = folder / ".playback.tmp.ogg"
    base = [str(ff.ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-map", "0:a:0",
            "-vn", "-map_metadata", "-1"]
    try:
        try:  # the mix is already Opus: a stream copy is instant and lossless
            run(base + ["-c:a", "copy", str(tmp)], timeout=timeout)
        except FfmpegError:  # an unusual source codec: re-encode once
            run(base + ["-c:a", "libopus", "-b:a", "48k", "-ac", "1", str(tmp)], timeout=timeout)
        if stream_count(ff, tmp) != 1:
            raise PlaybackError("the listening copy could not be verified")
        tmp.replace(out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return out
