"""Discord message/thread/interaction fakes for the notes sink and button handlers."""
from __future__ import annotations

import itertools
from types import SimpleNamespace
from typing import Any, Optional

_ids = itertools.count(10_000)
BOT_USER_ID = 1


class FakeMessage:
    def __init__(self, channel: "FakeChannel", content: str, view: Any) -> None:
        self.id = next(_ids)
        self.channel = channel
        self.content = content
        self.view = view
        self.deleted = False
        self.jump_url = f"https://discord.com/channels/1/{channel.id}/{self.id}"
        self.edits = 0
        self.author = SimpleNamespace(id=BOT_USER_ID)  # everything in these fakes is posted by the bot
        self.file: Any = None

    @property
    def attachments(self) -> list:
        f = self.file
        name = f.get("name") if isinstance(f, dict) else getattr(f, "filename", None)
        return [SimpleNamespace(filename=name)] if name else []

    async def edit(self, *, content: Optional[str] = None, view: Any = None, **kw: Any) -> "FakeMessage":
        if self.deleted:
            raise LookupError("Unknown Message")
        if self.channel.archived:
            raise FakeHTTPError(400, 50083, "Operation cannot be performed on an archived thread")
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
                 threads_ok: bool = True, kind: str = "text", category_id: Optional[int] = None,
                 position: int = 0, can_post: bool = True, guild_id: Optional[int] = None,
                 can_attach: bool = True, public: bool = True, nsfw: bool = False) -> None:
        self.id = cid
        self.name = name
        self.parent = parent
        self.bot = bot or (parent.bot if parent else None)
        self.threads_ok = threads_ok
        self.messages: dict[int, FakeMessage] = {}
        self.guild_id = guild_id if guild_id is not None else (parent.guild_id if parent else None)
        self._guild_stub = SimpleNamespace(id=1, me=SimpleNamespace(id=BOT_USER_ID))
        self.is_dm = False
        self.can_attach = can_attach
        self.type = kind
        self.category_id = category_id
        self.position = position
        self.can_post = can_post
        self.public = public  # visible to @everyone
        self.nsfw = nsfw
        self.fail_sends = 0
        self.archived = False
        self.applied_tags: list = []
        self.thread_edits: list[dict] = []

    @property
    def jump_url(self) -> str:
        return f"https://discord.com/channels/1/{self.id}"

    async def delete(self) -> None:  # a thread / forum post (discord.py ``Thread.delete``)
        self.bot.channels.pop(self.id, None)

    async def edit(self, **kw: Any) -> "FakeChannel":
        """Thread edit (name, applied_tags, archived) as in discord.py ``Thread.edit``."""
        if self.parent is not None and self.id not in self.bot.channels:
            raise LookupError("Unknown Channel")
        self.thread_edits.append(kw)
        self.name = kw.get("name", self.name)
        self.applied_tags = list(kw.get("applied_tags", self.applied_tags))
        self.archived = kw.get("archived", self.archived)
        return self

    @property
    def guild(self) -> Any:
        if self.is_dm:
            return None  # a DM has no server (discord.py: DMChannel has no ``guild``)
        if self.bot is not None:
            return self.bot.get_guild(self.guild_id or self.bot.guild.id) or self._guild_stub
        return self._guild_stub

    def permissions_for(self, member: Any) -> SimpleNamespace:
        if getattr(member, "name", None) == "@everyone":
            return SimpleNamespace(view_channel=self.public, send_messages=self.public)
        ok = self.can_post
        return SimpleNamespace(view_channel=ok, send_messages=ok, create_public_threads=ok and self.threads_ok,
                               send_messages_in_threads=ok, attach_files=ok and self.can_attach)

    @property
    def mention(self) -> str:
        return f"<#{self.id}>"

    async def send(self, content: str = "", *, view: Any = None, **kw: Any) -> FakeMessage:
        if self.parent is not None and self.id not in self.bot.channels:
            raise LookupError("Unknown Channel")  # a deleted thread / forum post
        if self.fail_sends:
            self.fail_sends -= 1
            raise RuntimeError("503 Service Unavailable")
        msg = FakeMessage(self, content, view)
        msg.file = kw.get("file")  # attachments (transcript, DESIGN §17.3)
        self.messages[msg.id] = msg
        return msg

    async def history(self, *, limit: int = 100, **kw: Any):
        for msg in sorted(self.messages.values(), key=lambda m: m.id, reverse=True)[:limit]:
            yield msg

    def get_partial_message(self, mid: int) -> FakeMessage:
        return self.messages.get(int(mid)) or _Gone(self)

    def ordered(self) -> list[FakeMessage]:
        return sorted(self.messages.values(), key=lambda m: m.id)


class FakeHTTPError(Exception):
    def __init__(self, status: int, code: int, text: str) -> None:
        super().__init__(f"{status} (error code: {code}): {text}")
        self.status = status
        self.code = code


class FakeForum(FakeChannel):
    """A forum (type 15) or media (type 16) channel: no ``send``; ``create_thread`` opens a post and
    returns ``(thread, message)`` like discord.py's ``ThreadWithMessage``."""

    def __init__(self, cid: int, name: str, *, tags: tuple[str, ...] = (), require_tag: bool = False,
                 media: bool = False, **kw: Any) -> None:
        super().__init__(cid, name, kind="media" if media else "forum", **kw)
        self.available_tags = [SimpleNamespace(id=cid * 10 + i, name=n) for i, n in enumerate(tags, start=1)]
        self.flags = SimpleNamespace(require_tag=require_tag)
        self.posts: list[FakeChannel] = []
        self.can_post_in_threads = True

    def permissions_for(self, member: Any) -> SimpleNamespace:
        p = super().permissions_for(member)
        p.send_messages_in_threads = bool(getattr(p, "send_messages", False)) and self.can_post_in_threads
        return p

    async def send(self, *a: Any, **kw: Any) -> FakeMessage:  # discord.py: ForumChannel has no send
        raise AttributeError("'ForumChannel' object has no attribute 'send'")

    async def create_thread(self, *, name: str, content: str = "", view: Any = None, applied_tags: Any = (),
                            **kw: Any) -> tuple[FakeChannel, FakeMessage]:
        if self.fail_sends:
            self.fail_sends -= 1
            raise RuntimeError("503 Service Unavailable")
        if self.flags.require_tag and not applied_tags:
            raise FakeHTTPError(400, 40067, "A tag is required to create a forum post in this channel")
        assert len(list(applied_tags)) <= 5
        thread = FakeChannel(next(_ids), name, parent=self)
        thread.type = "public_thread"
        thread.applied_tags = list(applied_tags)
        self.bot.channels[thread.id] = thread
        self.posts.append(thread)
        first = await thread.send(content, view=view)
        return thread, first

    def delete_post(self, thread: FakeChannel) -> None:
        self.bot.channels.pop(thread.id, None)


class _Gone(FakeMessage):
    def __init__(self, channel: FakeChannel) -> None:
        super().__init__(channel, "", None)
        self.deleted = True


class FakeForbidden(Exception):
    def __init__(self) -> None:
        super().__init__("403 Forbidden (error code: 50007): Cannot send messages to this user")
        self.code = 50007


class FakeUser:
    def __init__(self, uid: int, *, dms_open: bool = True) -> None:
        self.id = uid
        self.dms_open = dms_open
        self.dm = FakeChannel(90_000 + uid, f"dm-{uid}")
        self.dm.is_dm = True

    async def send(self, content: str = "", *, view: Any = None, **kw: Any) -> FakeMessage:
        if not self.dms_open:
            raise FakeForbidden()
        return await self.dm.send(content, view=view, **kw)

    async def create_dm(self) -> FakeChannel:
        return self.dm


class FakeGuild:
    def __init__(self, bot: "FakeBot", gid: int = 100, name: str = "Example Team") -> None:
        self.id = gid
        self.name = name
        self.bot = bot
        self.me = SimpleNamespace(id=1)
        self.default_role = SimpleNamespace(id=gid, name="@everyone")
        self.system_channel_id: Optional[int] = None

    @property
    def system_channel(self) -> Optional[FakeChannel]:
        return self.bot.channels.get(self.system_channel_id) if self.system_channel_id else None

    @property
    def channels(self) -> list[FakeChannel]:
        return [c for c in self.bot.channels.values() if c.parent is None and not c.is_dm
                and (c.guild_id or self.bot.guild.id) == self.id]


class FakeBot:
    def __init__(self) -> None:
        self.channels: dict[int, FakeChannel] = {}
        self.users: dict[int, FakeUser] = {}
        self.guild = FakeGuild(self)
        self.extra_guilds: list[FakeGuild] = []

    @property
    def guilds(self) -> list[FakeGuild]:
        return [self.guild, *self.extra_guilds]

    def add_guild(self, gid: int, name: str) -> FakeGuild:
        g = FakeGuild(self, gid, name)
        self.extra_guilds.append(g)
        return g

    def add(self, cid: int, name: str, **kw: Any) -> FakeChannel:
        ch = FakeChannel(cid, name, bot=self, **kw)
        self.channels[cid] = ch
        return ch

    def add_forum(self, cid: int, name: str, **kw: Any) -> FakeForum:
        ch = FakeForum(cid, name, bot=self, **kw)
        self.channels[cid] = ch
        return ch

    def get_guild(self, gid: int) -> Optional[FakeGuild]:
        return next((g for g in self.guilds if g.id == int(gid)), None)

    def get_channel(self, cid: int) -> Optional[FakeChannel]:
        return self.channels.get(int(cid))

    def user(self, uid: int, **kw: Any) -> FakeUser:
        self.users[uid] = FakeUser(uid, **kw)
        self.channels[self.users[uid].dm.id] = self.users[uid].dm
        return self.users[uid]

    async def fetch_user(self, uid: int) -> FakeUser:
        if int(uid) not in self.users:
            raise LookupError(f"Unknown User {uid}")
        return self.users[int(uid)]


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
        self.defers: list[dict] = []
        self.sent: list[dict] = []

    async def defer(self, **kw: Any) -> None:
        self.deferred = True
        self.defers.append(kw)

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
    def __init__(self, user_id: int, values: Optional[list[str]] = None, *, message: Any = None,
                 ephemeral: bool = False, dm: bool = False) -> None:
        self.user = SimpleNamespace(id=user_id, roles=[])
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.data = {"values": values or []}
        self.message = message or SimpleNamespace(id=1, flags=SimpleNamespace(ephemeral=ephemeral))
        self.guild = None if dm else SimpleNamespace(id=100)
        self.original_edits: list[dict] = []

    async def edit_original_response(self, **kw: Any) -> None:
        self.original_edits.append(kw)

    def replies(self) -> str:
        return "\n".join(m["content"] for m in self.response.sent + self.followup.sent)
