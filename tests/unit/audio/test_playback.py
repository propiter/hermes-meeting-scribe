"""Listening copy of a multitrack archive (``playback.ogg``): what the Desktop player streams."""
import pytest

from meeting_scribe.audio import archive, playback
from meeting_scribe.audio.ffmpeg import probe_duration, stream_count, _probe
from meeting_scribe.domain.models import Speaker

SPK = [Speaker("10", "Ana"), Speaker("11", "Luis")]


def test_playback_is_the_mix_stream_as_ogg_opus(ff, make_track, tmp_path):
    tracks = {"10": make_track("10", 2.0, 440), "11": make_track("11", 3.0, 660)}
    mka = archive.build_archive(ff, tracks, SPK, tmp_path, "multitrack", bitrate_kbps=48)
    out = playback.build_playback(ff, tmp_path)
    assert out == tmp_path / "playback.ogg" and mka.exists()  # the original is kept
    assert stream_count(ff, out) == 1
    assert _probe(ff, out)["streams"][0]["codec_name"] == "opus"
    assert probe_duration(ff, out) == pytest.approx(3.0, abs=0.25)
    assert not list(tmp_path.glob(".playback*"))
    assert playback.build_playback(ff, tmp_path) == out  # idempotent


def test_mixed_retention_is_already_playable(ff, make_track, tmp_path):
    archive.build_archive(ff, {"10": make_track("10", 1.0)}, SPK, tmp_path, "mixed", bitrate_kbps=48)
    assert playback.playable(tmp_path) == tmp_path / "recording.ogg"
    assert playback.build_playback(ff, tmp_path) == tmp_path / "recording.ogg"
    assert not (tmp_path / "playback.ogg").exists()


def test_nothing_to_play(tmp_path, ff):
    assert playback.playable(tmp_path) is None
    with pytest.raises(playback.PlaybackError):
        playback.build_playback(ff, tmp_path)


def test_a_broken_archive_leaves_no_partial_file(ff, tmp_path):
    (tmp_path / "recording.mka").write_bytes(b"not a matroska file")
    with pytest.raises(Exception):
        playback.build_playback(ff, tmp_path)
    assert playback.playable(tmp_path) is None
    assert not list(tmp_path.glob("*.ogg")) and not list(tmp_path.glob(".playback*"))


def test_archiver_writes_the_listening_copy_after_a_multitrack_archive(ff, make_track, tmp_path, meeting):
    from meeting_scribe.config import settings_from_mapping
    from meeting_scribe.pipeline.stages import make_archiver

    make_track("10", 1.0)
    make_track("11", 1.5, 660)
    arch = make_archiver(lambda space=None: settings_from_mapping({"audio_retention": "multitrack"}), lambda: ff)
    assert arch(meeting, tmp_path) == tmp_path / "recording.mka"
    assert (tmp_path / "playback.ogg").is_file()


def test_archiver_survives_a_playback_failure(ff, make_track, tmp_path, meeting, monkeypatch):
    from meeting_scribe.config import settings_from_mapping
    from meeting_scribe.pipeline import stages

    make_track("10", 1.0)

    def boom(*a, **k):
        raise RuntimeError("encoder exploded")
    monkeypatch.setattr(stages, "build_playback", boom)
    arch = stages.make_archiver(lambda space=None: settings_from_mapping({"audio_retention": "multitrack"}), lambda: ff)
    assert arch(meeting, tmp_path) == tmp_path / "recording.mka"  # the meeting is not failed for it
    assert not (tmp_path / "playback.ogg").exists()
