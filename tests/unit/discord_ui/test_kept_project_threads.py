"""A project thread / forum post where people wrote is never deleted by a re-delivery (DESIGN §16.1).

Switching the placement, losing a route or a reprocess moves the tasks out of a project channel. A
thread holding only the bot's messages is deleted with its anchor; one with people's replies stays,
its anchor saying where the tasks are now. Invented names only.
"""
from __future__ import annotations

from types import SimpleNamespace

from .test_task_sink import deliver, env, thread_of  # noqa: F401 - fixture reuse


async def _person_says(thread, text: str):
    msg = await thread.send(text)
    msg.author = SimpleNamespace(id=77, bot=False)  # a member, not the bot
    return msg


def _anchor_of(env, channel):
    return channel.ordered()[0]


async def test_switch_to_meeting_keeps_a_project_thread_where_people_replied(env):  # noqa: F811
    await deliver(env)  # projects
    _, orion_thread = thread_of(env, env.orion)
    human = await _person_says(orion_thread, "I already started the landing, see the draft")
    env.cfg["delivery_tasks_placement"] = "meeting"
    await deliver(env, env.make())
    assert orion_thread.id in env.bot.channels and human.id in orion_thread.messages
    assert not [m for m in orion_thread.ordered() if "Landing page" in m.content]  # the task moved
    anchor = _anchor_of(env, env.orion)
    assert "now with the notes" in anchor.content and "https://discord.com/" in anchor.content
    assert any("Landing page" in m.content for m in env.chat.ordered())


async def test_losing_a_route_keeps_the_project_thread_with_replies(env):  # noqa: F811
    await deliver(env)
    _, nebula_thread = thread_of(env, env.nebula)
    await _person_says(nebula_thread, "contract comments inside")
    env.nebula.can_post = False  # a permission change / outage between deliveries
    await deliver(env, env.make())
    assert nebula_thread.id in env.bot.channels


async def test_a_thread_with_only_the_bots_messages_is_still_deleted(env):  # noqa: F811
    await deliver(env)
    _, orion_thread = thread_of(env, env.orion)
    env.cfg["delivery_tasks_placement"] = "meeting"
    await deliver(env, env.make())
    assert orion_thread.id not in env.bot.channels and env.orion.ordered() == []


async def test_a_kept_thread_is_used_again_when_the_tasks_come_back(env):  # noqa: F811
    await deliver(env)
    _, orion_thread = thread_of(env, env.orion)
    await _person_says(orion_thread, "question about the landing")
    env.cfg["delivery_tasks_placement"] = "meeting"
    await deliver(env, env.make())
    await deliver(env, env.make())  # the retired anchor is edited once, not on every delivery
    edits = _anchor_of(env, env.orion).edits
    env.cfg["delivery_tasks_placement"] = "projects"
    await deliver(env, env.make())
    assert [c for c in env.bot.channels.values() if c.parent is env.orion] == [orion_thread]
    assert any("Landing page" in m.content for m in orion_thread.ordered())
    anchor = _anchor_of(env, env.orion)
    assert "now with the notes" not in anchor.content and anchor.edits == edits + 1


async def test_a_forum_post_with_replies_is_kept(env):  # noqa: F811
    del env.bot.channels[501]
    forum = env.bot.add_forum(501, "orion", category_id=900)
    await deliver(env)
    [post] = forum.posts
    await _person_says(post, "reply inside the post")
    env.cfg["delivery_tasks_placement"] = "meeting"
    await deliver(env, env.make())
    assert post.id in env.bot.channels
    assert "now with the notes" in post.ordered()[0].content
