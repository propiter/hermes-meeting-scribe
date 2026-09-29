"""discord.py glue: persistent ``DynamicItem`` buttons, selects and the task panel (DESIGN §8, §16).

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
from ..i18n import t
from .render import ButtonSpec
from .render_tasks import TaskPanel

BUTTON_TEMPLATE = (r"mscribe:(?P<action>ok|lin|no|prj|allk|alll|mine|pg|shp|shd|sha|shc|spk|scfm):(?P<meeting>[a-z0-9]{1,16}):"
                   r"(?P<item>[A-Za-z0-9_-]{1,40})")
SELECT_TEMPLATE = r"mscribe:(?P<action>psel|tsel|ssel):(?P<meeting>[a-z0-9]{1,16}):(?P<item>[A-Za-z0-9_-]{1,40})"
_STYLES = {"success": discord.ButtonStyle.success, "primary": discord.ButtonStyle.primary,
           "danger": discord.ButtonStyle.danger, "secondary": discord.ButtonStyle.secondary}


class Handler(Protocol):
    async def handle(self, interaction: Any, action: str, meeting_id: str, item_id: str,
                     values: Optional[Sequence[str]] = None) -> None: ...


def source_label(source: str, lang: str) -> str:
    """Where a project candidate comes from, in plain words (``kanban`` → "Kanban board")."""
    key = f"ui.source.{source}"
    label = t(key, lang)
    return "" if label == key else label


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
                self.action, self.meeting_id, self.item_id = m["action"], m["meeting"], m["item"]

            @classmethod
            async def from_custom_id(cls, interaction: Any, item: Any, match: re.Match[str]) -> "ScribeSelect":
                options = list(getattr(item, "options", None) or [discord.SelectOption(label="-")])
                return cls(discord.ui.Select(custom_id=item.custom_id, options=options))

            async def callback(self, interaction: Any) -> None:
                values = list((getattr(interaction, "data", None) or {}).get("values") or ())
                await kit.handler.handle(interaction, self.action, self.meeting_id, self.item_id, values)

        self.button_cls = ScribeButton
        self.select_cls = ScribeSelect

    def register(self, bot: Any) -> None:
        bot.add_dynamic_items(self.button_cls, self.select_cls)

    def _button(self, spec: ButtonSpec, *, row: Optional[int]) -> Any:
        button = discord.ui.Button(label=_clip(spec.label, 80), custom_id=spec.custom_id,
                                   style=_STYLES.get(spec.style, discord.ButtonStyle.secondary),
                                   emoji=spec.emoji, row=row)
        item = self.button_cls(button)
        if row is not None:
            item.row = row
        return item

    def view(self, buttons: Sequence[ButtonSpec]) -> Optional[discord.ui.View]:
        if not buttons:
            return None
        view = discord.ui.View(timeout=None)
        for spec in buttons:
            view.add_item(self._button(spec, row=spec.row))
        return view

    def panel_view(self, panel: TaskPanel) -> discord.ui.LayoutView:
        """Components v2: header, then per task its text with ITS button row right under it, then nav."""
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.TextDisplay(panel.header))
        for block in panel.blocks:
            view.add_item(discord.ui.TextDisplay(block.content))
            if block.buttons:
                row: discord.ui.ActionRow = discord.ui.ActionRow()
                for spec in block.buttons[:5]:
                    row.add_item(self._button(spec, row=None))
                view.add_item(row)
        if panel.nav:
            nav: discord.ui.ActionRow = discord.ui.ActionRow()
            for spec in panel.nav[:5]:
                nav.add_item(self._button(spec, row=None))
            view.add_item(nav)
        return view

    def move_view(self, meeting_id: str, item_id: str, options: Sequence[tuple[str, str]]) -> discord.ui.View:
        opts = [discord.SelectOption(label=_clip(label), value=str(value)) for value, label in options][:25]
        select = discord.ui.Select(custom_id=f"mscribe:tsel:{meeting_id}:{item_id}", options=opts, min_values=1,
                                   max_values=1)
        view = discord.ui.View(timeout=None)
        view.add_item(self.select_cls(select))
        return view

    def speaker_view(self, meeting_id: str, label: str, options: Sequence[tuple[str, str]]) -> discord.ui.View:
        """Who an unidentified track was: one option per participant (DESIGN §4.1)."""
        opts = [discord.SelectOption(label=_clip(name), value=uid) for uid, name in options][:25]
        select = discord.ui.Select(custom_id=f"mscribe:ssel:{meeting_id}:{label}", options=opts, min_values=1,
                                   max_values=1)
        view = discord.ui.View(timeout=None)
        view.add_item(self.select_cls(select))
        return view

    def send_kwargs(self) -> dict[str, Any]:
        """A panel (components v2): it pings nobody."""
        return self.mention_kwargs(())

    def mention_kwargs(self, users: Sequence[str]) -> dict[str, Any]:
        """Ping exactly ``users`` (never @everyone/@here, roles or the replied user)."""
        return {"allowed_mentions": discord.AllowedMentions(
            users=[discord.Object(id=int(u)) for u in users], roles=False, everyone=False, replied_user=False)}

    def file(self, name: str, data: bytes) -> discord.File:
        """An attachment built from memory (the transcript, DESIGN §17.3)."""
        import io

        return discord.File(io.BytesIO(data), filename=name)

    def project_view(self, meeting_id: str, candidates: Sequence[Candidate]) -> discord.ui.View:
        # Select values are capped at 100 chars; real keys ("linear:<uuid>", "hermes:<slug>") fit, and a
        # truncated key could never be resolved again, so oversized ones are left out of the picker.
        lang = getattr(self.handler, "lang", "en")
        options = [discord.SelectOption(label=_clip(c.name or c.key), value=c.key,
                                        description=_clip(source_label(c.source, lang)) or None)
                   for c in candidates if len(c.key) <= 100][:25]
        select = discord.ui.Select(custom_id=f"mscribe:psel:{meeting_id}:all", options=options, min_values=1,
                                   max_values=1)
        view = discord.ui.View(timeout=None)
        view.add_item(self.select_cls(select))
        return view
