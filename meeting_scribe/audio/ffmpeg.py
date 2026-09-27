"""ffmpeg binary resolution and thin, typed wrappers.

Resolution order: ``PATH`` → Hermes-managed tools (``~/.hermes/tools/ffmpeg-*/bin``, newest
version first) → ``audio_ffmpeg_path`` config. PATH wins so a system ffmpeg the user maintains is
preferred; the config path is the explicit escape hatch when neither exists.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

_TIMEOUT = 120


class FfmpegNotFound(RuntimeError):
    pass


class FfmpegError(RuntimeError):
    pass


@dataclass(frozen=True)
class Ffmpeg:
    ffmpeg: Path
    ffprobe: Path


def _version_key(path: Path) -> tuple[int, ...]:
    m = re.search(r"ffmpeg-(\d+(?:\.\d+)*)", str(path))
    return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)


def default_tools_dirs() -> list[Path]:
    """Hermes-managed tool roots. The *default* root is used on purpose: tools are shared by
    all profiles (``~/.hermes/tools``), unlike profile data."""
    return [Path.home() / ".hermes" / "tools"]


def _pair(ffmpeg: Path) -> Optional[Ffmpeg]:
    probe = ffmpeg.with_name("ffprobe")
    if not probe.exists():
        found = shutil.which("ffprobe")
        probe = Path(found) if found else probe
    return Ffmpeg(ffmpeg, probe) if ffmpeg.exists() and probe.exists() else None


def resolve_ffmpeg(configured: str = "", tools_dirs: Optional[Iterable[Path]] = None) -> Ffmpeg:
    on_path = shutil.which("ffmpeg")
    if on_path and (pair := _pair(Path(on_path))):
        return pair
    candidates: list[Path] = []
    for root in tools_dirs if tools_dirs is not None else default_tools_dirs():
        candidates += list(Path(root).glob("ffmpeg-*/bin/ffmpeg"))
    for cand in sorted(candidates, key=_version_key, reverse=True):
        if pair := _pair(cand):
            return pair
    if configured and (pair := _pair(Path(configured).expanduser())):
        return pair
    raise FfmpegNotFound("ffmpeg/ffprobe not found (PATH, ~/.hermes/tools/ffmpeg-*/bin, audio_ffmpeg_path)")


def run(args: Sequence[str], timeout: float = _TIMEOUT) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise FfmpegError(f"{Path(args[0]).name} failed ({proc.returncode}): {proc.stderr.strip()[-800:]}")
    return proc


def capabilities(ff: Ffmpeg) -> dict[str, object]:
    version = run([str(ff.ffmpeg), "-hide_banner", "-version"]).stdout.splitlines()[0]
    encoders = run([str(ff.ffmpeg), "-hide_banner", "-encoders"]).stdout
    return {"version": version, "libopus": bool(re.search(r"\blibopus\b", encoders))}


def _probe(ff: Ffmpeg, path: Path) -> dict:
    out = run([str(ff.ffprobe), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)])
    return json.loads(out.stdout or "{}")


def probe_duration(ff: Ffmpeg, path: Path) -> float:
    return float(_probe(ff, path).get("format", {}).get("duration") or 0.0)


def stream_count(ff: Ffmpeg, path: Path) -> int:
    return len([s for s in _probe(ff, path).get("streams", []) if s.get("codec_type") == "audio"])


def stream_titles(ff: Ffmpeg, path: Path) -> list[str]:
    return [(s.get("tags") or {}).get("title") or (s.get("tags") or {}).get("TITLE") or ""
            for s in _probe(ff, path).get("streams", []) if s.get("codec_type") == "audio"]


def decode_to_wav(ff: Ffmpeg, src: Path, dst: Path, *, stream: Optional[int] = None,
                  timeout: float = 3600) -> Path:
    """16 kHz mono PCM wav, the format whisper consumes without resampling in-process."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.wav")
    args = [str(ff.ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    if stream is not None:
        args += ["-map", f"0:a:{stream}"]
    run(args + ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp)], timeout=timeout)
    tmp.replace(dst)
    return dst
