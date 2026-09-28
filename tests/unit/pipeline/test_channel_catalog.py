"""The channel catalog (DESIGN §19.3): gateway snapshot + debounced refresh, the editor's rule builder
and the ``route`` CLI. Invented names only: server «Example Team» (100), voice «Leadership» (300),
category «Board» (900), public text «orion» (501), private text «board-notes» (700), forum «nebula» (502).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from meeting_scribe import cli, doctor, route_editor
from meeting_scribe.channel_catalog import Catalog, snapshot_guild
from meeting_scribe.discord_ui.channel_watch import ChannelWatch
from meeting_scribe.route_editor import RuleError
from meeting_scribe.storage.repo import Repository

from .test_cli import FakeRuntime, parse


class Perms(SimpleNamespace):
    pass


class Ch:
    def __init__(self, cid, name, kind, *, category=None, public=True, guild=None, position=0):
        self.id, self.name, self.type, self.category_id, self.position = cid, name, kind, category, position
        self.public, self.guild = public, guild

    def permissions_for(self, role):
        return Perms(view_channel=self.public)


def guild():
    g = SimpleNamespace(id=100, name="Example Team", default_role=SimpleNamespace(id=100))
    g.channels = [Ch(900, "Board", "category", guild=g), Ch(300, "Leadership", "voice", category=900, guild=g),
                  Ch(501, "orion", "text", guild=g), Ch(700, "board-notes", "text", category=900, public=False, guild=g),
                  Ch(502, "nebula", "forum", guild=g), Ch(990, "a-thread", "public_thread", guild=g)]
    return g


CATALOG = [{"id": "900", "name": "Board", "type": "category", "parent_id": "", "parent_name": "", "public": True},
           {"id": "300", "name": "Leadership", "type": "voice", "parent_id": "900", "parent_name": "Board", "public": True},
           {"id": "501", "name": "orion", "type": "text", "parent_id": "", "parent_name": "", "public": True},
           {"id": "700", "name": "board-notes", "type": "text", "parent_id": "900", "parent_name": "Board",
            "public": False},
           {"id": "502", "name": "nebula", "type": "forum", "parent_id": "", "parent_name": "", "public": True}]


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path / "db.sqlite")
    yield r
    r.close()


def test_snapshot_lists_channels_with_kind_parent_and_visibility():
    rows = {r["id"]: r for r in snapshot_guild(guild())}
    assert "990" not in rows  # threads are not catalogued
    assert rows["300"] == {"id": "300", "name": "Leadership", "type": "voice", "parent_id": "900",
                           "parent_name": "Board", "public": True}
    assert rows["700"]["public"] is False and rows["502"]["type"] == "forum"


def test_repository_keeps_one_catalog_per_server(repo):
    repo.set_guild_channels("100", CATALOG)
    repo.set_guild_channels("200", CATALOG[:1])
    assert set(repo.guild_channels()) == {"100", "200"}
    assert set(repo.guild_channels(["100"])) == {"100"}
    assert repo.guild_channels(["100"])["100"][0][2]["name"] == "orion"


async def test_watch_records_at_connect_and_debounces_channel_events(repo):
    g = guild()
    w = ChannelWatch(lambda: repo, delay=0.05)
    w.record_all([g])
    assert len(repo.guild_channels()["100"][0]) == 5
    g.channels.append(Ch(503, "new-room", "text", guild=g))
    await w.handlers["on_guild_channel_create"](g.channels[-1])
    await w.handlers["on_guild_channel_update"](g.channels[2], g.channels[2])  # a burst: one write later
    assert len(repo.guild_channels()["100"][0]) == 5
    await asyncio.sleep(0.1)
    assert len(repo.guild_channels()["100"][0]) == 6


async def test_watch_attach_and_detach_register_every_event():
    added, removed = [], []
    bot = SimpleNamespace(add_listener=lambda f, n: added.append(n), remove_listener=lambda f, n: removed.append(n))
    w = ChannelWatch(lambda: None)
    w.attach(bot)
    w.detach(bot)
    assert set(added) == set(removed) == {"on_guild_channel_create", "on_guild_channel_delete",
                                         "on_guild_channel_update", "on_guild_join"}


# -- catalog lookups -----------------------------------------------------------------------------------
def cat():
    return Catalog({"100": (CATALOG, 1.0)}, {"100": "Example Team"})


def test_find_by_name_id_kind_and_ambiguity():
    c = cat()
    assert c.find("#Orion", ("text",)).id == "501"
    assert c.find("700", ("text",)).public is False
    assert c.find("Leadership", ("text", "forum")).status == "wrong_kind"
    assert c.find("missing", ("text",)).status == "missing"
    twin = Catalog({"100": (CATALOG + [dict(CATALOG[2], id="509")], 1.0)})
    assert twin.find("orion", ("text",)).status == "ambiguous"
    assert Catalog({}).find("orion", ("text",)).status == "unknown"


# -- the rule builder ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind,origin,target,mode,entry", [
    ("voice", "Leadership", "#orion", "normal", "300=501"),
    ("category", "Board", "board-notes", "private", "category:900=700:private"),
    ("meet", "Weekly-*", "", "dm", "meet:weekly-*=:dm"),
    ("voice", "300", "nebula", "normal", "300=502"),
])
def test_build_entry_resolves_names_to_ids(kind, origin, target, mode, entry):
    assert route_editor.build_entry(kind, origin, target, mode, cat()).entry == entry


def test_build_entry_without_catalog_keeps_names():
    assert route_editor.build_entry("voice", "Leadership", "#orion", "private").entry == "Leadership=#orion:private"


@pytest.mark.parametrize("kind,origin,target,mode,msg", [
    ("voice", "Leadership", "orion", "dm", "leave the channel empty"),
    ("voice", "Leadership", "", "normal", "choose the text or forum channel"),
    ("voice", "orion", "orion", "normal", "expected voice"),
    ("voice", "Leadership", "Leadership", "normal", "expected text/forum/media"),
    ("room", "x", "y", "normal", "origin: choose"),
    ("voice", "Leadership", "orion", "secret", "mode: choose"),
])
def test_build_entry_refuses_with_a_plain_reason(kind, origin, target, mode, msg):
    with pytest.raises(RuleError, match=msg):
        route_editor.build_entry(kind, origin, target, mode, cat())


def test_private_rule_to_a_public_channel_is_warned():
    assert "@everyone" in route_editor.build_entry("voice", "Leadership", "orion", "private", cat()).warning
    assert route_editor.build_entry("voice", "Leadership", "board-notes", "private", cat()).warning == ""


def test_list_operations_keep_the_list_valid():
    rules = route_editor.add([], "300=501")
    rules = route_editor.add(rules, "category:900=:dm", 1)
    assert rules == ["category:900=:dm", "300=501"]
    with pytest.raises(RuleError, match="twice"):
        route_editor.add(rules, "300=502")
    assert route_editor.move(rules, "300", 1) == ["300=501", "category:900=:dm"]
    assert route_editor.remove(rules, "2") == ["category:900=:dm"]
    with pytest.raises(RuleError, match="no rule"):
        route_editor.remove(rules, "9")


def test_verify_marks_rules_checked_and_warns():
    from meeting_scribe.routes import parse_route
    c = cat()
    ok = c.verify(parse_route("Leadership = board-notes:private"))
    assert ok["status"] == "ok" and not ok["warning"] and ok["origin_check"]["id"] == "300"
    risky = c.verify(parse_route("Leadership = orion:private"))
    assert risky["status"] == "ok" and "@everyone" in risky["warning"]
    assert c.verify(parse_route("Leadership = :dm"))["status"] == "ok"
    assert c.verify(parse_route("Leadership = gone"))["status"] == "problem"
    assert Catalog({}).verify(parse_route("Leadership = orion"))["status"] == "not_checked"


# -- CLI -----------------------------------------------------------------------------------------------
@pytest.fixture
def rt(prepo, layout, settings, clock):
    from .test_commands import make
    _, service, _runner = make(prepo, layout, settings, clock)
    r = FakeRuntime(service, {})
    service.repo.set_guild_channels("100", CATALOG)
    return r


def run(rt, argv, capsys):
    code = cli.dispatch(parse(argv), rt)
    return code, capsys.readouterr().out


def test_route_add_list_move_remove(rt, capsys):
    code, out = run(rt, ["route", "add", "--voice", "Leadership", "--to", "#board-notes", "--private"], capsys)
    assert code == 0 and "300=700:private" in out
    code, out = run(rt, ["route", "add", "--category", "Board", "--dm"], capsys)
    assert code == 0 and rt.cfg["meeting_routes"] == ["300=700:private", "category:900=:dm"]
    code, out = run(rt, ["route", "list"], capsys)
    assert "1. ✓ Voice channel «Leadership» → channel «board-notes» · Private" in out
    assert "2. ✓ Category «Board» → direct messages to each participant · Direct messages only" in out
    assert "(700, private channel)" in out and "not checked" not in out
    code, _ = run(rt, ["route", "move", "2", "1"], capsys)
    assert code == 0 and rt.cfg["meeting_routes"][0] == "category:900=:dm"
    code, out = run(rt, ["route", "remove", "category:900"], capsys)
    assert code == 0 and rt.cfg["meeting_routes"] == ["300=700:private"]
    code, out = run(rt, ["route", "list", "--json"], capsys)
    assert json.loads(out)["rules"][0]["channel_name"] == "board-notes"


def test_route_add_refuses_and_warns(rt, capsys):
    code, out = run(rt, ["route", "add", "--voice", "Leadership", "--to", "orion", "--dm"], capsys)
    assert code == 2 and "leave the channel empty" in out
    code, out = run(rt, ["route", "add", "--voice", "Nowhere", "--to", "orion"], capsys)
    assert code == 2 and "no voice channel named" in out
    code, out = run(rt, ["route", "add", "--voice", "Leadership", "--to", "orion", "--private"], capsys)
    assert code == 0 and "@everyone" in out
    assert "meeting_routes" not in rt.cfg or rt.cfg["meeting_routes"] == ["300=501:private"]


def test_route_add_with_several_spaces_writes_the_space_override(rt, capsys):
    rt.spaces().create("Second team", "second")
    code, out = run(rt, ["route", "add", "--meet", "retro-*", "--dm"], capsys)
    assert code == 2  # several spaces: say which
    code, _ = run(rt, ["route", "add", "--meet", "retro-*", "--dm", "--space", "second"], capsys)
    assert code == 0 and rt.spaces().get("second").overrides["meeting_routes"] == ["meet:retro-*=:dm"]
    assert "meeting_routes" not in rt.cfg


def test_config_list_uses_catalog_names_instead_of_not_checked(rt, capsys):
    rt.cfg["meeting_routes"] = ["Leadership = 700:private"]
    code, out = run(rt, ["config", "list"], capsys)
    assert "Leadership (voice channel, 300) → #board-notes (700, private channel), private" in out
    assert "not checked against Discord yet" not in out


def test_doctor_route_rows_without_catalog_say_not_checked(tmp_path):
    from meeting_scribe.config import settings_from_mapping
    r = Repository(tmp_path / "x.sqlite")
    rows = doctor.route_rows(settings_from_mapping({"meeting_routes": ["Leadership = orion"]}), r)
    assert rows[0]["status"] == "not_checked"
    r.close()
