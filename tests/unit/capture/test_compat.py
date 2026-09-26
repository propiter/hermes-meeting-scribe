"""Compat probe over the Hermes voice internals we depend on (DESIGN §4, §13)."""
from __future__ import annotations

import threading
from collections import defaultdict

from meeting_scribe.capture.compat import CompatResult, probe


class GoodReceiver:
    def __init__(self, voice_client, allowed_user_ids=None):
        self._vc = voice_client
        self._lock = threading.Lock()
        self._buffers = defaultdict(bytearray)
        self._ssrc_to_user = {}
        self._dave_session = None

    def start(self):
        pass

    def stop(self):
        pass

    def map_ssrc(self, ssrc, user_id):
        pass

    def _on_packet(self, data):
        ssrc, pcm = 1, b""
        with self._lock:
            self._buffers[ssrc].extend(pcm)


class GoodAdapter:
    def __init__(self):
        self._voice_clients = {}
        self._voice_locks = {}
        self._client = None

    async def leave_voice_channel(self, guild_id):
        pass

    async def get_user_voice_channel(self, guild_id, user_id):
        pass

    async def _resolve_channel(self, channel_id):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        pass


class GoodConn:
    def __init__(self):
        self.hook = None
        self.secret_key = None
        self.ssrc = 0
        self.dave_session = None

    def add_socket_listener(self, cb):
        pass

    def remove_socket_listener(self, cb):
        pass

    def send_packet(self, packet):
        pass


def good_auth(interaction, allowed_user_ids, allowed_role_ids):
    return True


def test_compatible_surface_passes():
    res = probe(GoodReceiver, GoodAdapter, GoodConn, good_auth)
    assert isinstance(res, CompatResult)
    assert res.ok, res.problems
    assert res.problems == ()
    assert "VoiceReceiver._on_packet" in " ".join(res.checked)


def test_receiver_buffer_contract_change_is_detected():
    class Changed(GoodReceiver):
        def _on_packet(self, data):
            self._pcm_queue.put(data)

    res = probe(Changed, GoodAdapter, GoodConn, good_auth)
    assert not res.ok
    assert any("_buffers[ssrc].extend(" in p for p in res.problems)


def test_missing_methods_and_attrs_are_listed():
    class NoMap(GoodReceiver):
        map_ssrc = None

    class NoLocks(GoodAdapter):
        def __init__(self):
            self._voice_clients = {}

    class NoDave(GoodConn):
        def __init__(self):
            self.hook = None
            self.secret_key = None
            self.ssrc = 0

    res = probe(NoMap, NoLocks, NoDave, good_auth)
    joined = "\n".join(res.problems)
    assert "VoiceReceiver.map_ssrc" in joined
    assert "DiscordAdapter._voice_locks" in joined
    assert "VoiceConnectionState.dave_session" in joined


def test_receiver_signature_is_checked():
    class OddInit(GoodReceiver):
        def __init__(self, voice_client, allowed, extra):
            super().__init__(voice_client)

    res = probe(OddInit, GoodAdapter, GoodConn, good_auth)
    assert any("VoiceReceiver.__init__" in p for p in res.problems)


def test_missing_auth_helper_and_none_classes():
    res = probe(None, None, None, None)
    assert not res.ok
    assert {"VoiceReceiver", "DiscordAdapter", "VoiceConnectionState", "_component_check_auth"} <= {
        p.split(" ")[0].split(".")[0] for p in res.problems}


def test_summary_is_human_readable():
    res = probe(GoodReceiver, GoodAdapter, GoodConn, None)
    assert "_component_check_auth" in res.summary()
