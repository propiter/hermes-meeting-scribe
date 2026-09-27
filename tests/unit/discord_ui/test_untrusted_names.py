"""Names chosen by Meet guests are untrusted Discord text (review finding 12)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from meeting_scribe.discord_ui.auth import check_task
from meeting_scribe.discord_ui.render import RenderOptions
from meeting_scribe.discord_ui.render_tasks import TaskView, render_index, render_task, safe_name
from meeting_scribe.discord_ui.routing import Route
from meeting_scribe.domain.models import ActionItem

OPTS = RenderOptions(lang="en", kanban_on=True, linear_on=True, is_owner_item=lambda i: False)
EVIL = "<@123> **x** @everyone"


def gmeet_view(name=EVIL, owner="gmeet:p1"):
    item = ActionItem(id="a0000000001", title="Task", owner_speaker_id=owner, owner_name=name, quote="q")
    return TaskView(item, Route("501", "orion", reason="fuzzy"))


def assert_inert(text):
    assert "<@123>" not in text and "@everyone" not in text and "**x**" not in text


@pytest.mark.parametrize("name", ["<@123>", "**x**", "<@&5>", "@here", "<@!7>", "`c`", "__u__", "||s||"])
def test_safe_name_neutralises_mentions_and_markdown(name):
    out = safe_name(name)
    assert out != name
    assert "<@" not in out.replace("<@\u200b", "") and "@here" not in out


def test_safe_name_keeps_normal_names():
    assert safe_name("María José") == "María José"


def test_task_message_escapes_a_meet_display_name(meeting):
    assert_inert(render_task(meeting, gmeet_view(), OPTS).content)


def test_discord_owner_name_suffix_is_escaped_too(meeting):
    content = render_task(meeting, gmeet_view(owner="11"), OPTS).content
    assert content.replace("<@\u200b", "").count("<@") == 1 and "<@11>" in content
    assert_inert(content.replace("<@11>", ""))


def test_index_person_line_escapes_a_meet_display_name(meeting):
    assert_inert(render_index(meeting, [gmeet_view()], {}, [], OPTS).content)


def test_belongs_to_message_escapes_a_meet_display_name():
    v = check_task("no", gmeet_view().item, "999", frozenset({"1"}), "en")
    assert not v.allowed
    assert_inert(v.message)
