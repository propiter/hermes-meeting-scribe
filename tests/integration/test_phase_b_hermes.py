"""Phase B against the REAL Hermes Discord adapter module and discord.py (read-only checkout).

Run via ``scripts/test-integration.sh`` (Hermes venv, py3.11). Covers:
  * the plugin, loaded by the real PluginManager, registers the Discord platform handler;
  * the compat probe passes against ``plugins.platforms.discord.adapter`` + discord.py;
  * ScribeReceiver built on the REAL ``VoiceReceiver`` captures a real NaCl-encrypted,
    Opus-encoded RTP packet into its timed buffers (no crypto/codec mocks);
  * the factory attaches to a real ``DiscordAdapter`` class and registers DynamicItems on a
    real ``commands.Bot`` without connecting to Discord.
"""
from __future__ import annotations

import asyncio
import shutil
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.integration
plugins_mod = pytest.importorskip("hermes_cli.plugins", reason="Hermes is not importable (set PYTHONPATH)")
adapter_mod = pytest.importorskip("plugins.platforms.discord.adapter")
discord = pytest.importorskip("discord")
yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
IGNORE = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "*.pyc", "tests")


@pytest.fixture
def manager(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(REPO, home / "plugins" / "meeting-scribe", ignore=IGNORE)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["meeting-scribe"]}}), encoding="utf-8")
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)  # not a gateway unless a test says so
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: bundled)
    mgr = plugins_mod.PluginManager()
    mgr.discover_and_load()
    yield mgr
    import meeting_scribe.plugin as plugin

    for rt in list(plugin.RUNTIMES.values()):
        rt.close()
    plugin.RUNTIMES.clear()
    if "meeting-scribe" in mgr._plugins:
        mgr.unload("meeting-scribe")


def test_plugin_registers_discord_platform_handler(manager):
    loaded = manager._plugins["meeting-scribe"]
    assert loaded.enabled and loaded.module is not None, getattr(loaded, "error", None)
    factories = manager.get_platform_handler_factories("discord")
    assert [name for _, name in factories].count("meeting-scribe") == 1
    import meeting_scribe.plugin as plugin

    rt = next(iter(plugin.RUNTIMES.values()))
    assert rt.capture is not None and any(getattr(s, "name", "") == "discord" for s in rt.sinks())


def test_compat_probe_passes_against_real_hermes():
    from meeting_scribe.capture.compat import probe_hermes

    res = probe_hermes()
    assert res.ok, res.problems
    assert len(res.checked) >= 20


def _voice_client(key: bytes):
    conn = SimpleNamespace(secret_key=list(key), dave_session=None, ssrc=1, hook=None,
                           add_socket_listener=lambda fn: None, remove_socket_listener=lambda fn: None)
    return SimpleNamespace(_connection=conn, user=SimpleNamespace(id=9999),
                           channel=SimpleNamespace(members=[]))


def _rtp(key: bytes, opus_payload: bytes, *, ssrc: int = 100, seq: int = 1) -> bytes:
    import nacl.secret

    header = struct.pack(">BBHII", 0x80, 0x78, seq, seq * 960, ssrc)
    nonce4 = struct.pack(">I", seq)
    nonce = nonce4 + b"\x00" * 20
    ct = nacl.secret.Aead(key).encrypt(opus_payload, header, nonce).ciphertext
    return header + ct + nonce4


def test_scribe_receiver_on_real_voice_receiver_packet_path():
    import discord.opus as opus

    if not opus.is_loaded():
        opus._load_default()
    if not opus.is_loaded():
        pytest.skip("libopus not loadable")
    from meeting_scribe.capture.receiver import scribe_receiver_class

    key = bytes(range(32))
    cls = scribe_receiver_class(adapter_mod.VoiceReceiver)
    clock = iter([100.0, 100.02, 100.04, 200.0, 200.0])
    rx = cls(_voice_client(key), clock=lambda: next(clock))
    rx.start()
    rx.map_ssrc(100, 42)
    enc = opus.Encoder()
    frame = enc.encode(b"\x00\x01" * 1920, 960)  # 20 ms of 48 kHz stereo
    for seq in (1, 2, 3):
        rx._on_packet(_rtp(key, frame, seq=seq))
    drained = rx.drain()
    assert list(drained) == [42]
    times = [t for t, _ in drained[42]]
    assert times == [100.0, 100.02, 100.04]
    assert all(len(pcm) == 3840 for _, pcm in drained[42])
    rx.stop()


def test_real_voice_receiver_without_speaking_keeps_audio_until_identified():
    """Hermes' real ``_on_packet`` with a real Opus decoder: an SSRC SPEAKING never mapped is
    retained (not decoded as noise) and, once SPEAKING arrives, decoded at its original arrival
    times; without it, it becomes an unidentified track inferred to be the only candidate's (§4.1)."""
    import discord.opus as opus

    if not opus.is_loaded():
        opus._load_default()
    if not opus.is_loaded():
        pytest.skip("libopus not loadable")
    from meeting_scribe.capture.receiver import scribe_receiver_class

    key = bytes(range(32))
    cls = scribe_receiver_class(adapter_mod.VoiceReceiver)
    now = [100.0]
    rx = cls(_voice_client(key), clock=lambda: now[0])
    rx.start()
    rx.update_presence([42])
    frame = opus.Encoder().encode(b"\x00\x01" * 1920, 960)
    for seq in (1, 2, 3):
        rx._on_packet(_rtp(key, frame, ssrc=777, seq=seq))
        now[0] += 0.02
    assert rx.drain() == {}  # plain Opus proves nothing: nobody is guessed
    rx.map_ssrc(777, 42)
    drained = rx.drain()
    assert list(drained) == [42]
    assert [t for t, _ in drained[42]] == pytest.approx([100.0, 100.02, 100.04])
    assert all(len(pcm) == 3840 for _, pcm in drained[42])
    assert rx.voice_report().identified == {777: (42, "speaking")}
    for seq in (4, 5):
        rx._on_packet(_rtp(key, frame, ssrc=778, seq=seq))
        now[0] += 0.02
    rx.update_presence([42, 43])
    rx.drain(final=True)
    assert [len(f) for f in rx.drain_unidentified().values()] == [2]
    assert rx.voice_report().inferred == {"unidentified-1": (43, "sole")}
    rx.stop()


def test_real_voice_websocket_hands_clients_connect_to_the_scribe_receiver():
    """discord.py's real voice websocket, built as it is on connect (``hook=`` from the connection
    state of the scribe's voice client): op 11 received during the handshake, before the receiver
    exists, reaches it at start(); op 13 afterwards goes through the listener (DESIGN §4.1)."""
    import asyncio

    import discord
    from discord.gateway import DiscordVoiceWebSocket
    from discord.voice_state import VoiceConnectionState

    from meeting_scribe.capture.receiver import scribe_receiver_class
    from meeting_scribe.capture.voice_client import scribe_voice_client_class

    cls = scribe_voice_client_class(discord.VoiceClient, VoiceConnectionState)
    vc = cls.__new__(cls)
    state = vc.create_connection_state()
    vc._connection = state
    state.secret_key, state.ssrc = list(bytes(range(32))), 1
    vc.channel, vc._state = SimpleNamespace(members=[]), SimpleNamespace(user=SimpleNamespace(id=9999))

    async def handshake():
        ws = DiscordVoiceWebSocket(None, asyncio.get_running_loop(), hook=state.hook)
        ws._connection = state
        await ws.received_message({"op": 11, "d": {"user_ids": ["42", "43"]}})
        return ws

    ws = asyncio.run(handshake())
    state.add_socket_listener = state.remove_socket_listener = lambda fn: None
    rx = scribe_receiver_class(adapter_mod.VoiceReceiver)(vc, clock=lambda: 0.0)
    rx.start()  # Hermes wraps state.hook for SPEAKING; op 11 is already known
    assert rx._voice_clients == {42, 43}
    asyncio.run(ws.received_message({"op": 13, "d": {"user_id": "43"}}))
    assert rx._voice_clients == {42}
    rx.stop()


def test_factory_on_real_bot_registers_dynamic_items(manager):
    asyncio.run(_factory_on_real_bot(manager))


async def _factory_on_real_bot(manager):
    from discord.ext import commands

    factory = next(f for f, name in manager.get_platform_handler_factories("discord") if name == "meeting-scribe")
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    adapter = adapter_mod.DiscordAdapter.__new__(adapter_mod.DiscordAdapter)
    adapter._voice_clients, adapter._voice_locks = {}, {}
    adapter._allowed_user_ids, adapter._allowed_role_ids = set(), set()
    factory(bot, adapter)
    try:
        assert len(bot._connection._view_store._dynamic_items) == 2
        import meeting_scribe.plugin as plugin

        rt = next(iter(plugin.RUNTIMES.values()))
        assert rt.capture.adapter is adapter and rt.capture.compat_result.ok, rt.capture.compat_result
        assert rt.capture.loop is asyncio.get_running_loop()
    finally:
        await bot.close()
