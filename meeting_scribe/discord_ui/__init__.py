"""Discord UI (Phase B, DESIGN §8): notes sink, persistent buttons and the platform handler.

``install(ctx, runtime)`` registers ONE ``ctx.register_platform_handler('discord', factory)``.
Hermes calls ``factory(bot, adapter)`` on the gateway loop at every connect (and on plugin
re-wire). The factory: stores a weak adapter ref + the loop, attaches the capture controller,
adds the ``on_voice_state_update`` listener (auto-join), registers the DynamicItem classes and
starts the pipeline worker with the live meeting ids so it never processes a recording in progress.
A reconnect with a NEW bot moves the listener; calling twice with the same bot is a no-op.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..domain.models import ActionItem, Meeting
from .actions import ButtonActions
from .render import RenderOptions
from .sink import DiscordNotesSink

log = logging.getLogger(__name__)
_STATES: "weakref.WeakKeyDictionary[Any, UiState]" = weakref.WeakKeyDictionary()

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

    @property
    def adapter(self) -> Any:
        return self.adapter_ref() if self.adapter_ref is not None else None


def state_for(runtime: Any) -> UiState:
    return _STATES[runtime]


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
        s = runtime.settings()
        owners = set(runtime.owners())
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
        capture = runtime.capture
        if capture is not None and hasattr(capture, "attach"):
            capture.attach(bot, adapter)
        try:
            runtime.start_pipeline(capture.live_meeting_ids() if capture is not None else ())
        except Exception:  # storage problems: commands and doctor report them
            log.exception("meeting-scribe: pipeline start on connect failed")
    return meeting_scribe_discord


def install(ctx: Any, runtime: Any) -> UiState:
    from .views import ViewKit  # discord.py needed from here on

    holder: dict[str, UiState] = {}

    async def refresh(meeting_id: str) -> None:
        await holder["s"].sink.refresh(meeting_id)

    actions = ButtonActions(service=runtime.service, settings=runtime.settings, owners=runtime.owners,
                            check_auth=lambda i: _check_auth(holder["s"])(i), refresh=refresh,
                            project_view=lambda mid, cands: holder["s"].kit.project_view(mid, cands))
    kit = ViewKit(actions)
    sink = DiscordNotesSink(settings=runtime.settings, service=runtime.service, adapter=lambda: holder["s"].adapter,
                            loop=lambda: holder["s"].loop, options=_render_options(runtime), views=kit)
    state = UiState(runtime=runtime, actions=actions, kit=kit, sink=sink)
    holder["s"] = state
    capture = runtime.capture
    if capture is not None and hasattr(capture, "start_in"):
        from ..capture.autojoin import AutoJoiner

        state.autojoin = AutoJoiner(capture, runtime.settings)

        async def on_voice_state_update(member: Any, before: Any, after: Any) -> None:
            try:
                await state.autojoin.on_voice_state_update(member, before, after)
            except Exception:  # never let a plugin bug surface as a discord.py listener error storm
                log.exception("meeting-scribe: voice state handling failed")
        state.listener = on_voice_state_update
        on_unload = getattr(ctx, "on_unload", None)
        if callable(on_unload):
            on_unload(state.autojoin.close)
    _STATES[runtime] = state
    runtime.add_sink(sink)
    ctx.register_platform_handler("discord", _factory(state))
    return state
