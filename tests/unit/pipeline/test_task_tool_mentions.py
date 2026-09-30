"""What the agent's task tools hand back to the model, which answers in the (often public) chat through
Hermes' Discord adapter, where user mentions notify. The card already carries the ONE mention of an
assignment (DESIGN §16.2); a tool result never adds another: people are named, never mentioned."""
from __future__ import annotations

import re

from .test_task_tools import ANA, LUIS, OWNER, call, discord, world  # noqa: F401  (fixture)

MENTION = re.compile(r"<@!?&?\d+>")


def test_a_refused_take_names_the_assignee_without_mentioning_them(world):
    """Any member asks "asígnamela" on Luis's task: relayed by the agent, the refusal must not ping Luis
    on every attempt."""
    service, mid, _, state, tools = world
    state["caller"] = discord(ANA)
    out = call(tools, "task_assign", meeting_id=mid, task_id="report", assignee="me")
    assert out["code"] == "taken" and "Luis" in out["error"]
    assert not MENTION.search(out["error"]), out["error"]


def test_an_owner_s_assignment_reply_carries_no_second_mention(world):
    service, mid, _, state, tools = world
    state["caller"] = discord(OWNER, chat="7999", parent="")
    out = call(tools, "task_assign", meeting_id=mid, task_id="fix-mail", assignee=f"<@{LUIS}>")
    assert out["ok"] and "Luis" in out["message"]
    assert not MENTION.search(out["message"]), out["message"]


def test_no_task_tool_result_carries_a_live_mention(world):
    """Every string of every result of the three tools, successes and refusals, including someone the
    meeting does not know by name (shown as an inert ``@id``)."""
    service, mid, _, state, tools = world
    outs = []
    state["caller"] = discord(ANA)
    outs.append(call(tools, "task_assign", meeting_id=mid, task_id="report", assignee="none"))  # not_yours
    outs.append(call(tools, "task_send", meeting_id=mid, task_id="report", target="linear"))  # belongs to
    outs.append(call(tools, "task_list", meeting_id=mid))
    state["caller"] = discord(OWNER, chat="7999", parent="")
    outs.append(call(tools, "task_assign", meeting_id=mid, task_id="fix-mail", assignee="<@100000000000000055>"))
    outs.append(call(tools, "task_list", meeting_id=mid))
    text = repr(outs)
    assert not MENTION.search(text), text
    assert "@\u200b100000000000000055" in text or "@\\u200b100000000000000055" in text
