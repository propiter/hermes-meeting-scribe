import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.storage.repo import Repository


@pytest.fixture
def repo(tmp_path, meeting):
    r = Repository(tmp_path / "db" / "index.sqlite")
    r.save_meeting(meeting)
    yield r
    r.close()


@pytest.fixture
def settings_of():
    return lambda **kw: (lambda: settings_from_mapping({k.replace("__", "."): v for k, v in kw.items()}))
