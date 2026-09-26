import pytest

from meeting_scribe.audio import archive
from meeting_scribe.audio.ffmpeg import probe_duration, stream_count, stream_titles
from meeting_scribe.domain.models import Speaker

SPK = [Speaker("10", "Ana"), Speaker("11", "Luis Pérez")]


def test_multitrack_archive_has_mix_plus_one_stream_per_speaker(ff, make_track, tmp_path):
    tracks = {"10": make_track("10", 2.0, 440), "11": make_track("11", 3.0, 660)}
    out = archive.build_archive(ff, tracks, SPK, tmp_path, "multitrack", bitrate_kbps=48)
    assert out == tmp_path / "recording.mka"
    assert stream_count(ff, out) == 3
    assert stream_titles(ff, out) == ["Mix", "Ana (10)", "Luis Pérez (11)"]
    assert probe_duration(ff, out) == pytest.approx(3.0, abs=0.2)
    assert not (tmp_path / "tracks").exists()  # removed only after verification


def test_extract_speaker_stream_back(ff, make_track, tmp_path):
    tracks = {"10": make_track("10", 2.0), "11": make_track("11", 3.0)}
    out = archive.build_archive(ff, tracks, SPK, tmp_path, "multitrack", bitrate_kbps=48)
    restored = archive.extract_tracks(ff, out, tmp_path / "tracks")
    assert sorted(restored) == ["10", "11"]
    assert probe_duration(ff, restored["11"]) == pytest.approx(3.0, abs=0.2)


def test_mixed_archive(ff, make_track, tmp_path):
    tracks = {"10": make_track("10", 1.0), "11": make_track("11", 1.0)}
    out = archive.build_archive(ff, tracks, SPK, tmp_path, "mixed", bitrate_kbps=48)
    assert out.name == "recording.ogg" and stream_count(ff, out) == 1


def test_single_speaker_multitrack(ff, make_track, tmp_path):
    out = archive.build_archive(ff, {"10": make_track("10", 1.0)}, SPK[:1], tmp_path, "multitrack", 48)
    assert stream_count(ff, out) == 2


def test_none_retention_deletes_tracks(ff, make_track, tmp_path):
    make_track("10", 1.0)
    assert archive.build_archive(ff, {"10": tmp_path / "tracks" / "10.ogg"}, SPK, tmp_path, "none", 48) is None
    assert not (tmp_path / "tracks").exists()


def test_verification_failure_keeps_tracks(ff, make_track, tmp_path, monkeypatch):
    tracks = {"10": make_track("10", 1.0), "11": make_track("11", 1.0)}
    monkeypatch.setattr(archive, "stream_count", lambda *_a: 1)
    with pytest.raises(archive.ArchiveError):
        archive.build_archive(ff, tracks, SPK, tmp_path, "multitrack", 48)
    assert tracks["10"].exists()
    assert not (tmp_path / "recording.mka").exists()


def test_existing_verified_archive_is_idempotent(ff, make_track, tmp_path):
    tracks = {"10": make_track("10", 1.0)}
    out = archive.build_archive(ff, tracks, SPK[:1], tmp_path, "multitrack", 48)
    again = archive.build_archive(ff, {}, SPK[:1], tmp_path, "multitrack", 48)
    assert again == out
