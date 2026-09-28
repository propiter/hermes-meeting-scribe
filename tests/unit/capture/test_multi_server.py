"""Several Discord servers at once, and one voice connection per server (DESIGN §23).

The bot keeps ONE voice connection per server (Hermes' adapter stores ``_voice_clients`` by guild
id), so two servers record in parallel but a second channel of the same server must wait: auto-join
never leaves a live recording, and ``/meeting start`` from elsewhere says which channel is recorded.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from meeting_scribe.capture.autojoin import AutoJoiner
from meeting_scribe.capture.compat import CompatResult
from meeting_scribe.capture.controller import CaptureManager
from meeting_scribe.commands import Caller
from meeting_scribe.config import settings_from_mapping

from .fakes import FakeAdapter, FakeBot, FakeGuild, FakeMember, FakeTextChannel, FakeVoiceChannel, FakeVoiceReceiver
from .test_controller import NullWriter, Svc


def server(gid: int, text_id: int, voice_ids: tuple[int, ...]) -> FakeGuild:
    guild = FakeGuild(gid, f"Server {gid}")
    guild.channels = [FakeTextChannel(text_id, "general", guild),
                      *(FakeVoiceChannel(v, f"Voice {v}", guild) for v in voice_ids)]
    return guild


def sit(guild: FakeGuild, channel: FakeVoiceChannel, uid: int) -> FakeMember:
    member = FakeMember(uid, f"u{uid}", channel=channel)
    channel.members.append(member)
    guild.members[uid] = member
    return member


@pytest.fixture
def world(tmp_path: Path):
    a, b = server(100, 300, (500, 501)), server(200, 400, (600,))
    mod = types.ModuleType("fake_hermes_adapter_multi")
    setattr(mod, "VoiceReceiver", FakeVoiceReceiver)
    sys.modules[mod.__name__] = mod
    adapter = type("DiscordAdapter", (FakeAdapter,), {"__module__": mod.__name__})(FakeBot([a, b]))
    svc = Svc(tmp_path)
    s = settings_from_mapping({"consent_nickname_prefix": "", "autojoin_min_humans": 1,
                               "autojoin_grace_seconds": 0})
    spaces = {100: "main", 200: "team"}
    mgr = CaptureManager(service=lambda: svc, settings=lambda space=None: s, ffmpeg=lambda: None,
                         writer_factory=lambda ff, path, t0, kbps: NullWriter(),
                         compat=lambda adapter: CompatResult(True, (), ("x",)), tick=0.001,
                         space_of=lambda guild: spaces.get(int(guild.id)))
    yield types.SimpleNamespace(a=a, b=b, adapter=adapter, mgr=mgr, svc=svc, settings=s, spaces=spaces)
    sys.modules.pop(mod.__name__, None)


def caller(chat: int, user: int) -> Caller:
    return Caller("discord", str(chat), str(user))


def connect(world) -> None:
    """On the test's loop, as Hermes calls the platform handler on the gateway loop."""
    world.mgr.attach(world.adapter._client, world.adapter)


async def test_two_servers_record_in_parallel(world):
    connect(world)
    a_voice, b_voice = world.a.get_channel(500), world.b.get_channel(600)
    sit(world.a, a_voice, 42)
    sit(world.b, b_voice, 43)
    replies = await asyncio.gather(asyncio.to_thread(world.mgr.start, caller(300, 42), None),
                                   asyncio.to_thread(world.mgr.start, caller(400, 43), None))
    assert "Voice 500" in replies[0] and "Voice 600" in replies[1]
    live_a, live_b = world.mgr.session_for(100), world.mgr.session_for(200)
    assert live_a.meeting.space == "main" and live_b.meeting.space == "team"
    assert set(world.adapter._voice_clients) == {100, 200}  # one connection per server
    assert len(world.mgr.live_meeting_ids()) == 2
    await asyncio.to_thread(world.mgr.stop, caller(300, 42))
    assert world.mgr.session_for(200) is live_b and not live_b.done  # stopping one leaves the other
    await asyncio.to_thread(world.mgr.stop, caller(400, 43))
    assert {mid for mid, _ in world.svc.finished} == {live_a.meeting.id, live_b.meeting.id}


async def test_start_from_another_channel_of_the_same_server_names_the_live_one(world):
    connect(world)
    daily, planning = world.a.get_channel(500), world.a.get_channel(501)
    sit(world.a, daily, 42)
    sit(world.a, planning, 43)
    assert "Voice 500" in await asyncio.to_thread(world.mgr.start, caller(300, 42), None)
    live = world.mgr.session_for(100)
    reply = await asyncio.to_thread(world.mgr.start, caller(300, 43), None)
    assert "already recording **Voice 500**" in reply and "one voice channel per server" in reply
    assert live.meeting.id in reply and planning.connects == 0
    same = await asyncio.to_thread(world.mgr.start, caller(300, 42), None)
    assert "one voice channel" not in same and "Voice 500" in same  # asked again for the same channel
    assert world.mgr.session_for(100) is live and not live.done
    await asyncio.to_thread(world.mgr.stop, caller(300, 42))


async def test_autojoin_never_leaves_a_live_recording(world):
    connect(world)
    daily, planning = world.a.get_channel(500), world.a.get_channel(501)
    joiner = AutoJoiner(world.mgr, lambda space=None: world.settings, clock=lambda: 0.0, poll=0.001,
                        space_of=world.mgr.space_of)
    sit(world.a, daily, 42)
    await asyncio.to_thread(world.mgr.start, caller(300, 42), None)
    live = world.mgr.session_for(100)
    member = sit(world.a, planning, 43)
    await joiner.on_voice_state_update(member, types.SimpleNamespace(channel=None),
                                       types.SimpleNamespace(channel=planning))
    for _ in range(20):
        await asyncio.sleep(0.002)
    assert planning.connects == 0 and world.mgr.session_for(100) is live and not live.done
    assert world.adapter._voice_clients[100] is live.vc
    # Another server is free: auto-join records there at the same time.
    other = world.b.get_channel(600)
    guest = sit(world.b, other, 44)
    await joiner.on_voice_state_update(guest, types.SimpleNamespace(channel=None),
                                       types.SimpleNamespace(channel=other))
    for _ in range(50):
        await asyncio.sleep(0.002)
        if world.mgr.session_for(200) is not None:
            break
    assert other.connects == 1 and world.mgr.session_for(200).meeting.space == "team"
    joiner.close()
    await asyncio.to_thread(world.mgr.shutdown)
