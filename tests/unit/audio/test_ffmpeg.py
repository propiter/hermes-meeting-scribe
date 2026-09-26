import os
import stat
from pathlib import Path

import pytest

from meeting_scribe.audio import ffmpeg as ffm


def _fake_bin(dirpath: Path, name: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / name
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def test_resolution_order_path_first(tmp_path, monkeypatch):
    on_path = _fake_bin(tmp_path / "path", "ffmpeg")
    _fake_bin(tmp_path / "path", "ffprobe")
    monkeypatch.setenv("PATH", str(tmp_path / "path"))
    got = ffm.resolve_ffmpeg("", tools_dirs=[tmp_path / "tools"])
    assert got.ffmpeg == on_path and got.ffprobe == tmp_path / "path" / "ffprobe"


def test_resolution_falls_back_to_hermes_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    old = _fake_bin(tmp_path / "tools" / "ffmpeg-8.0.0-linux-x64" / "bin", "ffmpeg")
    new = _fake_bin(tmp_path / "tools" / "ffmpeg-9.0.1-linux-x64" / "bin", "ffmpeg")
    _fake_bin(new.parent, "ffprobe")
    got = ffm.resolve_ffmpeg("", tools_dirs=[tmp_path / "tools"])
    assert got.ffmpeg == new and old != new


def test_resolution_uses_configured_path_last(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    cfg = _fake_bin(tmp_path / "custom", "ffmpeg")
    _fake_bin(tmp_path / "custom", "ffprobe")
    assert ffm.resolve_ffmpeg(str(cfg), tools_dirs=[tmp_path / "none"]).ffmpeg == cfg


def test_not_found_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(ffm.FfmpegNotFound):
        ffm.resolve_ffmpeg("", tools_dirs=[tmp_path / "none"])


def test_real_ffmpeg_capabilities(ff):
    caps = ffm.capabilities(ff)
    assert caps["libopus"] is True and caps["version"]


def test_probe_duration_and_streams(ff, make_track):
    track = make_track("10", seconds=2.0)
    assert ffm.probe_duration(ff, track) == pytest.approx(2.0, abs=0.1)
    assert ffm.stream_count(ff, track) == 1


def test_decode_to_wav_16k_mono(ff, make_track, tmp_path):
    wav = ffm.decode_to_wav(ff, make_track("10", seconds=1.0), tmp_path / "out" / "10.wav")
    import wave
    with wave.open(str(wav)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        assert abs(w.getnframes() - 16000) < 400
