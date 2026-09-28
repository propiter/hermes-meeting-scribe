"""Several spaces in one install (DESIGN §23): nothing is recorded, shown or published without an
explicit or assigned space; with a single space every surface behaves as before spaces."""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meeting_scribe.capture.autojoin import AutoJoiner
from meeting_scribe.commands import Caller, MeetingCommands
from meeting_scribe.config import settings_from_mapping
from meeting_scribe.discord_ui.destination import resolve
from meeting_scribe.domain.models import SOURCE_GOOGLE_MEET, MeetingState
from meeting_scribe.runtime import Host, Runtime
from meeting_scribe.tools import MeetingTools

from .capture.fakes import FakeGuild, FakeMember, FakeVoiceChannel
from .discord_ui.fakes import FakeBot


class FakeKanban:
    def create_task(self, **kw):
        return "t1"

    def list_boards(self):
        return []


def runtime(tmp_path: Path, config=None) -> Runtime:
    cfg = dict(config or {})
    return Runtime(Host(
        get_config=lambda key, default=None: cfg.get(key, default),
        set_config=lambda key, value: cfg.__setitem__(key, value),
        data_dir=lambda: tmp_path / "data", llm=lambda: SimpleNamespace(), secret=lambda name: None,
        spawner=lambda target, *, name, daemon=True: threading.Thread(target=target, name=name, daemon=daemon),
        call_mcp=lambda: None, kanban=FakeKanban(), project_sources=lambda: [], llm_ready=lambda: (True, "ok")))


def discord(guild: str = "", user: str = "10") -> Caller:
    return Caller(platform="discord", chat_id="555", user_id=user, scope_id=guild)


# -- guild ownership ----------------------------------------------------------------------------
def test_one_space_claims_every_server_as_before(tmp_path):
    rt = runtime(tmp_path)
    assert rt.space_of_guild(SimpleNamespace(id=100, name="Acme")) == "main"
    assert rt.space_of_guild("200") == "main"
    assert getattr(rt.spaces().for_guild("100"), "slug", None) == "main"  # recorded: stays after a second space appears
    rt.spaces().create("Team", "team")
    assert rt.space_of_guild("100") == "main"
    assert rt.space_of_guild("300") is None  # unassigned with several spaces: never recorded
    assert rt.spaces().for_guild("300") is None
    rt.close()


def test_adoption_on_first_connect_is_idempotent(tmp_path):
    rt = runtime(tmp_path)
    guilds = [SimpleNamespace(id=100, name="Acme"), SimpleNamespace(id=200, name="Beta")]
    assert sorted(rt.adopt_guilds(guilds)) == ["100", "200"]
    assert rt.adopt_guilds(guilds + [SimpleNamespace(id=300, name="Late")]) == []  # only once
    assert set(rt.spaces().require("main").guild_ids) == {"100", "200"}
    rt.close()


def test_space_guilds_restricts_publishing_only_with_several_spaces(tmp_path):
    rt = runtime(tmp_path)
    rt.space_of_guild("100")
    assert rt.space_guilds("main") is None  # one space: any server of the bot, as before
    rt.spaces().create("Team", "team")
    rt.spaces().add_guild("team", "200")
    assert rt.space_guilds("main") == frozenset({"100"})
    assert rt.space_guilds("team") == frozenset({"200"})
    assert rt.space_guilds("gone") == frozenset()
    rt.close()


# -- chat commands and agent tools --------------------------------------------------------------
@pytest.fixture
def two_teams(tmp_path, meeting, utterances):
    rt = runtime(tmp_path)
    rt.space_of_guild("100")  # the first team's server joins ``main``
    rt.spaces().create("Team", "team")
    rt.spaces().add_guild("team", "200")
    repo = rt.repo()
    mine = replace(meeting, id="main0001", state=MeetingState.DONE, space="main")
    theirs = replace(meeting, id="team0001", guild_id="200", state=MeetingState.DONE, space="team",
                     title="Secret roadmap")
    for m in (mine, theirs):
        repo.save_meeting(m)
        repo.replace_utterances(m.id, utterances)
    cmds = MeetingCommands(rt.service, rt.settings, capture=lambda: None)
    yield SimpleNamespace(rt=rt, cmds=cmds, mine=mine, theirs=theirs)
    rt.close()


def test_commands_act_in_the_space_of_the_callers_server(two_teams):
    cmds, mine, theirs = two_teams.cmds, two_teams.mine, two_teams.theirs
    in_main = cmds.handle("list", discord("100"), "meeting")
    assert mine.id in in_main and theirs.id not in in_main
    in_team = cmds.handle("list", discord("200"), "meeting")
    assert theirs.id in in_team and mine.id not in in_team
    assert "No meeting" in cmds.handle(f"show {theirs.id}", discord("100"), "meeting")
    assert "Secret roadmap" in cmds.handle(f"show {theirs.id}", discord("200"), "meeting")
    found = cmds.handle("search credenciales", discord("100"), "meeting")
    assert mine.id in found and theirs.id not in found


def test_without_a_server_several_spaces_require_a_choice(two_teams):
    cmds = two_teams.cmds
    for sub in ("list", "status", f"show {two_teams.mine.id}", "search credenciales"):
        reply = cmds.handle(sub, discord(""), "meeting")
        assert "several teams" in reply
        assert two_teams.mine.id not in reply and two_teams.theirs.id not in reply


def members(table):
    """A membership check over ``{user_id: {guild ids}}`` (the bot's member cache)."""
    return lambda: (lambda user, guilds: {g for g in guilds if g in table.get(user, set())})


def test_a_dm_uses_the_callers_only_team(two_teams):
    cmds = MeetingCommands(two_teams.rt.service, two_teams.rt.settings, capture=lambda: None,
                           membership=members({"10": {"200"}}))
    reply = cmds.handle("list", discord(""), "meeting")
    assert two_teams.theirs.id in reply and two_teams.mine.id not in reply
    reply = cmds.handle("list space=main", discord(""), "meeting")  # not one of the caller's teams
    assert "`team` (Team)" in reply and two_teams.mine.id not in reply
    reply = cmds.handle("list", discord("", user="11"), "meeting")  # in no team's server
    assert "your team's Discord server" in reply and two_teams.theirs.id not in reply


def test_a_dm_with_several_teams_asks_which_one(two_teams):
    cmds = MeetingCommands(two_teams.rt.service, two_teams.rt.settings, capture=lambda: None,
                           membership=members({"10": {"100", "200"}}))
    reply = cmds.handle("search credenciales", discord(""), "meeting")
    assert "`main`" in reply and "`team`" in reply and "space=main" in reply
    assert two_teams.mine.id not in reply and two_teams.theirs.id not in reply
    reply = cmds.handle("search credenciales space=team", discord(""), "meeting")
    assert two_teams.theirs.id in reply and two_teams.mine.id not in reply
    assert two_teams.mine.id in cmds.handle("list SPACE=Main", discord(""), "meeting")


def test_space_option_inside_a_server_cannot_reach_another_team(two_teams):
    cmds = MeetingCommands(two_teams.rt.service, two_teams.rt.settings, capture=lambda: None,
                           membership=members({"10": {"100", "200"}}))
    reply = cmds.handle("list space=team", discord("100"), "meeting")
    assert "`space=` works in private messages" in reply and two_teams.theirs.id not in reply
    assert two_teams.mine.id in cmds.handle("list space=main", discord("100"), "meeting")


def test_a_dm_without_discord_connected_explains(two_teams):
    cmds = MeetingCommands(two_teams.rt.service, two_teams.rt.settings, capture=lambda: None,
                           membership=lambda: None)
    assert "several teams" in cmds.handle("list space=team", discord(""), "meeting")


def test_an_unassigned_server_sees_nothing(two_teams):
    reply = two_teams.cmds.handle("list", discord("999"), "meeting")
    assert "not linked to any team" in reply and two_teams.mine.id not in reply
    assert "hermes meeting-scribe space add-guild <space> 999" in reply  # what the administrator runs


def test_agent_tools_follow_the_chat_server(two_teams):
    rt = two_teams.rt
    from_team = MeetingTools(rt.service, guild=lambda: "200")
    hits = json.loads(from_team.search({"query": "credenciales"}))["results"]
    assert {h["meeting_id"] for h in hits} == {two_teams.theirs.id}
    assert "error" in json.loads(from_team.get({"meeting_id": two_teams.mine.id}))
    outside = MeetingTools(rt.service, guild=lambda: "")
    assert "several spaces" in json.loads(outside.search({"query": "credenciales"}))["error"]


def test_status_is_filtered_by_space(two_teams):
    svc = two_teams.rt.service()
    assert [r["id"] for r in svc.status("team")["recent"]] == [two_teams.theirs.id]
    assert {r["id"] for r in svc.status(None)["recent"]} == {two_teams.mine.id, two_teams.theirs.id}


def test_one_space_dm_keeps_working(tmp_path, meeting):
    rt = runtime(tmp_path)
    rt.repo().save_meeting(replace(meeting, state=MeetingState.DONE, space="main"))
    cmds = MeetingCommands(rt.service, rt.settings, capture=lambda: None)
    assert meeting.id in cmds.handle("list", discord(""), "meeting")
    assert meeting.id in cmds.handle("list", Caller("telegram", "1", "10"), "meeting")
    rt.close()


def test_per_space_settings_reach_the_command_language(two_teams):
    two_teams.rt.spaces().set_override("team", "ui_language", "es")
    assert "Lista" in two_teams.cmds.handle("list", discord("200"), "meeting")
    assert "Ready" in two_teams.cmds.handle("list", discord("100"), "meeting")


# -- capture ------------------------------------------------------------------------------------
async def test_autojoin_ignores_servers_of_no_space_and_uses_the_space_settings():
    guild, other = FakeGuild(), FakeGuild(999, "Stray")
    ch, stray = FakeVoiceChannel(500, "Daily", guild), FakeVoiceChannel(600, "Daily", other)
    spaces = {int(guild.id): "team"}
    seen = []

    def settings(space=None):
        seen.append(space)
        return settings_from_mapping({"autojoin_min_humans": 1, "autojoin_enabled": space == "team"})

    launcher: Any = SimpleNamespace(busy=lambda g: False)
    joiner = AutoJoiner(launcher, settings, clock=lambda: 0.0, poll=0.001,
                        space_of=lambda g: spaces.get(int(g.id)) if g is not None else None)
    for c in (ch, stray):
        c.members.append(FakeMember(1, "ana", channel=c))
    assert joiner.eligible(ch) and seen[-1] == "team"
    assert not joiner.eligible(stray)
    await asyncio.sleep(0)


# -- publishing ---------------------------------------------------------------------------------
def test_destination_never_leaves_the_meeting_space(meeting):
    bot = FakeBot()
    bot.add(301, "meeting-notes", position=0)
    other = bot.add_guild(200, "Other Team")
    bot.add(777, "meeting-notes", guild_id=200)
    meet = replace(meeting, guild_id="", channel_id="gmeet:x", text_channel_id=None, source=SOURCE_GOOGLE_MEET)
    s = settings_from_mapping({"google_meet_discord_channel": "777"})
    mine = frozenset({str(bot.guild.id)})
    d = resolve(bot, meet, s, allowed_guilds=mine)
    assert "777" not in d.targets and d.guild is not other
    assert {st.key: st.status for st in d.steps}["google_meet_discord_channel"] == "other_guild"
    by_name = resolve(bot, meet, settings_from_mapping({"google_meet_discord_channel": "meeting-notes"}),
                      allowed_guilds=frozenset({"200"}))
    assert by_name.targets[:1] == ["777"] and by_name.guild is other
    nothing = resolve(bot, meet, settings_from_mapping({}), allowed_guilds=frozenset())
    assert nothing.targets == [] and "this space" in nothing.problem
    discord_meeting = replace(meeting, guild_id="200", channel_id="778", text_channel_id=None)
    d = resolve(bot, discord_meeting, settings_from_mapping({}), allowed_guilds=mine)
    assert d.guild is not other and "777" not in d.targets


# -- Google, Desktop, reconciliation ------------------------------------------------------------
def test_google_is_per_space_and_ambiguous_without_one(tmp_path):
    from meeting_scribe.spaces import SpaceError

    rt = runtime(tmp_path)
    assert rt.google_files().dir == tmp_path / "data" / "google" / "main"
    assert rt.meet_importer().space == "main"
    rt.spaces().create("Team", "team")
    assert rt.google_files("team").dir == tmp_path / "data" / "google" / "team"
    with pytest.raises(SpaceError):
        rt.google_files()
    rt.close()


def test_poller_reconciliation_is_throttled(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    calls = []
    monkeypatch.setattr(rt, "start_meet_pollers", lambda: calls.append(1))
    assert rt.reconcile_meet_pollers(force=True)
    assert not rt.reconcile_meet_pollers()  # within POLLER_RECONCILE_SECONDS: no DB read
    rt._pollers_checked -= rt.POLLER_RECONCILE_SECONDS + 1
    assert rt.reconcile_meet_pollers()
    assert calls == [1, 1]
    rt.close()


def test_bootstrap_on_every_open_is_idempotent(tmp_path):
    rt = runtime(tmp_path)
    rt.repo()
    rt.close()
    rt = runtime(tmp_path)
    assert [s.slug for s in rt.spaces().all()] == ["main"]
    rt.close()
