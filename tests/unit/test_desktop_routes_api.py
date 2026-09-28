"""The channel catalog and rule endpoints of the Desktop API (DESIGN §19.3, Appendix A)."""
import pytest

pytest.importorskip("fastapi")

from meeting_scribe.storage.repo import Repository  # noqa: E402
from tests.unit.test_desktop_api import PREFIX, env, two_spaces  # noqa: E402,F401  (fixture)

CATALOG = [{"id": "900", "name": "Board", "type": "category", "parent_id": "", "parent_name": "", "public": True},
           {"id": "300", "name": "Leadership", "type": "voice", "parent_id": "900", "parent_name": "Board",
            "public": True},
           {"id": "501", "name": "orion", "type": "text", "parent_id": "", "parent_name": "", "public": True},
           {"id": "700", "name": "board-notes", "type": "text", "parent_id": "900", "parent_name": "Board",
            "public": False},
           {"id": "502", "name": "nebula", "type": "forum", "parent_id": "", "parent_name": "", "public": True}]


def seed(env, guild="100", rows=CATALOG):
    repo = Repository(env["root"] / "index.sqlite")
    repo.set_guild_channels(guild, rows)
    repo.set_bot_guilds([(guild, "Example Team")])
    repo.close()


def test_channels_before_the_gateway_reported_them(env):
    r = env["client"].get(f"{PREFIX}/v1/discord/channels").json()
    assert r == {"items": [], "seen_at": None}


def test_channels_and_rule_crud_with_one_space(env):
    c = env["client"]
    c.get(f"{PREFIX}/v1/meetings")  # bootstrap
    seed(env)
    items = c.get(f"{PREFIX}/v1/discord/channels").json()["items"]
    assert {i["id"] for i in items} == {"900", "300", "501", "700", "502"}
    assert next(i for i in items if i["id"] == "700")["public"] is False
    assert items[0]["guild_name"] == "Example Team"
    r = c.post(f"{PREFIX}/v1/routes", json={"origin_kind": "voice", "origin": "Leadership", "target": "board-notes",
                                              "mode": "private"}).json()
    assert r["added"] == "300=700:private" and r["warning"] == "" and r["scope"] == "global"
    assert r["items"][0]["sentence"] == "Voice channel «Leadership» → channel «board-notes» · Private"
    assert r["items"][0]["status"] == "ok"
    assert env["mem"].entry["settings"]["meeting_routes"] == ["300=700:private"]
    r = c.post(f"{PREFIX}/v1/routes", params={"lang": "es"},
               json={"origin_kind": "meet", "origin": "retro-*", "mode": "dm", "position": 1}).json()
    assert [i["text"] for i in r["items"]] == ["meet:retro-*=:dm", "300=700:private"]
    assert r["items"][0]["sentence"] == "Google Meet «retro-*» → mensajes directos a cada participante · " \
                                        "Solo mensajes directos"
    risky = c.put(f"{PREFIX}/v1/routes/2", json={"origin_kind": "voice", "origin": "300", "target": "orion",
                                                  "mode": "private"}).json()
    assert "@everyone" in risky["warning"] and risky["items"][1]["warning"]
    moved = c.post(f"{PREFIX}/v1/routes/2/move", json={"to": 1}).json()
    assert moved["items"][0]["text"] == "300=501:private"
    left = c.delete(f"{PREFIX}/v1/routes/1").json()
    assert [i["text"] for i in left["items"]] == ["meet:retro-*=:dm"]


@pytest.mark.parametrize("body,msg", [
    ({"origin_kind": "voice", "origin": "Leadership", "target": "orion", "mode": "dm"}, "leave the channel empty"),
    ({"origin_kind": "voice", "origin": "Nowhere", "target": "orion"}, "no voice channel named"),
    ({"origin_kind": "voice", "origin": "Leadership", "target": ""}, "choose the text or forum channel"),
    ({"origin_kind": "room", "origin": "x"}, "origin: choose"),
])
def test_rule_errors_are_plain_400s(env, body, msg):
    c = env["client"]
    c.get(f"{PREFIX}/v1/meetings")
    seed(env)
    r = c.post(f"{PREFIX}/v1/routes", json=body)
    assert r.status_code == 400 and msg in r.json()["detail"]


def test_duplicate_origin_missing_rule_and_bad_move(env):
    c = env["client"]
    c.get(f"{PREFIX}/v1/meetings")
    seed(env)
    c.post(f"{PREFIX}/v1/routes", json={"origin_kind": "voice", "origin": "Leadership", "target": "orion"})
    dup = c.post(f"{PREFIX}/v1/routes", json={"origin_kind": "voice", "origin": "300", "target": "nebula"})
    assert dup.status_code == 400 and "twice" in dup.json()["detail"]
    assert c.delete(f"{PREFIX}/v1/routes/9").status_code == 400
    assert c.post(f"{PREFIX}/v1/routes/1/move", json={"to": "first"}).status_code == 400


def test_with_several_spaces_rules_are_the_space_override_and_the_catalog_is_its_servers(env):
    c, _mid = two_spaces(env)
    seed(env, "100")
    seed(env, "200", [dict(CATALOG[2], id="601", name="team-room")])
    assert c.get(f"{PREFIX}/v1/routes").status_code == 409
    ids = {i["id"] for i in c.get(f"{PREFIX}/v1/discord/channels", params={"space": "team"}).json()["items"]}
    assert ids == {"601"}
    r = c.post(f"{PREFIX}/v1/routes", params={"space": "team"},
               json={"origin_kind": "category", "origin": "Anything", "target": "team-room"})
    assert r.status_code == 400  # the category is not one of the team's servers
    r = c.post(f"{PREFIX}/v1/routes", params={"space": "team"},
               json={"origin_kind": "meet", "origin": "standup", "target": "team-room"}).json()
    assert r["scope"] == "space" and r["added"] == "meet:standup=601"
    assert "meeting_routes" not in env["mem"].entry["settings"]
    assert c.get(f"{PREFIX}/v1/routes", params={"space": "main"}).json()["items"] == []
