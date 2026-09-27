"""Where notes go (DESIGN §19): ids or names, automatic channel, never a DM. Invented names only."""
from __future__ import annotations

from dataclasses import replace

import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.discord_ui.destination import auto_channel, norm_name, pick_guild, resolve
from meeting_scribe.domain.models import SOURCE_GOOGLE_MEET

from .fakes import FakeBot


@pytest.fixture
def meet(meeting):
    return replace(meeting, guild_id="", channel_id="gmeet:space1", text_channel_id=None, source=SOURCE_GOOGLE_MEET)


def bot_with(*names, system=None):
    bot = FakeBot()
    for i, name in enumerate(names):
        bot.add(300 + i, name, position=i)
    if system is not None:
        bot.guild.system_channel_id = system
    return bot


def test_norm_name_ignores_decoration_case_and_separators():
    assert norm_name("#📝┃Meeting_Notes") == norm_name("meeting notes") == norm_name("meeting-notes")


def test_meet_channel_by_name_is_resolved_in_the_only_server(meet):
    bot = bot_with("random", "🎙️・meeting-notes")
    d = resolve(bot, meet, settings_from_mapping({"google_meet_discord_channel": "#meeting-notes"}))
    assert d.targets[0] == "301" and d.guild is bot.guild and d.guild_source == "only"


def test_meet_channel_by_id_fixes_the_server(meet):
    bot = bot_with("general")
    other = bot.add_guild(200, "Other Team")
    bot.add(777, "notes", guild_id=200)
    d = resolve(bot, meet, settings_from_mapping({"google_meet_discord_channel": "777"}))
    assert d.targets[0] == "777" and d.guild is other and d.guild_source == "channel"


def test_ambiguous_or_unknown_names_are_not_guessed(meet):
    bot = bot_with("notes", "Notes")
    d = resolve(bot, meet, settings_from_mapping({"google_meet_discord_channel": "notes",
                                                  "delivery_auto_channel_names": ["nothing-here"]}))
    assert d.targets == [] and "ambiguous" in [s.status for s in d.steps]
    assert "google_meet_discord_channel" in d.problem and "config set" in d.problem
    d = resolve(bot, meet, settings_from_mapping({"delivery_discord_channel": "does-not-exist",
                                                  "delivery_auto_channel_names": ["nothing-here"]}))
    assert d.targets == [] and "missing" in [s.status for s in d.steps]


def test_automatic_prefers_the_system_channel_when_the_bot_can_attach(meet):
    bot = bot_with("general", "welcome", system=301)
    d = resolve(bot, meet, settings_from_mapping({}))
    assert d.targets == ["301"]
    bot.channels[301].can_attach = False  # cannot attach the transcript: use the named channel
    assert resolve(bot, meet, settings_from_mapping({})).targets == ["300"]


def test_automatic_follows_the_configured_name_order(meet):
    bot = bot_with("general", "💬-reuniones")
    s = settings_from_mapping({"delivery_auto_channel_names": ["reuniones", "general"]})
    assert resolve(bot, meet, s).targets == ["301"]
    bot.channels[301].can_post = False
    assert resolve(bot, meet, s).targets == ["300"]


def test_several_servers_and_nothing_configured_is_pending(meet):
    bot = bot_with("general")
    bot.add_guild(200, "Other Team")
    d = resolve(bot, meet, settings_from_mapping({}))
    assert d.targets == [] and d.guild is None and "delivery_discord_guild" in d.problem


def test_guild_setting_by_name_or_id(meet):
    bot = bot_with("general")
    other = bot.add_guild(200, "Other Team")
    bot.add(801, "meetings", guild_id=200)
    for value in ("Other Team", "200"):
        d = resolve(bot, meet, settings_from_mapping({"delivery_discord_guild": value}))
        assert d.guild is other and d.targets == ["801"], value
    g, _src, problem = pick_guild(bot, "Nope")
    assert g is None and "no server" in problem


def test_a_name_unique_across_servers_picks_its_server(meet):
    bot = bot_with("general")
    other = bot.add_guild(200, "Other Team")
    bot.add(802, "meet-notes", guild_id=200)
    d = resolve(bot, meet, settings_from_mapping({"google_meet_discord_channel": "meet-notes"}))
    assert d.targets[0] == "802" and d.guild is other


def test_discord_meeting_order_is_setting_then_voice_chat_then_auto(meeting):
    bot = bot_with("general")
    bot.add(200, "Daily Sync", kind="voice")
    m = replace(meeting, text_channel_id=None, channel_id="200")
    assert resolve(bot, m, settings_from_mapping({})).targets == ["200", "300"]
    s = settings_from_mapping({"delivery_discord_channel": "general"})
    assert resolve(bot, m, s).targets == ["300", "200"]


def test_fallback_channel_for_tasks_without_project(meet):
    bot = bot_with("general", "backlog")
    d = resolve(bot, meet, settings_from_mapping({"delivery_fallback_channel": "#backlog"}))
    assert d.fallback_channel == "301"
    assert resolve(bot, meet, settings_from_mapping({})).fallback_channel is None


def test_home_channel_is_never_a_candidate(meet):
    """Hermes' home channel is often a DM with the owner: it is not a fallback any more."""
    bot = bot_with()
    user = bot.user(42)
    d = resolve(bot, meet, settings_from_mapping({"delivery_auto_channel_names": []}))
    assert str(user.dm.id) not in d.targets and d.targets == []


def test_auto_without_guild_reports_it():
    assert auto_channel(None, ["general"]).status == "no_guild"


def test_report_is_serialisable(meet):
    import json
    d = resolve(bot_with("general"), meet, settings_from_mapping({}))
    rep = json.loads(json.dumps(d.report()))
    assert rep["guild"]["name"] == "Example Team" and rep["targets"] == ["300"]
