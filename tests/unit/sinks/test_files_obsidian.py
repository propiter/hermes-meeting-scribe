from dataclasses import replace

from meeting_scribe.sinks.files import FilesSink
from meeting_scribe.sinks.obsidian import ObsidianSink


def test_files_sink_writes_notes(tmp_path, meeting, notes, settings_of):
    folder = tmp_path / "m"
    res = FilesSink(settings_of(ui__language="es")).deliver(meeting, notes, folder)
    assert res.ok and (folder / "notes.md").exists() and (folder / "tasks.json").exists()


def test_obsidian_disabled_without_vault(settings_of):
    assert ObsidianSink(settings_of()).enabled() is False


def test_obsidian_copies_notes_idempotently(tmp_path, meeting, notes, settings_of):
    vault = tmp_path / "vault"
    vault.mkdir()
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / "notes.md").write_text("# hi\n")
    sink = ObsidianSink(settings_of(obsidian__vault_path=str(vault), obsidian__folder="Reuniones"))
    assert sink.enabled()
    stored = replace(meeting, folder="meetings/2026/09/2026-09-26_1504_daily-sync_k3v7q2ab")
    res = sink.deliver(stored, notes, folder)
    target = vault / "Reuniones" / "2026-09-26_1504_daily-sync_k3v7q2ab.md"
    assert res.ok and target.read_text() == "# hi\n"
    sink.deliver(replace(stored, title="Renamed after analysis"), notes, folder)
    assert len(list((vault / "Reuniones").iterdir())) == 1


def test_obsidian_missing_vault_is_error(tmp_path, meeting, notes, settings_of):
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / "notes.md").write_text("x")
    res = ObsidianSink(settings_of(obsidian__vault_path=str(tmp_path / "nope"))).deliver(meeting, notes, folder)
    assert not res.ok and "vault" in res.errors[0]


def test_obsidian_folder_cannot_escape_vault(tmp_path, meeting, notes, settings_of):
    vault = tmp_path / "vault"
    vault.mkdir()
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / "notes.md").write_text("x")
    res = ObsidianSink(settings_of(obsidian__vault_path=str(vault), obsidian__folder="../../etc")).deliver(
        meeting, notes, folder)
    assert not res.ok
