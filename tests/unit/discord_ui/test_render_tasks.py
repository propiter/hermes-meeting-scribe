"""Task-centred rendering (DESIGN §16): one message per task with ITS buttons under it, the compact
index for the meeting chat, and the paginated per-user panel (ephemeral / DM)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from meeting_scribe.discord_ui.render import RenderOptions
from meeting_scribe.discord_ui.render_tasks import (
    PANEL_TEXT_LIMIT,
    TASKS_PER_PAGE,
    TaskView,
    render_index,
    render_panel,
    render_task,
)
from meeting_scribe.discord_ui.routing import Route
from meeting_scribe.domain.models import ActionItem, ActionStatus

OPTS = RenderOptions(lang="en", kanban_on=True, linear_on=True, is_owner_item=lambda i: i.owner_speaker_id == "11")


def tv(i: int, owner: str | None = "11", **kw) -> TaskView:
    item = ActionItem(id=f"a{i:010d}", title=f"Task {i}", owner_speaker_id=owner,
                      owner_name={"11": "Luis", "10": "Ana"}.get(owner or ""), quote=f"quote {i}")
    route = kw.pop("route", Route("501", "orion", reason="fuzzy"))
    return TaskView(replace(item, **kw), route)


def ids(buttons) -> list[str]:
    return [b.custom_id for b in buttons]


def test_task_message_has_its_own_buttons_on_one_row(meeting):
    spec = render_task(meeting, tv(1), OPTS)
    assert "Task 1" in spec.content and "<@11>" in spec.content and "orion" in spec.content
    assert ids(spec.buttons) == ["mscribe:ok:k3v7q2ab:a0000000001", "mscribe:lin:k3v7q2ab:a0000000001",
                                 "mscribe:no:k3v7q2ab:a0000000001", "mscribe:prj:k3v7q2ab:a0000000001",
                                 "mscribe:tas:k3v7q2ab:a0000000001"]
    assert {b.row for b in spec.buttons} == {0}


def test_an_unassigned_task_offers_i_ll_take_it_and_a_dm_copy_offers_nothing(meeting):
    """DESIGN §16.2: 🙋 on a task without assignee, 👤 on one that has one, neither in a DM copy."""
    spec = render_task(meeting, tv(3, owner=None), OPTS)
    [take] = [b for b in spec.buttons if ":tak:" in b.custom_id]
    assert take.label == "I'll take it" and take.emoji == "🙋" and not any(":tas:" in c for c in ids(spec.buttons))
    es = replace(OPTS, lang="es")
    assert [b.label for b in render_task(meeting, tv(3, owner=None), es).buttons if ":tak:" in b.custom_id] == [
        "Me la quedo"]
    no_assign = replace(OPTS, can_assign=False)
    assert not any(":tak:" in c or ":tas:" in c for c in ids(render_task(meeting, tv(3, owner=None), no_assign).buttons))


def test_kanban_button_only_on_owner_tasks(meeting):
    spec = render_task(meeting, tv(2, owner="10"), OPTS)
    assert not any(":ok:" in c for c in ids(spec.buttons)) and any(":lin:" in c for c in ids(spec.buttons))


def test_finished_task_shows_result_and_loses_its_buttons(meeting):
    done = replace(tv(1, status=ActionStatus.DELIVERED), refs={"kanban": "t_42"})
    spec = render_task(meeting, done, OPTS)
    assert "✅ Kanban `t_42`" in spec.content and spec.buttons == ()
    lin = replace(tv(1, owner="10", status=ActionStatus.DELIVERED), refs={"linear": "ENG-7"})
    assert "🟣 Linear ENG-7" in render_task(meeting, lin, OPTS).content
    gone = render_task(meeting, tv(3, status=ActionStatus.DISMISSED), OPTS)
    assert "❌" in gone.content and "~~Task 3~~" in gone.content and gone.buttons == ()


def test_uncertain_project_is_flagged(meeting):
    spec = render_task(meeting, tv(1, route=Route("501", "orion", uncertain=True, reason="fuzzy")), OPTS)
    assert "⚠️" in spec.content and "📁" in spec.content


def test_index_counts_per_project_and_person_with_links_and_one_button(meeting):
    views = [tv(1), tv(2, owner="10"), tv(3, owner=None, route=Route("502", "nebula", uncertain=True)),
             tv(4, owner="10", route=Route(None)),
             tv(5, route=Route(None, "atlas", reason="no_permission", wanted_channel_id="503"))]
    spec = render_index(meeting, views, {"501": "7001", "502": "7002", None: "7000"}, ["10"], OPTS)
    text = spec.content
    assert "**orion** — 2" in text and "<#7001>" in text and "**nebula** — 1" in text and "<#7002>" in text
    assert "<@11> — 2" in text and "<@10> — 2" in text and "Unassigned — 1" in text
    assert "<#503>" in text  # missing permission is reported
    assert "<@10>" in text.split("✉️")[1]  # DM failure noted
    assert ids(spec.buttons) == ["mscribe:mine:k3v7q2ab:all"]


def test_panel_blocks_keep_task_then_buttons_even_after_an_approval(meeting):
    views = [tv(1), replace(tv(2, status=ActionStatus.DELIVERED), refs={"kanban": "t_9"}), tv(3)]
    panel = render_panel(meeting, views, user_id="11", scope="m", page=0, o=OPTS, is_owner=True)
    assert [b.item_id for b in panel.blocks] == ["a0000000001", "a0000000002", "a0000000003"]
    assert panel.blocks[1].buttons == () and "t_9" in panel.blocks[1].content
    for block in panel.blocks:
        assert all(c.endswith(block.item_id) for c in ids(block.buttons))


def test_panel_shows_only_the_clickers_tasks_and_paginates(meeting):
    views = [tv(i) for i in range(10)] + [tv(99, owner="10")]
    p0 = render_panel(meeting, views, user_id="11", scope="m", page=0, o=OPTS, is_owner=False)
    assert len(p0.blocks) == TASKS_PER_PAGE and "a0000000099" not in [b.item_id for b in p0.blocks]
    assert (p0.page, p0.pages) == (0, 3)
    assert ids(p0.nav) == ["mscribe:pg:k3v7q2ab:m1"]  # no "previous" on the first page, no "all" for non-owners
    p2 = render_panel(meeting, views, user_id="11", scope="m", page=7, o=OPTS, is_owner=False)
    assert p2.page == 2 and len(p2.blocks) == 2 and ids(p2.nav) == ["mscribe:pg:k3v7q2ab:m1"]


def test_panel_respects_discord_limits(meeting):
    long = [tv(i, title="T" * 400, quote="Q" * 400, description="D" * 400) for i in range(9)]
    for page in range(3):
        p = render_panel(meeting, long, user_id="11", scope="m", page=page, o=OPTS, is_owner=True)
        rows = len(p.blocks) + (1 if p.nav else 0)
        components = 1 + sum(2 + len(b.buttons) for b in p.blocks) + (1 + len(p.nav) if p.nav else 0)
        assert rows <= 5 and components <= 40
        assert len(p.header) + sum(len(b.content) for b in p.blocks) <= PANEL_TEXT_LIMIT
        assert all(len(c) < 100 for b in p.blocks for c in ids(b.buttons))


def test_owner_can_switch_to_all_tasks(meeting):
    views = [tv(1), tv(2, owner="10"), tv(3, owner=None)]
    mine = render_panel(meeting, views, user_id="11", scope="m", page=0, o=OPTS, is_owner=True)
    assert "mscribe:pg:k3v7q2ab:a0" in ids(mine.nav)
    every = render_panel(meeting, views, user_id="11", scope="a", page=0, o=OPTS, is_owner=True)
    assert len(every.blocks) == 3 and "mscribe:pg:k3v7q2ab:m0" in ids(every.nav)


def test_empty_panel_says_so(meeting):
    p = render_panel(meeting, [tv(1)], user_id="77", scope="m", page=0, o=OPTS, is_owner=False)
    assert p.blocks == () and "no tasks" in p.header.lower()


@pytest.mark.parametrize("lang, word", [("en", "Tasks"), ("es", "Tareas")])
def test_index_language(meeting, lang, word):
    assert word in render_index(meeting, [tv(1)], {}, [], replace(OPTS, lang=lang)).content


# -- private meetings (DESIGN §19.2) --------------------------------------------------------------------
def private(view: TaskView, **kw) -> TaskView:
    from meeting_scribe.discord_ui.render_tasks import Sharing

    return replace(view, sharing=Sharing(**{"target": "501", "target_name": "#orion", "can_dm": True, **kw}))


def test_private_task_keeps_its_buttons_and_adds_share_buttons_on_a_second_row(meeting):
    view = private(tv(1, quote="the secret plan"))
    spec = render_task(meeting, view, OPTS)
    cids = ids(spec.buttons)
    a1 = "k3v7q2ab:a0000000001"
    assert f"mscribe:ok:{a1}" in cids and f"mscribe:shd:{a1}" in cids and f"mscribe:shp:{a1}" in cids
    share = [b for b in spec.buttons if b.custom_id.split(":")[1] in ("shd", "shp")]
    assert {b.row for b in share} == {1} and "Publish in #orion" in [b.label for b in share]
    assert "Nothing has been shared yet" in spec.content


def test_private_task_state_is_reflected_and_done_buttons_disappear(meeting):
    spec = render_task(meeting, private(tv(1), dm=True, channel="501"), OPTS)
    assert "Sent to <@11>" in spec.content and "Published in <#501>" in spec.content
    assert not any(":shd:" in c or ":shp:" in c for c in ids(spec.buttons))
    none = render_task(meeting, private(tv(1, owner=None), target="", can_dm=False), OPTS)
    assert not any(":shd:" in c or ":shp:" in c for c in ids(none.buttons))


def test_shared_text_is_the_task_only(meeting):
    from meeting_scribe.discord_ui.render_tasks import render_shared_dm, render_shared_task

    view = private(tv(1, quote="the secret plan", description="Draft it", due="2026-10-02"))
    for spec in (render_shared_task(meeting, view, "en"), render_shared_dm(meeting, view, "es")):
        assert "Draft it" in spec.content and "2026-10-02" in spec.content and not spec.buttons
        assert "the secret plan" not in spec.content and meeting.title not in spec.content
        assert "discord.com" not in spec.content


def test_private_index_counts_shared_tasks_and_offers_share_all(meeting):
    views = [private(tv(1), dm=True), private(tv(2))]
    spec = render_index(meeting, views, {}, (), OPTS, private=True)
    assert "Private meeting" in spec.content and "Shared: 1 of 2" in spec.content
    assert "mscribe:sha:k3v7q2ab:all" in ids(spec.buttons)
    done = [private(tv(1), dm=True, channel="501")]
    assert "mscribe:sha:k3v7q2ab:all" not in ids(render_index(meeting, done, {}, (), OPTS, private=True).buttons)
    assert "Private" not in render_index(meeting, [tv(1)], {}, (), OPTS).content
