"""Discord message/thread/interaction fakes for the notes sink and button handlers."""
from __future__ import annotations

import itertools
from types import SimpleNamespace
from typing import Any, Optional

_ids = itertools.count(10_000)


class FakeMessage:
    def __init__(self, channel: "FakeChannel", content: str, view: Any) -> None:
        self.id = next(_ids)
        self.channel = channel
        self.content = content
        self.view = view
        self.deleted = False
        self.jump_url = f"https://discord.com/channels/1/{channel.id}/{self.id}"
        self.edits = 0

    async def edit(self, *, content: Optional[str] = None, view: Any = None, **kw: Any) -> "FakeMessage":
        if self.deleted:
            raise LookupError("Unknown Message")
        self.content, self.view = content if content is not None else self.content, view
        self.edits += 1
        return self

    async def delete(self) -> None:
        self.deleted = True
        self.channel.messages.pop(self.id, None)

    async def create_thread(self, *, name: str, auto_archive_duration: int = 1440, **kw: Any) -> "FakeChannel":
        if not self.channel.threads_ok:
            raise PermissionError("Missing Permissions")
        thread = FakeChannel(next(_ids), name, parent=self.channel)
        self.channel.bot.channels[thread.id] = thread
        return thread


class FakeChannel:
    def __init__(self, cid: int, name: str, *, parent: Optional["FakeChannel"] = None, bot: Any = None,
                 threads_ok: bool = True) -> None:
        self.id = cid
        self.name = name
        self.parent = parent
        self.bot = bot or (parent.bot if parent else None)
        self.threads_ok = threads_ok
        self.messages: dict[int, FakeMessage] = {}
        self.guild = SimpleNamespace(id=1)

    async def send(self, content: str = "", *, view: Any = None, **kw: Any) -> FakeMessage:
        msg = FakeMessage(self, content, view)
        self.messages[msg.id] = msg
        return msg

    def get_partial_message(self, mid: int) -> FakeMessage:
        return self.messages.get(int(mid)) or _Gone(self)

    def ordered(self) -> list[FakeMessage]:
        return sorted(self.messages.values(), key=lambda m: m.id)


class _Gone(FakeMessage):
    def __init__(self, channel: FakeChannel) -> None:
        super().__init__(channel, "", None)
        self.deleted = True


class FakeBot:
    def __init__(self) -> None:
        self.channels: dict[int, FakeChannel] = {}

    def add(self, cid: int, name: str, **kw: Any) -> FakeChannel:
        ch = FakeChannel(cid, name, bot=self, **kw)
        self.channels[cid] = ch
        return ch


class FakeAdapter:
    def __init__(self, bot: FakeBot, home: Optional[str] = None) -> None:
        self._client = bot
        self.config = SimpleNamespace(home_channel=SimpleNamespace(chat_id=home) if home else None)
        self._allowed_user_ids: set = set()
        self._allowed_role_ids: set = set()

    async def _resolve_channel(self, cid: Any) -> FakeChannel:
        ch = self._client.channels.get(int(cid))
        if ch is None:
            raise LookupError(f"Unknown Channel {cid}")
        return ch


class FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.sent: list[dict] = []

    async def defer(self, **kw: Any) -> None:
        self.deferred = True

    async def send_message(self, content: str = "", **kw: Any) -> None:
        self.sent.append({"content": content, **kw})

    def is_done(self) -> bool:
        return self.deferred or bool(self.sent)


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content: str = "", **kw: Any) -> None:
        self.sent.append({"content": content, **kw})


class FakeInteraction:
    def __init__(self, user_id: int, values: Optional[list[str]] = None) -> None:
        self.user = SimpleNamespace(id=user_id, roles=[])
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.data = {"values": values or []}

    def replies(self) -> str:
        return "\n".join(m["content"] for m in self.response.sent + self.followup.sent)
