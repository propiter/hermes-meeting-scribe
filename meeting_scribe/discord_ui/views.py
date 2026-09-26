"""discord.py glue: persistent ``DynamicItem`` buttons and the project select (DESIGN §8).

Each :class:`ViewKit` builds its OWN DynamicItem subclasses bound to one ``ButtonActions``: Hermes
may run several profiles/bots in one process and each bot must route clicks to its own runtime.
discord.py keys dynamic items by compiled template, so buttons and the select use disjoint
templates. Registered with ``bot.add_dynamic_items`` they survive restarts: clicks on messages
posted before a restart are parsed from ``custom_id`` alone.
"""
from __future__ import annotations

import re
from typing import Any, Optional, Protocol, Sequence

import discord

from ..domain.models import Candidate
from .render import ButtonSpec

BUTTON_TEMPLATE = r"mscribe:(?P<action>ok|lin|no|prj|allk|alll):(?P<meeting>[a-z0-9]{1,16}):(?P<item>[A-Za-z0-9_-]{1,40})"
SELECT_TEMPLATE = r"mscribe:(?P<action>psel):(?P<meeting>[a-z0-9]{1,16}):(?P<item>[A-Za-z0-9_-]{1,40})"
_STYLES = {"success": discord.ButtonStyle.success, "primary": discord.ButtonStyle.primary,
           "danger": discord.ButtonStyle.danger, "secondary": discord.ButtonStyle.secondary}


class Handler(Protocol):
    async def handle(self, interaction: Any, action: str, meeting_id: str, item_id: str,
                     values: Optional[Sequence[str]] = None) -> None: ...


def _clip(text: str, limit: int = 100) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


class ViewKit:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        kit = self

        class ScribeButton(discord.ui.DynamicItem[discord.ui.Button], template=BUTTON_TEMPLATE):  # type: ignore[misc]
            def __init__(self, button: discord.ui.Button) -> None:
                super().__init__(button)
                m = re.fullmatch(BUTTON_TEMPLATE, button.custom_id or "")
                assert m is not None  # DynamicItem.__init__ already validated the template
                self.action, self.meeting_id, self.item_id = m["action"], m["meeting"], m["item"]

            @classmethod
            async def from_custom_id(cls, interaction: Any, item: Any, match: re.Match[str]) -> "ScribeButton":
                return cls(discord.ui.Button(custom_id=item.custom_id, label=getattr(item, "label", None)))

            async def callback(self, interaction: Any) -> None:
                await kit.handler.handle(interaction, self.action, self.meeting_id, self.item_id)

        class ScribeSelect(discord.ui.DynamicItem[discord.ui.Select], template=SELECT_TEMPLATE):  # type: ignore[misc]
            def __init__(self, select: discord.ui.Select) -> None:
                super().__init__(select)
                m = re.fullmatch(SELECT_TEMPLATE, select.custom_id or "")
                assert m is not None
                self.meeting_id, self.item_id = m["meeting"], m["item"]

            @classmethod
            async def from_custom_id(cls, interaction: Any, item: Any, match: re.Match[str]) -> "ScribeSelect":
                options = list(getattr(item, "options", None) or [discord.SelectOption(label="-")])
                return cls(discord.ui.Select(custom_id=item.custom_id, options=options))

            async def callback(self, interaction: Any) -> None:
                values = list((getattr(interaction, "data", None) or {}).get("values") or ())
                await kit.handler.handle(interaction, "psel", self.meeting_id, self.item_id, values)

        self.button_cls = ScribeButton
        self.select_cls = ScribeSelect

    def register(self, bot: Any) -> None:
        bot.add_dynamic_items(self.button_cls, self.select_cls)

    def view(self, buttons: Sequence[ButtonSpec]) -> Optional[discord.ui.View]:
        if not buttons:
            return None
        view = discord.ui.View(timeout=None)
        for spec in buttons:
            button = discord.ui.Button(label=_clip(spec.label, 80), custom_id=spec.custom_id,
                                       style=_STYLES.get(spec.style, discord.ButtonStyle.secondary),
                                       emoji=spec.emoji, row=spec.row)
            item = self.button_cls(button)
            item.row = spec.row
            view.add_item(item)
        return view

    def send_kwargs(self) -> dict[str, Any]:
        return {"allowed_mentions": discord.AllowedMentions(users=True, roles=False, everyone=False)}

    def project_view(self, meeting_id: str, candidates: Sequence[Candidate]) -> discord.ui.View:
        # Select values are capped at 100 chars; real keys ("linear:<uuid>", "hermes:<slug>") fit, and a
        # truncated key could never be resolved again, so oversized ones are left out of the picker.
        options = [discord.SelectOption(label=_clip(c.name or c.key), value=c.key, description=_clip(c.source))
                   for c in candidates if len(c.key) <= 100][:25]
        select = discord.ui.Select(custom_id=f"mscribe:psel:{meeting_id}:all", options=options, min_values=1,
                                   max_values=1)
        view = discord.ui.View(timeout=None)
        view.add_item(self.select_cls(select))
        return view
