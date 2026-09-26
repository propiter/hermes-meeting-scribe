"""Persistent DynamicItem buttons/select built with the real discord.py (DESIGN §8)."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

discord = pytest.importorskip("discord")

from meeting_scribe.discord_ui.render import ButtonSpec, TEMPLATE  # noqa: E402
from meeting_scribe.discord_ui.views import ViewKit  # noqa: E402
from meeting_scribe.domain.models import Candidate  # noqa: E402

from .fakes import FakeInteraction  # noqa: E402


class Recorder:
    def __init__(self):
        self.calls = []

    async def handle(self, interaction, action, meeting_id, item_id, values=None):
        self.calls.append((action, meeting_id, item_id, values))


def specs():
    return [ButtonSpec("Kanban", "mscribe:ok:k3v7q2ab:a1", "success", 0, "✅"),
            ButtonSpec("Dismiss", "mscribe:no:k3v7q2ab:a1", "secondary", 0, "❌"),
            ButtonSpec("Linear", "mscribe:lin:k3v7q2ab:a2", "primary", 1)]


async def test_view_contains_dynamic_items_with_rows_and_styles():
    kit = ViewKit(Recorder())
    view = kit.view(specs())
    assert isinstance(view, discord.ui.View) and view.timeout is None
    items = view.children
    assert [i.custom_id for i in items] == [s.custom_id for s in specs()]
    assert all(isinstance(i, kit.button_cls) for i in items)
    assert [i.item.style for i in items] == [discord.ButtonStyle.success, discord.ButtonStyle.secondary,
                                              discord.ButtonStyle.primary]
    assert [i.row for i in items] == [0, 0, 1]
    assert view.is_persistent()


async def test_no_buttons_means_no_view():
    assert ViewKit(Recorder()).view([]) is None


async def test_from_custom_id_and_callback_route_to_actions():
    rec = Recorder()
    kit = ViewKit(rec)
    cid = "mscribe:ok:k3v7q2ab:a1"
    base = discord.ui.Button(custom_id=cid)
    m = re.match(TEMPLATE, cid)
    item = await kit.button_cls.from_custom_id(FakeInteraction(1), base, m)
    assert item.custom_id == cid
    await item.callback(FakeInteraction(1))
    assert rec.calls == [("ok", "k3v7q2ab", "a1", None)]


async def test_project_select_uses_candidates_and_routes_values():
    rec = Recorder()
    kit = ViewKit(rec)
    view = kit.project_view("k3v7q2ab", [Candidate("hermes:p1", "Website", "hermes"),
                                         Candidate("linear:x" * 20, "L" * 150, "linear")])
    sel = view.children[0]
    assert sel.custom_id == "mscribe:psel:k3v7q2ab:all"
    opts = sel.item.options
    assert opts[0].value == "hermes:p1" and opts[0].label == "Website"
    assert all(len(o.label) <= 100 and len(o.value) <= 100 for o in opts)
    base = discord.ui.Select(custom_id=sel.custom_id, options=[discord.SelectOption(label="x")])
    item = await kit.select_cls.from_custom_id(FakeInteraction(1), base, re.match(TEMPLATE, sel.custom_id))
    await item.callback(FakeInteraction(1, values=["hermes:p1"]))
    assert rec.calls == [("psel", "k3v7q2ab", "all", ["hermes:p1"])]


def test_each_kit_has_its_own_classes_and_register_adds_them():
    a, b = ViewKit(Recorder()), ViewKit(Recorder())
    assert a.button_cls is not b.button_cls
    bot = SimpleNamespace(added=[], add_dynamic_items=lambda *c: bot.added.extend(c))
    a.register(bot)
    assert bot.added == [a.button_cls, a.select_cls]
    assert issubclass(a.button_cls, discord.ui.DynamicItem)
