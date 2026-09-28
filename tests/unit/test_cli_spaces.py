"""``hermes meeting-scribe space …`` and ``--space`` on the other commands (DESIGN §23), on a real Runtime."""
from __future__ import annotations

import argparse
import json
from dataclasses import replace

import pytest

from meeting_scribe import cli
from meeting_scribe.domain.models import MeetingState

from .test_spaces_isolation import runtime


def parse(argv):
    parser = argparse.ArgumentParser(prog="hermes meeting-scribe")
    cli.setup_parser(parser)
    return parser.parse_args(argv)


def run(rt, argv, capsys):
    code = cli.dispatch(parse(argv), rt)
    return code, capsys.readouterr().out


@pytest.fixture
def rt(tmp_path):
    r = runtime(tmp_path)
    yield r
    r.close()


@pytest.fixture
def two(rt, meeting):
    rt.spaces().create("Team", "team")
    rt.spaces().add_guild("team", "200", "Beta")
    repo = rt.repo()
    repo.save_meeting(replace(meeting, id="main0001", state=MeetingState.DONE, space="main", title="Main talk"))
    repo.save_meeting(replace(meeting, id="team0001", guild_id="200", state=MeetingState.DONE, space="team",
                              title="Team talk"))
    return rt


def test_create_rename_list_show_delete(rt, capsys):
    code, out = run(rt, ["space", "create", "Acme Corp"], capsys)
    assert code == 0 and "acme-corp" in out
    assert run(rt, ["space", "create", "Acme Corp"], capsys)[0] == 2  # slug taken
    assert run(rt, ["space", "rename", "acme-corp", "Acme"], capsys)[0] == 0
    code, out = run(rt, ["space", "list", "--json"], capsys)
    data = json.loads(out)
    assert [s["slug"] for s in data["spaces"]] == ["main", "acme-corp"]
    assert data["spaces"][1]["name"] == "Acme" and data["spaces"][1]["counts"]["meetings"] == 0
    assert data["spaces"][1]["google"]["connected"] is False
    code, out = run(rt, ["space", "show", "acme-corp"], capsys)
    assert code == 0 and "Acme" in out
    assert run(rt, ["space", "show", "ghost"], capsys)[0] == 2
    code, out = run(rt, ["space", "delete", "acme-corp"], capsys)
    assert code == 0 and rt.spaces().get("acme-corp") is None
    code, out = run(rt, ["space", "delete", "main"], capsys)
    assert code == 2 and "only one" in out  # the last space stays


def test_delete_refuses_a_space_with_meetings_and_cleans_an_empty_one(two, capsys, tmp_path):
    code, out = run(two, ["space", "delete", "team"], capsys)
    assert code == 2 and "1 meeting" in out
    two.spaces().create("Gone", "gone")
    two.spaces().add_guild("gone", "300")
    two.service().link("gone", "10", "ana@example.com")
    two.repo().kv_set("google.gone.last_poll_at", "x")
    (tmp_path / "data" / "google" / "gone").mkdir(parents=True)
    assert run(two, ["space", "delete", "gone"], capsys)[0] == 0
    repo = two.repo()
    assert repo.space_of_guild("300") is None and repo.kv_prefix("google.gone.") == {}
    assert repo._x("SELECT COUNT(*) FROM links WHERE space='gone'").fetchone()[0] == 0
    assert not (tmp_path / "data" / "google" / "gone").exists()


def test_guilds_are_assigned_to_one_space_only(two, capsys):
    code, out = run(two, ["space", "add-guild", "main", "200"], capsys)
    assert code == 2 and "team" in out
    assert run(two, ["space", "add-guild", "main", "abc"], capsys)[0] == 2
    two.repo().set_bot_guilds([("400", "Gamma"), ("200", "Beta")])
    code, out = run(two, ["space", "add-guild", "main", "400"], capsys)
    assert code == 0 and "Gamma" in out  # the name the bot last saw
    assert run(two, ["space", "remove-guild", "team", "200"], capsys)[0] == 0
    code, out = run(two, ["space", "list"], capsys)
    assert "Beta (200)" in out and "belongs to no space" in out  # the bot's unassigned server
    assert run(two, ["space", "remove-guild", "team", "200"], capsys)[0] == 2


def test_overrides_set_unset_and_global_keys_refused(two, capsys):
    code, out = run(two, ["space", "set", "team", "ui_language", "es"], capsys)
    assert code == 0 and two.settings("team").ui_language == "es" and two.settings("main").ui_language == "en"
    code, out = run(two, ["space", "set", "team", "pipeline_workers", "4"], capsys)
    assert code == 2 and "machine-wide" in out and "config set pipeline_workers" in out
    code, out = run(two, ["space", "set", "team", "kanban_mode", "bogus"], capsys)
    assert code == 2
    assert run(two, ["space", "set", "team", "nope", "1"], capsys)[0] == 2
    code, out = run(two, ["space", "show", "team"], capsys)
    assert "ui_language = es" in out
    assert run(two, ["space", "unset", "team", "ui_language"], capsys)[0] == 0
    assert two.settings("team").ui_language == "en"


def test_config_with_space(two, capsys):
    assert run(two, ["config", "set", "ui_language", "es", "--space", "team"], capsys)[0] == 0
    assert two.spaces().get("team").overrides == {"ui_language": "es"}
    assert two.settings().ui_language == "en"  # the global value is untouched
    code, out = run(two, ["config", "set", "pipeline_workers", "3", "--space", "team"], capsys)
    assert code == 2 and "machine-wide" in out
    assert run(two, ["config", "set", "ui_language", "es", "--space", "ghost"], capsys)[0] == 2
    code, out = run(two, ["config", "get", "ui_language", "--space", "team"], capsys)
    assert out.strip() == "es"
    code, out = run(two, ["config", "get", "ui_language"], capsys)  # several spaces: every value
    assert "en  (global)" in out and "team: es" in out
    code, out = run(two, ["config", "list", "--json", "--space", "team"], capsys)
    rows = {r["key"]: r for r in json.loads(out)["settings"]}
    assert rows["ui_language"]["origin"] == "space" and rows["ui_language"]["value"] == "es"
    assert rows["pipeline_workers"]["scope"] == "global"
    code, out = run(two, ["config", "list", "--json"], capsys)
    assert json.loads(out)["overrides"]["team"] == {"ui_language": "es"}
    code, out = run(two, ["config", "list"], capsys)
    assert "[space team: Team]" in out


def test_views_show_every_space_and_actions_require_one(two, capsys):
    code, out = run(two, ["list"], capsys)
    assert code == 0 and "main0001  main" in out and "team0001  team" in out
    code, out = run(two, ["list", "--space", "team"], capsys)
    assert "team0001" in out and "main0001" not in out
    code, out = run(two, ["status", "--json"], capsys)
    assert {r["space"] for r in json.loads(out)["recent"]} == {"main", "team"}
    code, out = run(two, ["status", "--json", "--space", "main"], capsys)
    assert {r["space"] for r in json.loads(out)["recent"]} == {"main"}
    assert run(two, ["show", "team0001", "--space", "main"], capsys)[0] == 1  # not in that space
    assert run(two, ["show", "team0001"], capsys)[0] == 0
    assert run(two, ["export", "team0001", "--space", "team", "--format", "json"], capsys)[0] == 0
    code, out = run(two, ["reprocess", "team0001"], capsys)
    assert code == 2 and "--space" in out
    assert run(two, ["reprocess", "team0001", "--space", "main"], capsys)[0] == 1
    assert run(two, ["list", "--space", "ghost"], capsys)[0] == 2
    for view in (["status"], ["show", "x"], ["export", "x"], ["reprocess", "x"]):
        assert run(two, [*view, "--space", "ghost"], capsys)[0] == 2  # an unknown space is never guessed


def test_text_views_carry_the_space_column(two, capsys):
    code, out = run(two, ["status"], capsys)
    assert code == 0 and "main0001  main" in out and "team0001  team" in out
    code, out = run(two, ["status", "--space", "team"], capsys)
    assert "team0001  done" in out and "main0001" not in out  # one space chosen: no column
    code, out = run(two, ["export", "team0001", "--format", "json"], capsys)  # a view: ids are unique
    assert code == 0 and json.loads(out)["meeting"]["space"] == "team"
    assert run(two, ["export", "team0001", "--space", "main"], capsys)[0] == 1
    assert run(two, ["reprocess", "team0001", "--space", "team", "--from", "deliver"], capsys)[0] == 0


def test_one_space_needs_no_selector(rt, meeting, capsys):
    rt.repo().save_meeting(replace(meeting, state=MeetingState.DONE))
    code, out = run(rt, ["list"], capsys)
    assert code == 0 and f"{meeting.id}  2026" in out  # no space column
    assert run(rt, ["reprocess", meeting.id, "--from", "deliver"], capsys)[0] == 0


def test_google_commands_per_space(two, capsys):
    code, out = run(two, ["google", "sync"], capsys)
    assert code == 2 and "--space" in out
    assert run(two, ["google", "disconnect"], capsys)[0] == 2
    assert run(two, ["google", "connect", "--no-browser"], capsys)[0] == 2
    code, out = run(two, ["google", "status", "--json"], capsys)
    assert [s["space"] for s in json.loads(out)["spaces"]] == ["main", "team"]
    code, out = run(two, ["google", "status", "--json", "--space", "team"], capsys)
    assert json.loads(out)["space"] == "team" and json.loads(out)["connected"] is False
    code, out = run(two, ["google", "status"], capsys)
    assert "[space main]" in out and "[space team]" in out
    code, out = run(two, ["google", "sync", "--space", "team"], capsys)
    assert code == 1  # not connected: that space's own answer
    assert run(two, ["google", "disconnect", "--space", "team"], capsys)[0] == 0


def test_config_list_shows_a_forum_and_its_warnings(rt, capsys):
    """DESIGN §19.1: ``config list`` names the kind of a resolved forum and repeats its warnings."""
    from meeting_scribe.discord_ui.destination import REPORT_KV

    rt.repo().kv_set(f"{REPORT_KV}.discord", json.dumps({
        "guild": {"id": "100", "name": "Example Team", "source": "meeting"}, "targets": ["700"],
        "steps": [{"key": "delivery_discord_channel", "value": "notes", "status": "ok", "channel_id": "700",
                   "channel_name": "notes", "kind": "forum"}],
        "warnings": ["delivery_discord_channel: the bot is missing Attach Files in forum #notes",
                     "project_channels[orion]: forum #orion requires a tag on every post"]}))
    code, out = run(rt, ["config", "list", "--group", "delivery"], capsys)
    assert code == 0 and "→ forum #notes (700)" in out and "! delivery_discord_channel: the bot is missing" in out
    code, out = run(rt, ["config", "list", "--json"], capsys)
    rows = {r["key"]: r for r in json.loads(out)["settings"]}
    assert rows["delivery_discord_channel"]["resolved"]["kind"] == "forum"
    assert "requires a tag" in rows["project_channels"]["resolved"]["warnings"][0]
