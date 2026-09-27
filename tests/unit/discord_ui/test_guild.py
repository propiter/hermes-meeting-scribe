"""Guild channel snapshot and the Discord-channels project catalog (DESIGN §16)."""
from __future__ import annotations

import asyncio

from meeting_scribe.discord_ui.guild import DiscordChannelCatalog, snapshot_channels

from .fakes import FakeBot


def guild():
    bot = FakeBot()
    bot.add(900, "Product", kind="category")
    bot.add(501, "『🚀』orion", category_id=900, position=1)
    bot.add(502, "🟢┃nebula", category_id=900, position=2, threads_ok=False)
    bot.add(503, "Daily Sync", kind="voice")
    bot.add(504, "readonly", can_post=False)
    bot.add(505, "🎉", position=9)
    return bot


def test_snapshot_keeps_text_channels_and_categories_with_permissions():
    bot = guild()
    snap = {c.id: c for c in snapshot_channels(bot.guild, need_threads=True)}
    assert set(snap) == {"900", "501", "502", "504", "505"}  # voice excluded
    assert snap["900"].kind == "category" and snap["501"].category_id == "900"
    assert snap["501"].can_post and not snap["502"].can_post and not snap["504"].can_post
    assert {c.id for c in snapshot_channels(bot.guild, need_threads=False) if c.can_post} >= {"501", "502"}


async def test_catalog_runs_on_the_loop_and_offers_clean_names(meeting):
    bot = guild()
    loop = asyncio.get_running_loop()
    cat = DiscordChannelCatalog(adapter=lambda: type("A", (), {"_client": bot})(), loop=lambda: loop,
                                ignore_prefixes=lambda: ())
    cands = await asyncio.to_thread(cat.candidates, meeting)
    by_key = {c.key: c for c in cands}
    assert by_key["discord:501"].name == "orion" and by_key["discord:501"].source == "discord"
    assert by_key["discord:900"].ref == {"channel_id": "900", "kind": "category"}
    assert "discord:505" not in by_key  # nothing left after removing decoration


def test_catalog_without_connection_is_empty(meeting):
    cat = DiscordChannelCatalog(adapter=lambda: None, loop=lambda: None, ignore_prefixes=lambda: ())
    assert cat.candidates(meeting) == []
