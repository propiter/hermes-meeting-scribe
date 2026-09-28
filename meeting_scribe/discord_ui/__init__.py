"""Discord UI (Phase B, DESIGN §8, §16): notes/task sink, persistent buttons and the platform handler.

``install(ctx, runtime)`` registers ONE ``ctx.register_platform_handler('discord', factory)``.
Hermes calls ``factory(bot, adapter)`` on the gateway loop at every connect (and on plugin
re-wire). The factory: stores a weak adapter ref + the loop, attaches the capture controller,
adds the ``on_voice_state_update`` listener (auto-join), registers the DynamicItem classes and
starts the pipeline worker with the live meeting ids so it never processes a recording in progress.
A reconnect with a NEW bot moves the listener; calling twice with the same bot is a no-op.

Reload safety (review W3): Hermes skips a factory whose ``(plugin, __qualname__)`` it already
wired on the same client, so every install gets a UNIQUE qualname — a reloaded plugin re-wires
the live bot. ``ctx.on_unload`` detaches this install from the bot (listener + DynamicItems) on the
gateway loop (``call_soon_threadsafe``/``run_coroutine_threadsafe``; unload runs on a worker
thread) so an old instance can never keep auto-recording behind the new one's back.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import sys
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from ..domain.models import ActionItem, Meeting
from .actions import ButtonActions
from .guild import DiscordChannelCatalog
from .render import RenderOptions
from .sink import DiscordNotesSink

log = logging.getLogger(__name__)
_STATES: "weakref.WeakKeyDictionary[Any, UiState]" = weakref.WeakKeyDictionary()
_INSTALLS = itertools.count(1)
DETACH_TIMEOUT = 10.0

__all__ = ["install", "state_for", "UiState", "DiscordNotesSink"]


@dataclass
class UiState:
    runtime: Any
    actions: ButtonActions
    kit: Any
    sink: DiscordNotesSink
    adapter_ref: Optional[Callable[[], Any]] = None
    loop: Optional[asyncio.AbstractEventLoop] = None
    bot_ref: Optional[Callable[[], Any]] = None
    autojoin: Any = None
    listener: Optional[Callable[..., Any]] = field(default=None, repr=False)
    detached: bool = False

    @property
    def adapter(self) -> Any:
        return self.adapter_ref() if self.adapter_ref is not None else None


def state_for(runtime: Any) -> UiState:
    return _STATES[runtime]


def membership_for(runtime: Any) -> Optional[Callable[[str, Sequence[str]], Optional[set[str]]]]:
    """For ``/meeting`` in a DM with several spaces: which of ``guild_ids`` the user is a member of,
    read from the connected bot's member cache (``None`` while Discord is not connected)."""
    state = _STATES.get(runtime)
    client = getattr(state.adapter, "_client", None) if state is not None else None
    if client is None:
        return None

    def check(user_id: str, guild_ids: Sequence[str]) -> Optional[set[str]]:
        found = set()
        for gid in guild_ids:
            guild = client.get_guild(int(gid)) if str(gid).isdigit() else None
            if guild is not None and guild.get_member(int(user_id)) is not None:
                found.add(str(gid))
        return found
    return check


def _check_auth(state: UiState) -> Callable[[Any], bool]:
    def check(interaction: Any) -> bool:
        adapter = state.adapter
        fn = getattr(sys.modules.get(type(adapter).__module__), "_component_check_auth", None) if adapter else None
        if not callable(fn):
            return False  # fail closed: without Hermes' helper only owners may click
        return bool(fn(interaction, getattr(adapter, "_allowed_user_ids", set()),
                       getattr(adapter, "_allowed_role_ids", set())))
    return check


def _render_options(runtime: Any) -> Callable[[Meeting], RenderOptions]:
    def options(meeting: Meeting) -> RenderOptions:
        space = getattr(meeting, "space", "") or None  # the meeting's team decides (DESIGN §23)
        s = runtime.settings(space)
        owners = set(runtime.owners(space))
        try:
            linear_on = s.linear_mode != "off" and runtime.linear_backend() is not None
        except Exception:  # Linear misconfigured: no Linear buttons rather than no notes
            log.exception("meeting-scribe: linear backend lookup failed")
            linear_on = False

        def is_owner_item(item: ActionItem) -> bool:
            return bool(item.owner_speaker_id) and item.owner_speaker_id in owners
        return RenderOptions(lang=s.ui_language, kanban_on=s.kanban_mode != "off", linear_on=linear_on,
                             is_owner_item=is_owner_item)
    return options


def _factory(state: UiState) -> Callable[[Any, Any], None]:
    def meeting_scribe_discord(bot: Any, adapter: Any) -> None:
        if state.detached:
            return  # an unloaded install must never re-attach
        runtime = state.runtime
        state.adapter_ref = weakref.ref(adapter)
        try:
            state.loop = asyncio.get_running_loop()
        except RuntimeError:
            state.loop = getattr(bot, "loop", None)
        previous = state.bot_ref() if state.bot_ref is not None else None
        if previous is not bot:
            if previous is not None and state.listener is not None:
                try:
                    previous.remove_listener(state.listener, "on_voice_state_update")
                except Exception as exc:  # old client already torn down
                    log.debug("meeting-scribe: removing old listener failed: %s", exc)
            state.bot_ref = weakref.ref(bot)
            if state.listener is not None:
                bot.add_listener(state.listener, "on_voice_state_update")
            state.kit.register(bot)
        adopt = getattr(runtime, "adopt_guilds", None)
        if callable(adopt):
            try:  # first connect after the spaces baseline: ``main`` takes the bot's servers
                adopt(list(getattr(bot, "guilds", None) or ()))
            except Exception:  # storage problems: commands and doctor report them
                log.exception("meeting-scribe: adopting the bot's servers failed")
        capture = runtime.capture
        if capture is not None and hasattr(capture, "attach"):
            capture.attach(bot, adapter)
        try:
            # The Discord-connected gateway owns capture: only it may close orphan recordings.
            runtime.start_pipeline(capture.live_meeting_ids() if capture is not None else (),
                                   owns_capture=capture is not None)
        except Exception:  # storage problems: commands and doctor report them
            log.exception("meeting-scribe: pipeline start on connect failed")
    seq = next(_INSTALLS)
    meeting_scribe_discord.__name__ = f"meeting_scribe_discord_{seq}"
    meeting_scribe_discord.__qualname__ = f"meeting_scribe_discord_{seq}"
    return meeting_scribe_discord


def _detach_from_bot(state: UiState) -> None:
    """Loop-side: remove our listener and DynamicItems from the bot this install is wired to."""
    bot = state.bot_ref() if state.bot_ref is not None else None
    if bot is None:
        return
    if state.listener is not None:
        try:
            bot.remove_listener(state.listener, "on_voice_state_update")
        except Exception as exc:  # already gone / client torn down
            log.debug("meeting-scribe: removing listener on unload failed: %s", exc)
    remove_items = getattr(bot, "remove_dynamic_items", None)
    if callable(remove_items):
        try:
            remove_items(state.kit.button_cls, state.kit.select_cls)
        except Exception as exc:
            log.debug("meeting-scribe: removing dynamic items on unload failed: %s", exc)
    state.bot_ref = None


def unload(state: UiState) -> None:
    """``ctx.on_unload`` callback (runs on a worker thread, or on the loop in tests)."""
    state.detached = True
    if state.autojoin is not None:
        state.autojoin.close()
    loop = state.loop
    try:
        on_loop = asyncio.get_running_loop() is loop
    except RuntimeError:
        on_loop = False
    if loop is None or on_loop or not loop.is_running():
        _detach_from_bot(state)
        return

    async def detach() -> None:
        _detach_from_bot(state)
    try:
        asyncio.run_coroutine_threadsafe(detach(), loop).result(DETACH_TIMEOUT)
    except Exception:  # loop wedged/closing: the bot is going away with it
        log.exception("meeting-scribe: detaching from the Discord client failed")


def install(ctx: Any, runtime: Any) -> UiState:
    from .views import ViewKit  # discord.py needed from here on

    holder: dict[str, UiState] = {}
    actions = ButtonActions(service=runtime.service, settings=runtime.settings, owners=runtime.owners,
                            check_auth=lambda i: _check_auth(holder["s"])(i), sink=lambda: holder["s"].sink,
                            project_view=lambda mid, cands: holder["s"].kit.project_view(mid, cands),
                            move_view=lambda mid, iid, opts: holder["s"].kit.move_view(mid, iid, opts),
                            buttons_view=lambda specs: holder["s"].kit.view(specs))
    kit = ViewKit(actions)
    sink = DiscordNotesSink(settings=runtime.settings, service=runtime.service, adapter=lambda: holder["s"].adapter,
                            loop=lambda: holder["s"].loop, options=_render_options(runtime), views=kit,
                            space_guilds=getattr(runtime, "space_guilds", None))
    state = UiState(runtime=runtime, actions=actions, kit=kit, sink=sink)
    holder["s"] = state
    capture = runtime.capture
    if capture is not None and hasattr(capture, "start_in"):
        from ..capture.autojoin import AutoJoiner

        state.autojoin = AutoJoiner(capture, runtime.settings, space_of=getattr(capture, "space_of", None))

        async def on_voice_state_update(member: Any, before: Any, after: Any) -> None:
            try:
                await state.autojoin.on_voice_state_update(member, before, after)
            except Exception:  # never let a plugin bug surface as a discord.py listener error storm
                log.exception("meeting-scribe: voice state handling failed")
        state.listener = on_voice_state_update
        if hasattr(capture, "on_session_end"):
            capture.on_session_end.append(state.autojoin.note_session_end)
    on_unload = getattr(ctx, "on_unload", None)
    if callable(on_unload):
        def meeting_scribe_discord_unload() -> None:
            unload(state)
        on_unload(meeting_scribe_discord_unload)
    _STATES[runtime] = state
    runtime.add_sink(sink)
    add_catalog = getattr(runtime, "add_catalog", None)
    if callable(add_catalog):  # the guild's channels become project candidates (§16)
        add_catalog(DiscordChannelCatalog(adapter=lambda: state.adapter, loop=lambda: state.loop,
                                          ignore_prefixes=lambda: runtime.settings().channel_name_ignore_prefixes,
                                          guild_for=sink.guild_for,
                                          ignore_for=lambda m: runtime.settings(
                                              getattr(m, "space", "") or None).channel_name_ignore_prefixes))
    ctx.register_platform_handler("discord", _factory(state))
    return state
