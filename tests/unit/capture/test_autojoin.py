"""Auto-join on voice_state_update with grace/debounce (DESIGN §4)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from meeting_scribe.capture.autojoin import AutoJoiner
from meeting_scribe.config import settings_from_mapping

from .fakes import FakeGuild, FakeMember, FakeVoiceChannel


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Launcher:
    def __init__(self) -> None:
        self.started: list = []
        self.busy_guilds: set = set()

    async def start_in(self, channel, *, started_by=None):
        self.started.append(channel.id)
        self.busy_guilds.add(channel.guild.id)

    def busy(self, guild):
        return guild.id in self.busy_guilds


@pytest.fixture
def world():
    guild = FakeGuild()
    ch = FakeVoiceChannel(500, "Daily Sync", guild)
    ignored = FakeVoiceChannel(501, "AFK", guild)
    guild.channels = [ch, ignored]
    launcher = Launcher()
    clock = Clock()
    cfg = {"autojoin_grace_seconds": 20, "autojoin_min_humans": 2, "autojoin_ignore_channels": ["afk"]}

    def make(**over):
        s = settings_from_mapping({**cfg, **over})
        return AutoJoiner(launcher, lambda space=None: s, clock=clock, poll=0.001)
    return SimpleNamespace(guild=guild, ch=ch, ignored=ignored, launcher=launcher, clock=clock, make=make)


def join(ch, uid, bot=False):
    m = FakeMember(uid, f"u{uid}", bot=bot, channel=ch)
    ch.members.append(m)
    return m, SimpleNamespace(channel=None), SimpleNamespace(channel=ch)


async def settle(n=10):
    for _ in range(n):
        await asyncio.sleep(0.002)


async def test_starts_after_grace_with_min_humans(world):
    aj = world.make()
    await aj.on_voice_state_update(*join(world.ch, 1))
    await aj.on_voice_state_update(*join(world.ch, 2))
    await aj.on_voice_state_update(*join(world.ch, 3, bot=True))
    await settle()
    assert world.launcher.started == []
    world.clock.t = 21
    await settle()
    assert world.launcher.started == [500]


async def test_bots_do_not_count(world):
    aj = world.make()
    await aj.on_voice_state_update(*join(world.ch, 1))
    await aj.on_voice_state_update(*join(world.ch, 2, bot=True))
    world.clock.t = 30
    await settle()
    assert world.launcher.started == []


async def test_leaving_during_grace_cancels(world):
    aj = world.make()
    await aj.on_voice_state_update(*join(world.ch, 1))
    m, before, after = join(world.ch, 2)
    await aj.on_voice_state_update(m, before, after)
    world.clock.t = 10
    world.ch.members.remove(m)
    await aj.on_voice_state_update(m, after, SimpleNamespace(channel=None))
    world.clock.t = 25
    await settle()
    assert world.launcher.started == []


async def test_repeated_events_debounce_to_one_start(world):
    aj = world.make()
    for uid in (1, 2, 3, 4):
        await aj.on_voice_state_update(*join(world.ch, uid))
    world.clock.t = 21
    await settle()
    for uid in (5, 6):
        await aj.on_voice_state_update(*join(world.ch, uid))
    world.clock.t = 50
    await settle()
    assert world.launcher.started == [500]


async def test_ignore_list_allowlist_and_disabled(world):
    aj = world.make()
    await aj.on_voice_state_update(*join(world.ignored, 1))
    await aj.on_voice_state_update(*join(world.ignored, 2))
    world.clock.t = 30
    await settle()
    assert world.launcher.started == []

    aj2 = world.make(**{"autojoin_channels": ["999"]})
    await aj2.on_voice_state_update(*join(world.ch, 1))
    await aj2.on_voice_state_update(*join(world.ch, 2))
    world.clock.t = 60
    await settle()
    assert world.launcher.started == []

    aj3 = world.make(**{"autojoin_enabled": False})
    await aj3.on_voice_state_update(*join(world.ch, 3))
    world.clock.t = 90
    await settle()
    assert world.launcher.started == []


async def test_allowlist_by_name_and_busy_guild(world):
    aj = world.make(**{"autojoin_channels": ["daily sync"], "autojoin_grace_seconds": 0})
    world.launcher.busy_guilds.add(world.guild.id)
    await aj.on_voice_state_update(*join(world.ch, 1))
    await aj.on_voice_state_update(*join(world.ch, 2))
    await settle()
    assert world.launcher.started == []
    world.launcher.busy_guilds.clear()
    await aj.on_voice_state_update(*join(world.ch, 3))
    await settle()
    assert world.launcher.started == [500]


async def test_start_failure_is_logged_not_raised(world):
    async def boom(channel, *, started_by=None):
        raise RuntimeError("no perms")
    world.launcher.start_in = boom
    aj = world.make(**{"autojoin_grace_seconds": 0})
    await aj.on_voice_state_update(*join(world.ch, 1))
    await aj.on_voice_state_update(*join(world.ch, 2))
    await settle()
    aj.close()
