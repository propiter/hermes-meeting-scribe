"""Persistent DynamicItem buttons/select built with the real discord.py (DESIGN §8)."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

discord = pytest.importorskip("discord")

from meeting_scribe.discord_ui.render import ButtonSpec, TEMPLATE  # noqa: E402
from meeting_scribe.discord_ui.render_tasks import PanelBlock, TaskPanel  # noqa: E402
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
    assert opts[0].description == "Hermes project"  # never the internal source key
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
    assert bot.added == [a.button_cls, a.select_cls, a.user_select_cls]
    assert issubclass(a.button_cls, discord.ui.DynamicItem)


def _panel(n: int, nav: int = 2) -> TaskPanel:
    blocks = tuple(PanelBlock(f"a{i}", f"**Task {i}** · <@10>", (
        ButtonSpec("Linear", f"mscribe:lin:k3v7q2ab:a{i}", "primary", 0, "🟣"),
        ButtonSpec("Dismiss", f"mscribe:no:k3v7q2ab:a{i}", "secondary", 0, "❌"))) for i in range(n))
    navs = tuple(ButtonSpec(f"n{j}", f"mscribe:pg:k3v7q2ab:m{j}", "secondary", 0) for j in range(nav))
    return TaskPanel("### 📋 My tasks", blocks, navs, 0, 2)


async def test_panel_view_puts_each_tasks_buttons_directly_under_it():
    kit = ViewKit(Recorder())
    view = kit.panel_view(_panel(4))
    assert isinstance(view, discord.ui.LayoutView) and view.timeout is None and view.is_persistent()
    kinds = [type(c).__name__ for c in view.children]
    assert kinds == ["TextDisplay"] + ["TextDisplay", "ActionRow"] * 4 + ["ActionRow"]
    rows = [c for c in view.children if isinstance(c, discord.ui.ActionRow)]
    for i, row in enumerate(rows[:4]):
        assert all(ch.custom_id.endswith(f":a{i}") for ch in row.children)
        assert all(isinstance(ch, kit.button_cls) for ch in row.children)
    assert view.children[1].content.startswith("**Task 0**")


async def test_panel_view_fits_discord_component_and_text_limits():
    view = ViewKit(Recorder()).panel_view(_panel(5, nav=3))
    assert view.total_children_count <= 40 and view.content_length() <= 4000


async def test_move_view_is_a_select_of_channels_routed_as_tsel():
    rec = Recorder()
    kit = ViewKit(rec)
    view = kit.move_view("k3v7q2ab", "a2", [("502", "#nebula"), ("501", "#orion")])
    sel = view.children[0]
    assert sel.custom_id == "mscribe:tsel:k3v7q2ab:a2" and [o.value for o in sel.item.options] == ["502", "501"]
    base = discord.ui.Select(custom_id=sel.custom_id, options=[discord.SelectOption(label="x")])
    item = await kit.select_cls.from_custom_id(FakeInteraction(1), base, re.match(TEMPLATE, sel.custom_id))
    await item.callback(FakeInteraction(1, values=["502"]))
    assert rec.calls == [("tsel", "k3v7q2ab", "a2", ["502"])]


async def test_my_tasks_and_page_buttons_are_persistent_dynamic_items():
    kit = ViewKit(Recorder())
    view = kit.view([ButtonSpec("My tasks", "mscribe:mine:k3v7q2ab:all", "primary", 0, "📋")])
    assert isinstance(view.children[0], kit.button_cls) and view.is_persistent()
