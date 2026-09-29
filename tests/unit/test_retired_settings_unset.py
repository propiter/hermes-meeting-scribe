"""Removing a value kept under a retired name (``delivery_project_threads``, DESIGN §16.1).

An older version wrote ``delivery_project_threads`` globally or as a space override; it keeps being
read as ``delivery_tasks_placement`` until removed. ``space unset``, ``config unset`` and the Desktop
"Use default" must be able to remove it.
"""
from __future__ import annotations

from meeting_scribe.desktop import settings as ds

from .pipeline.test_cli import run
from .test_cli_spaces import rt  # noqa: F401 - fixture
from .test_desktop_settings import MemSettings


def test_space_unset_removes_a_retired_override(rt, capsys):  # noqa: F811
    rt.spaces().create("Team", "team")
    rt.repo().set_space_override("team", "delivery_project_threads", False)  # written by the old version
    assert rt.settings("team").delivery_tasks_placement == "projects_inline"
    code, out = run(rt, ["space", "unset", "team", "delivery_project_threads"], capsys)
    assert code == 0 and "delivery_project_threads" in out
    assert rt.settings("team").delivery_tasks_placement == "meeting"
    assert "delivery_project_threads" not in rt.repo().get_space("team").overrides


def test_space_unset_of_the_new_key_also_removes_the_retired_one(rt, capsys):  # noqa: F811
    rt.spaces().create("Team", "team")
    rt.repo().set_space_override("team", "delivery_project_threads", True)
    run(rt, ["space", "unset", "team", "delivery_tasks_placement"], capsys)
    assert rt.settings("team").delivery_tasks_placement == "meeting"


def test_space_set_of_a_retired_key_still_answers_with_the_replacement(rt, capsys):  # noqa: F811
    rt.spaces().create("Team", "team")
    code, out = run(rt, ["space", "set", "team", "delivery_project_threads", "true"], capsys)
    assert code != 0 and "delivery_tasks_placement" in out


def test_config_unset_space_removes_a_retired_override(rt, capsys):  # noqa: F811
    rt.spaces().create("Team", "team")
    rt.repo().set_space_override("team", "delivery_project_threads", False)
    code, _ = run(rt, ["config", "unset", "delivery_project_threads", "--space", "team"], capsys)
    assert code == 0 and rt.settings("team").delivery_tasks_placement == "meeting"


def test_config_unset_removes_a_retired_global_value(rt, capsys):  # noqa: F811
    rt.host.set_config("delivery_project_threads", False)
    assert rt.settings().delivery_tasks_placement == "projects_inline"
    code, out = run(rt, ["config", "unset", "delivery_project_threads"], capsys)
    assert code == 0 and "delivery_project_threads" in out
    assert rt.settings().delivery_tasks_placement == "meeting"


def test_config_unset_of_the_new_key_removes_every_spelling(rt, capsys):  # noqa: F811
    rt.host.set_config("delivery_tasks_placement", "projects")
    rt.host.set_config("delivery_project_threads", False)
    run(rt, ["config", "unset", "delivery_tasks_placement"], capsys)
    assert rt.settings().delivery_tasks_placement == "meeting"
    assert rt.config_origin("delivery_tasks_placement") == "default"


def test_desktop_use_default_removes_a_retired_global_value():
    mem = MemSettings({"delivery_project_threads": False})
    store = mem.store()
    view = ds.settings_view(store, "en")["values"]["delivery_tasks_placement"]
    assert view == {"value": "projects_inline", "origin": "configured"}  # the page shows it
    out = ds.unset_setting(store, None, "delivery_tasks_placement")
    assert out["key"] == "delivery_tasks_placement" and mem.writes == [("delivery_project_threads", None)]
    assert ds.settings_view(store, "en")["values"]["delivery_tasks_placement"] == {"value": "meeting",
                                                                                   "origin": "default"}


def test_desktop_space_override_null_removes_a_retired_override(tmp_path):
    from meeting_scribe.storage.repo import Repository

    repo = Repository(tmp_path / "index.sqlite")
    try:
        repo.insert_space("team", "Team")
        repo.set_space_override("team", "delivery_project_threads", True)
        ds.set_space_setting(MemSettings().store(), repo, "team", "delivery_tasks_placement", None)
        row = repo.get_space("team")
        assert row is not None and row.overrides == {}
    finally:
        repo.close()
