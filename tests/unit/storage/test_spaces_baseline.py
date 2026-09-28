"""Space bootstrap and storage isolation, with real databases and filesystem moves."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3

import pytest

from meeting_scribe.config import Settings, settings_from_mapping
from meeting_scribe.spaces import SpaceError, Spaces, bootstrap, move_google_files
from meeting_scribe.storage.baseline import backups
from meeting_scribe.storage.repo import SCHEMA_VERSION
from meeting_scribe.storage.repo import Repository


@pytest.fixture
def repo(tmp_path):
    instance = Repository(tmp_path / "index.sqlite")
    yield instance
    instance.close()


@pytest.mark.parametrize("language,name", [("en", "Main"), ("es", "Principal")])
def test_bootstrap_name_and_idempotence(repo, tmp_path, language, name):
    settings = settings_from_mapping({"ui_language": language})
    first = bootstrap(repo, settings, tmp_path)
    assert first.slug == "main"
    assert first.name == name
    assert first.adopt_guilds
    assert bootstrap(repo, settings, tmp_path) is None
    assert len(repo.list_spaces()) == 1


def test_google_move_preserves_every_loose_file_and_existing_space(repo, tmp_path):
    google = tmp_path / "google"
    google.mkdir()
    for name in ("client.json", "token.json", "token.json.lock", "other.json"):
        (google / name).write_text(name)
    (google / "other-space").mkdir()
    bootstrap(repo, Settings.defaults(), tmp_path)
    for name in ("client.json", "token.json", "token.json.lock", "other.json"):
        assert (google / "main" / name).read_text() == name
        assert not (google / name).exists()
    assert (google / "other-space").is_dir()
    bootstrap(repo, Settings.defaults(), tmp_path)
    assert (google / "main" / "token.json").read_text() == "token.json"


def test_google_move_resumes_staged_files(tmp_path):
    google = tmp_path / "google"
    stage = google / ".main.moving"
    stage.mkdir(parents=True)
    (stage / "client.json").write_text("client")
    (google / "token.json").write_text("token")
    move_google_files(google, "main")
    assert not stage.exists()
    assert (google / "main" / "client.json").read_text() == "client"
    assert (google / "main" / "token.json").read_text() == "token"


def test_space_crud_and_override_scopes(repo, tmp_path):
    bootstrap(repo, Settings.defaults(), tmp_path)
    spaces = Spaces(lambda: repo, lambda key, default=None: default)
    assert spaces.resolve(None).slug == "main"
    spaces.create("Team", "team")
    with pytest.raises(SpaceError, match="several spaces"):
        spaces.resolve(None)
    spaces.rename("team", "Team Two")
    spaces.set_override("team", "ui_language", "es")
    assert spaces.settings("team").ui_language == "es"
    assert spaces.settings("main").ui_language == "en"
    with pytest.raises(SpaceError, match="machine-wide"):
        spaces.set_override("team", "pipeline_workers", 4)
    spaces.set_override("team", "ui_language", None)
    assert spaces.settings("team").ui_language == "en"
    spaces.delete("team")
    assert spaces.resolve(None).slug == "main"


def test_guild_ownership_is_exclusive(repo, tmp_path):
    bootstrap(repo, Settings.defaults(), tmp_path)
    spaces = Spaces(lambda: repo, lambda key, default=None: default)
    spaces.create("Team", "team")
    spaces.add_guild("main", "100", "First server")
    with pytest.raises(SpaceError, match="already belongs"):
        spaces.add_guild("team", "100")
    assert spaces.for_guild("100").slug == "main"
    assert spaces.for_guild("999") is None
    spaces.remove_guild("main", "100")
    spaces.add_guild("team", "100")
    assert spaces.for_guild("100").slug == "team"


def test_adoption_only_when_main_is_the_only_space(repo, tmp_path):
    bootstrap(repo, Settings.defaults(), tmp_path)
    repo.insert_space("team", "Team")
    assert repo.adopt_guilds("main", [("100", "Server")]) == []
    assert repo.space_of_guild("100") is None


def test_bootstrap_concurrent_creates_once(repo, tmp_path):
    with ThreadPoolExecutor(max_workers=4) as workers:
        created = list(workers.map(lambda _: bootstrap(repo, Settings.defaults(), tmp_path), range(8)))
    assert sum(value is not None for value in created) == 1
    assert len(repo.list_spaces()) == 1


def test_storage_queries_are_scoped_and_nonempty_space_cannot_be_deleted(repo, tmp_path, meeting, utterances):
    bootstrap(repo, Settings.defaults(), tmp_path)
    spaces = Spaces(lambda: repo, lambda key, default=None: default)
    spaces.create("Team", "team")
    first = replace(meeting, space="main")
    second = replace(meeting, id="other123", space="team")
    for item in (first, second):
        repo.save_meeting(item)
        repo.replace_utterances(item.id, utterances)
    assert [m.id for m in repo.list_meetings(space="team")] == [second.id]
    assert repo.find_meeting(first.id, "team") is None
    assert {r["meeting_id"] for r in repo.search("SMTP", "main")} == {first.id}
    with pytest.raises(SpaceError, match="only be deleted when empty"):
        spaces.delete("team")


@pytest.mark.parametrize("version", [1, 8, 99])
def test_legacy_backup_keeps_database_and_meeting_files(tmp_path, version):
    path = tmp_path / "index.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE evidence(value TEXT)")
    conn.execute("INSERT INTO evidence VALUES ('preserved')")
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    conn.close()
    meetings = tmp_path / "meetings"
    meetings.mkdir()
    (meetings / "notes.md").write_text("notes")
    repo = Repository(path)
    try:
        assert repo.user_version() == SCHEMA_VERSION
        saved = backups(tmp_path)
        assert len(saved) == 1
        old = sqlite3.connect(saved[0] / "index.sqlite")
        try:
            assert old.execute("SELECT value FROM evidence").fetchone()[0] == "preserved"
        finally:
            old.close()
        assert (saved[0] / "meetings" / "notes.md").read_text() == "notes"
        assert repo.list_meetings() == []
    finally:
        repo.close()
    reopened = Repository(path)
    reopened.close()
    assert len(backups(tmp_path)) == 1
