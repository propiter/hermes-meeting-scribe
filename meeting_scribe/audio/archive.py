"""Final audio packaging per ``audio.retention`` (DESIGN §5).

``multitrack`` → ``recording.mka``: stream 0 is an ``amix`` of every speaker (default track,
plays anywhere), then one Opus stream per speaker titled ``Name (id)`` so ``reprocess
from=transcribe`` can extract them again. Speaker tracks are copied (no re-encode). The per-speaker
``tracks/`` directory is deleted only after ffprobe confirms the expected stream count.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Mapping, Optional, Sequence

from ..domain.models import Speaker
from .ffmpeg import Ffmpeg, run, stream_count, stream_titles

_TITLE_ID_RE = re.compile(r"\(([^()]+)\)\s*$")


class ArchiveError(RuntimeError):
    pass


def _title(speaker_id: str, speakers: Sequence[Speaker]) -> str:
    name = next((s.name for s in speakers if s.user_id == speaker_id), speaker_id)
    return f"{name} ({speaker_id})"


def _mix_args(n: int) -> list[str]:
    if n == 1:
        return ["-map", "0:a:0"]
    inputs = "".join(f"[{i}:a:0]" for i in range(n))
    return ["-filter_complex", f"{inputs}amix=inputs={n}:duration=longest:normalize=0[mix]", "-map", "[mix]"]


def build_archive(ff: Ffmpeg, tracks: Mapping[str, Path], speakers: Sequence[Speaker], folder: Path,
                  retention: str, bitrate_kbps: int, timeout: float = 7200) -> Optional[Path]:
    """Write the archive and remove ``tracks/``. Idempotent: an existing verified archive with no
    tracks left is returned as-is (resume after a crash between write and cleanup)."""
    tracks_dir = folder / "tracks"
    if retention == "none":
        shutil.rmtree(tracks_dir, ignore_errors=True)
        return None
    out = folder / ("recording.mka" if retention == "multitrack" else "recording.ogg")
    ids = sorted(tracks)
    if not ids:
        if out.exists():
            return out
        raise ArchiveError("no tracks to archive")
    expected = len(ids) + 1 if retention == "multitrack" else 1
    tmp = out.with_name(f".{out.stem}.tmp{out.suffix}")
    args = [str(ff.ffmpeg), "-hide_banner", "-loglevel", "error", "-y"]
    for sid in ids:
        args += ["-i", str(tracks[sid])]
    args += _mix_args(len(ids)) + ["-c:a:0", "libopus", "-b:a:0", f"{bitrate_kbps}k", "-ac:a:0", "1"]
    if retention == "multitrack":
        args += ["-metadata:s:a:0", "title=Mix", "-disposition:a:0", "default"]
        for i, sid in enumerate(ids, start=1):
            args += ["-map", f"{i - 1}:a:0", f"-c:a:{i}", "copy", f"-metadata:s:a:{i}",
                     f"title={_title(sid, speakers)}", f"-disposition:a:{i}", "0"]
    try:
        run(args + [str(tmp)], timeout=timeout)
        if stream_count(ff, tmp) != expected:
            raise ArchiveError(f"archive verification failed: expected {expected} audio streams")
        tmp.replace(out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    shutil.rmtree(tracks_dir, ignore_errors=True)
    return out


def extract_tracks(ff: Ffmpeg, archive: Path, dest: Path, timeout: float = 3600) -> dict[str, Path]:
    """Restore ``tracks/<id>.ogg`` from a multitrack archive (stream copy, lossless)."""
    titles = stream_titles(ff, archive)
    dest.mkdir(parents=True, exist_ok=True)
    restored: dict[str, Path] = {}
    for idx, title in enumerate(titles):
        if idx == 0:
            continue
        m = _TITLE_ID_RE.search(title)
        sid = m.group(1) if m else f"stream{idx}"
        out = dest / f"{sid}.ogg"
        run([str(ff.ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(archive), "-map",
             f"0:a:{idx}", "-c:a", "copy", str(out)], timeout=timeout)
        restored[sid] = out
    if not restored:
        raise ArchiveError(f"{archive.name} has no per-speaker streams (mixed archive?)")
    return restored
