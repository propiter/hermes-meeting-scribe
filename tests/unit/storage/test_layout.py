from pathlib import Path

from meeting_scribe.storage.layout import Layout


def test_meeting_folder_naming(tmp_path, meeting):
    layout = Layout(lambda: tmp_path)
    folder = layout.meeting_folder(meeting)
    assert folder == tmp_path / "meetings" / "2026" / "09" / "2026-09-26_1504_daily-sync_k3v7q2ab"
    assert layout.relative(folder) == "meetings/2026/09/2026-09-26_1504_daily-sync_k3v7q2ab"
    assert layout.resolve("meetings/2026/09/x") == tmp_path / "meetings/2026/09/x"


def test_paths_are_resolved_per_call(tmp_path, meeting):
    homes = iter([tmp_path / "a", tmp_path / "b"])
    layout = Layout(lambda: next(homes))
    assert layout.db_path().parent == tmp_path / "a"
    assert layout.db_path().parent == tmp_path / "b"


def test_tracks_dir_and_files(tmp_path, meeting):
    layout = Layout(lambda: tmp_path)
    folder = layout.meeting_folder(meeting)
    assert layout.tracks_dir(folder) == folder / "tracks"
    assert layout.track_path(folder, "10") == folder / "tracks" / "10.ogg"
    assert layout.archive_path(folder, "multitrack") == folder / "recording.mka"
    assert layout.archive_path(folder, "mixed") == folder / "recording.ogg"
    assert layout.archive_path(folder, "none") is None


def test_resolve_rejects_traversal(tmp_path):
    import pytest
    layout = Layout(lambda: tmp_path)
    with pytest.raises(ValueError):
        layout.resolve("../../etc")
