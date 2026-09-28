"""Fakes for Discord voice: a faithful subset of Hermes' ``VoiceReceiver`` (the RTP → NaCl → DAVE →
Opus → ``self._buffers[ssrc].extend(pcm)`` path copied from plugins/platforms/discord/adapter.py),
a fake voice connection/client, and the RTP packet builder from Hermes' test_voice_command.py.
The real class is exercised by the integration suite under the Hermes interpreter."""
from __future__ import annotations

import asyncio
import struct
import threading
from collections import defaultdict
from types import SimpleNamespace
from typing import Any, Callable, Optional

FRAME = b"\x01\x00" * 1920  # 20 ms of 48 kHz stereo s16le (3840 bytes)


class FakeDecoder:
    def decode(self, data: bytes) -> bytes:
        return FRAME


class FakeVoiceReceiver:
    """Mirror of Hermes ``VoiceReceiver`` (same attributes, same buffer and decoder seams)."""

    decoder_factory: Callable[[], Any] = FakeDecoder

    def __init__(self, voice_client: Any, allowed_user_ids: Optional[set] = None) -> None:
        self._vc = voice_client
        self._allowed_user_ids = allowed_user_ids or set()
        self._running = False
        self._secret_key: Optional[bytes] = None
        self._dave_session: Any = None
        self._bot_ssrc = 0
        self._ssrc_to_user: dict[int, int] = {}
        self._lock = threading.Lock()
        self._buffers: Any = defaultdict(bytearray)
        self._last_packet_time: dict[int, float] = {}
        self._decoders: dict[int, Any] = {}
        self._paused = False

    def start(self) -> None:
        conn = self._vc._connection
        self._secret_key = bytes(conn.secret_key)
        self._dave_session = conn.dave_session
        self._bot_ssrc = conn.ssrc
        self._install_speaking_hook(conn)
        conn.add_socket_listener(self._on_packet)
        self._running = True

    def _install_speaking_hook(self, conn: Any) -> None:
        original_hook = conn.hook

        async def wrapped_hook(ws: Any, msg: Any) -> None:
            if isinstance(msg, dict) and msg.get("op") == 5:
                data = msg.get("d", {})
                if data.get("ssrc") and data.get("user_id"):
                    self.map_ssrc(int(data["ssrc"]), int(data["user_id"]))
            if original_hook:
                await original_hook(ws, msg)
        conn.hook = wrapped_hook

    def stop(self) -> None:
        self._running = False
        self._vc._connection.remove_socket_listener(self._on_packet)
        with self._lock:
            self._buffers.clear()
            self._ssrc_to_user.clear()

    def map_ssrc(self, ssrc: int, user_id: int) -> None:
        with self._lock:
            self._ssrc_to_user[ssrc] = user_id

    def check_silence(self) -> list:
        raise AssertionError("the scribe must never call check_silence")

    def _on_packet(self, data: bytes) -> None:
        if not self._running or self._paused or len(data) < 16:
            return
        if (data[0] >> 6) != 2 or (data[1] & 0x7F) != 0x78:
            return
        _, _, _seq, _ts, ssrc = struct.unpack_from(">BBHII", data, 0)
        if ssrc == self._bot_ssrc:
            return
        header = bytes(data[:12])
        payload = data[12:]
        nonce = bytearray(24)
        nonce[:4] = payload[-4:]
        import nacl.secret

        decrypted = nacl.secret.Aead(self._secret_key).decrypt(bytes(payload[:-4]), header, bytes(nonce))
        if self._dave_session:
            with self._lock:
                user_id = self._ssrc_to_user.get(ssrc, 0)
            if user_id:
                try:
                    decrypted = self._dave_session.decrypt(user_id, "audio", decrypted)
                except Exception as e:
                    if "Unencrypted" not in str(e):
                        return
            # Unknown SSRC (no SPEAKING yet): skip DAVE, try Opus directly (Hermes does the same).
        try:
            if ssrc not in self._decoders:
                self._decoders[ssrc] = self.decoder_factory()
            pcm = self._decoders[ssrc].decode(decrypted)
            with self._lock:
                self._buffers[ssrc].extend(pcm)
        except Exception:
            with self._lock:
                self._decoders.pop(ssrc, None)


class FakeCodec:
    """Opus + DAVE media type for ``ScribeReceiver``: only payloads starting ``OPUS`` decode (to a
    frame carrying the payload so tests can tell packets apart); anything else raises."""

    def __init__(self) -> None:
        self.decoders = 0

    def new_decoder(self) -> Any:
        self.decoders += 1
        return self

    def decode(self, data: bytes) -> bytes:
        if not data.startswith(b"OPUS"):
            raise ValueError("corrupted stream")
        return FRAME[: len(FRAME) - len(data)] + data

    def audio_media(self) -> str:
        return "audio"


def dave_frame(owner: int, seq: int) -> bytes:
    """A fake DAVE frame of ``owner``: only their key opens it (to ``OPUS<seq>``)."""
    return b"DAVE" + struct.pack(">QI", owner, seq) + b"\x00\x00\xfa\xfa"


class FakeDave:
    """DAVE session: per-sender keys, a decryptor per group member, nonces usable once."""

    def __init__(self, members: list[int], bot: int = 9999, shared: Optional[dict[int, int]] = None) -> None:
        self.members = list(members)
        self.user_id = bot
        self.shared = shared or {}  # user -> another user whose key opens their frames too (never in Discord)
        self.seen: set[tuple[int, bytes]] = set()
        self.calls: list[int] = []
        self.ready = True

    def get_user_ids(self) -> list[str]:
        return [str(u) for u in self.members + [self.user_id]]

    def decrypt(self, user_id: int, media: Any, packet: bytes) -> bytes:
        self.calls.append(user_id)
        if user_id not in self.members:
            raise ValueError("Failed to decrypt: NoDecryptorForUser")
        if not packet.startswith(b"DAVE") or packet[-2:] != b"\xfa\xfa":
            raise ValueError("Failed to decrypt: UnencryptedWhenPassthroughDisabled")
        owner, seq = struct.unpack_from(">QI", packet, 4)
        if user_id != owner and self.shared.get(owner) != user_id:
            raise ValueError("Failed to decrypt: NoValidCryptorFound")
        if (user_id, packet) in self.seen:
            raise ValueError("Failed to decrypt: nonce already processed")
        self.seen.add((user_id, packet))
        return b"OPUS" + str(seq).encode()


def build_rtp_packet(ssrc: int = 100, seq: int = 1, timestamp: int = 960) -> bytes:
    header = struct.pack(">BBHII", 0x80, 0x78, seq, timestamp, ssrc)
    return header + b"\x00" * 20 + b"\x00\x00\x00\x01"


class FakeConn:
    def __init__(self, ssrc: int = 9999, dave: Any = None) -> None:
        self.secret_key = [0] * 32
        self.ssrc = ssrc
        self.dave_session = dave
        self.hook: Optional[Callable] = None
        self.listeners: list[Callable] = []
        self.sent: list[bytes] = []
        self.state = SimpleNamespace(name="connected")

    def add_socket_listener(self, cb: Callable) -> None:
        self.listeners.append(cb)

    def remove_socket_listener(self, cb: Callable) -> None:
        if cb in self.listeners:
            self.listeners.remove(cb)

    def send_packet(self, packet: bytes) -> None:
        self.sent.append(packet)


class FakeMember:
    def __init__(self, uid: int, name: str, bot: bool = False, channel: Any = None) -> None:
        self.id = uid
        self.display_name = name
        self.bot = bot
        self.voice = SimpleNamespace(channel=channel) if channel is not None else None
        self.nick: Optional[str] = None
        self.edits: list[dict] = []

    async def edit(self, **kw: Any) -> None:
        self.edits.append(kw)
        if "nick" in kw:
            self.nick = kw["nick"]


class FakeTextChannel:
    def __init__(self, cid: int, name: str = "general", guild: Any = None) -> None:
        self.id = cid
        self.name = name
        self.guild = guild
        self.sent: list[dict] = []

    async def send(self, content: str = "", **kw: Any) -> Any:
        msg = SimpleNamespace(id=len(self.sent) + 1, content=content, channel=self, **kw)
        self.sent.append({"content": content, **kw})
        return msg


class FakeVoiceClient:
    def __init__(self, channel: Any, conn: Optional[FakeConn] = None) -> None:
        self.channel = channel
        self._connection = conn or FakeConn()
        self.connected = True
        self.disconnects = 0
        self.forced: list[bool] = []
        self.timeout = 30.0
        self.user = SimpleNamespace(id=9999)

    def is_connected(self) -> bool:
        return self.connected

    async def disconnect(self, force: bool = False) -> None:
        self.disconnects += 1
        self.forced.append(force)
        self.connected = False


class FakeVoiceChannel(FakeTextChannel):
    """Voice channels double as their own text chat in Discord (``send`` works)."""

    def __init__(self, cid: int, name: str, guild: Any, members: Optional[list] = None) -> None:
        super().__init__(cid, name, guild)
        self.members: list[FakeMember] = members or []
        self.category = SimpleNamespace(name="Engineering")
        self.connects = 0
        self.vc: Optional[FakeVoiceClient] = None

    async def connect(self, **kw: Any) -> FakeVoiceClient:
        self.connects += 1
        self.vc = FakeVoiceClient(self)
        return self.vc


class FakeGuild:
    def __init__(self, gid: int = 100, name: str = "Acme") -> None:
        self.id = gid
        self.name = name
        self.members: dict[int, FakeMember] = {}
        self.channels: list[Any] = []
        self.me = FakeMember(9999, "Hermes", bot=True)
        self.voice_client: Any = None

    def get_member(self, uid: int) -> Optional[FakeMember]:
        return self.members.get(uid) or (self.me if uid == self.me.id else None)

    def get_channel(self, cid: int) -> Any:
        return next((c for c in self.channels if c.id == cid), None)

    @property
    def voice_channels(self) -> list:
        return [c for c in self.channels if isinstance(c, FakeVoiceChannel)]


class FakeBot:
    def __init__(self, guilds: list[FakeGuild]) -> None:
        self.guilds = guilds
        self.user = SimpleNamespace(id=9999)
        self.listeners: dict[str, list] = {}
        self.dynamic_items: list = []
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def get_guild(self, gid: int) -> Optional[FakeGuild]:
        return next((g for g in self.guilds if g.id == gid), None)

    def get_channel(self, cid: int) -> Any:
        for g in self.guilds:
            ch = g.get_channel(cid)
            if ch is not None:
                return ch
        return None

    async def fetch_channel(self, cid: int) -> Any:
        ch = self.get_channel(cid)
        if ch is None:
            raise LookupError(cid)
        return ch

    def add_listener(self, fn: Callable, name: Optional[str] = None) -> None:
        self.listeners.setdefault(name or fn.__name__, []).append(fn)

    def remove_listener(self, fn: Callable, name: Optional[str] = None) -> None:
        self.listeners.get(name or fn.__name__, []).remove(fn)

    def add_dynamic_items(self, *items: type) -> None:
        self.dynamic_items.extend(items)

    def remove_dynamic_items(self, *items: type) -> None:
        for item in items:
            if item in self.dynamic_items:
                self.dynamic_items.remove(item)


class FakeAdapter:
    def __init__(self, bot: FakeBot) -> None:
        self._client = bot
        self._voice_clients: dict[int, Any] = {}
        self._voice_locks: dict[int, asyncio.Lock] = {}
        self._allowed_user_ids: set = set()
        self._allowed_role_ids: set = set()
        self.left: list[int] = []

    async def _resolve_channel(self, channel_id: Any) -> Any:
        return self._client.get_channel(int(channel_id)) or await self._client.fetch_channel(int(channel_id))

    async def get_user_voice_channel(self, guild_id: int, user_id: str) -> Any:
        guild = self._client.get_guild(guild_id)
        member = guild.get_member(int(user_id)) if guild else None
        return member.voice.channel if member and member.voice else None

    async def leave_voice_channel(self, guild_id: int) -> None:
        self.left.append(guild_id)
        vc = self._voice_clients.pop(guild_id, None)
        if vc and vc.is_connected():
            await vc.disconnect()
