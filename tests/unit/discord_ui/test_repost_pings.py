"""Only a task's FIRST publication notifies its assignee (DESIGN §19.4).

A placement switch, a re-delivery, a 📁 move or a message deleted by hand re-post the task: the text
still shows the mention, but ``allowed_mentions`` names nobody. Invented names only.
"""
from __future__ import annotations

import pytest

from .test_task_sink import deliver, env, thread_of  # noqa: F401 - fixture reuse


def _pings(msg):
    return msg.sent_kwargs["allowed_mentions"]["users"]


def _task(env, title):
    return next(m for c in env.bot.channels.values() for m in c.ordered() if title in m.content)


async def test_the_first_publication_pings_the_assignee(env):  # noqa: F811
    await deliver(env)
    _, thread = thread_of(env, env.orion)
    [task] = thread.ordered()
    assert "<@11>" in task.content and _pings(task) == ("11",)


@pytest.mark.parametrize("first,second", [("projects", "meeting"), ("meeting", "projects"),
                                          ("meeting", "projects_inline"), ("projects_inline", "projects")])
async def test_a_placement_switch_reposts_without_pinging_again(env, first, second):  # noqa: F811
    env.cfg["delivery_tasks_placement"] = first
    await deliver(env)
    before = _task(env, "Landing page")
    env.cfg["delivery_tasks_placement"] = second
    await deliver(env, env.make())
    task = _task(env, "Landing page")
    assert task.id != before.id and "<@11>" in task.content and _pings(task) == ()


async def test_the_folder_button_moves_without_pinging_again(env):  # noqa: F811
    env.cfg["delivery_tasks_placement"] = "meeting"
    sink = await deliver(env)
    await sink.move_item(env.meeting.id, "a1", "502", viewer="11")
    task = _task(env, "Landing page")
    assert task.channel is env.nebula or task.channel.parent is env.nebula
    assert _pings(task) == ()


async def test_a_task_message_deleted_by_hand_comes_back_without_pinging(env):  # noqa: F811
    await deliver(env)
    task = _task(env, "Landing page")
    await task.delete()
    await deliver(env, env.make())
    again = _task(env, "Landing page")
    assert again.id != task.id and _pings(again) == ()


async def test_a_new_task_of_a_reprocess_still_pings_its_assignee(env):  # noqa: F811
    from dataclasses import replace

    from meeting_scribe.domain.models import ActionItem
    from meeting_scribe.storage.artifacts import write_notes

    await deliver(env)
    extra = ActionItem(id="a4", title="Write the release notes", owner_speaker_id="11", owner_name="Luis",
                       project="orion", project_key="discord:501", project_confidence=0.9)
    env.notes = replace(env.notes, action_items=(*env.notes.action_items, extra))
    write_notes(env.svc.folder(env.meeting), env.meeting, env.notes, "es")
    env.svc.repo.sync_action_items(env.meeting.id, env.notes.action_items)
    await deliver(env, env.make())
    assert _pings(_task(env, "release notes")) == ("11",)
