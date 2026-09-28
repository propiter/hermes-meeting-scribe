"""The scribe's voice client records CLIENTS_CONNECT & co. from the handshake on (DESIGN §4.1)."""
from __future__ import annotations

import asyncio

import discord
from discord.voice_state import VoiceConnectionState

from meeting_scribe.capture.compat import probe
from meeting_scribe.capture.voice_client import BACKLOG, scribe_voice_client_class


class Base:
    def __init__(self) -> None:
        self._connection = self.create_connection_state()

    def create_connection_state(self):  # discord.VoiceClient's shape
        raise AssertionError("overridden")


class State:
    def __init__(self, voice_client, *, hook=None) -> None:
        self.voice_client = voice_client
        self.hook = hook


def test_the_connection_state_gets_a_hook_that_keeps_ops_11_12_13():
    vc = scribe_voice_client_class(Base, State)()
    assert vc._connection.voice_client is vc
    hook = vc._connection.hook
    seen = []

    async def feed():
        await hook(None, {"op": 11, "d": {"user_ids": ["1", "2"]}})
        await hook(None, {"op": 5, "d": {"ssrc": 7, "user_id": "1"}})  # SPEAKING is Hermes' business
        await hook(None, {"op": 13, "d": {"user_id": "2"}})
        vc.voice_op_listener = lambda op, d: seen.append(op)
        await hook(None, {"op": 12, "d": {"user_id": "3"}})
        await hook(None, {"op": 12, "d": None})

    asyncio.run(feed())
    assert [op for op, _ in vc.voice_ops] == [11, 13, 12] and seen == [12]
    for _ in range(BACKLOG + 5):
        asyncio.run(hook(None, {"op": 12, "d": {"user_id": "4"}}))
    assert len(vc.voice_ops) == BACKLOG


def test_discord_py_accepts_the_client_and_its_hook():
    """Against the installed discord.py: the subclass builds a real connection state with our hook,
    and the probe accepts the surface it relies on."""
    cls = scribe_voice_client_class(discord.VoiceClient, VoiceConnectionState)
    vc = cls.__new__(cls)
    state = vc.create_connection_state()
    assert isinstance(state, VoiceConnectionState) and state.hook == vc._record_voice_op
    res = probe(None, None, VoiceConnectionState, None, voice_client_cls=discord.VoiceClient)
    assert not [p for p in res.problems if "VoiceClient" in p or "hook" in p]
    assert "VoiceConnectionState.__init__(voice_client, *, hook)" in res.checked


def test_probe_reports_a_connection_state_without_hook():
    class NoHook:
        def __init__(self, voice_client) -> None:
            pass

    res = probe(None, None, NoHook, None, voice_client_cls=Base)
    assert any("takes no hook" in p for p in res.problems)
    res = probe(None, None, State, None, voice_client_cls=object)
    assert "VoiceClient.create_connection_state missing or not callable" in res.problems
