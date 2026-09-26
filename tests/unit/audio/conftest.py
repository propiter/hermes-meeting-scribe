import subprocess
from pathlib import Path

import pytest

from meeting_scribe.audio.ffmpeg import FfmpegNotFound, resolve_ffmpeg


@pytest.fixture(scope="session")
def ff():
    try:
        return resolve_ffmpeg("")
    except FfmpegNotFound:
        pytest.skip("ffmpeg not available")


@pytest.fixture
def make_track(ff, tmp_path):
    def _make(name: str, seconds: float = 2.0, freq: int = 440, silence: bool = False) -> Path:
        out = tmp_path / "tracks" / f"{name}.ogg"
        out.parent.mkdir(parents=True, exist_ok=True)
        src = f"anullsrc=r=48000:cl=mono" if silence else f"sine=frequency={freq}:sample_rate=48000"
        subprocess.run([str(ff.ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-t",
                        str(seconds), "-i", src, "-ac", "1", "-c:a", "libopus", "-b:a", "48k", str(out)], check=True)
        return out
    return _make
