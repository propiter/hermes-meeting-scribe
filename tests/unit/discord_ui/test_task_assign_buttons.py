"""Who a task belongs to, from Discord (DESIGN §16.2): the card's 🙋/👤 buttons, what the click proves
about the clicker, and how an assignment is shown — the card edited in place, at most ONE mention of a
new assignee someone else chose, their DM panel posted once and edited afterwards, the index counts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.discord_ui.actions import ButtonActions
from meeting_scribe.pipeline.task_assign import Actor, TaskAssignError, TaskAssigned, assign, pending_announcements

from .fakes import FakeInteraction
from .test_actions import OWNER, Sink  # OWNER is Luis (11) there
from .test_task_sink import Svc, deliver, env  # noqa: F401  (the task sink's world, reused)

ANA, LUIS, STRANGER = 10, 11, 99


class AssignSvc(Svc):
    """The task sink's service plus what the assignment service needs."""

    clock = SimpleNamespace(now=lambda: datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc))

    def require(self, mid, space=None):
        meeting = self.repo.get_meeting(mid)
        if meeting is None:
            raise KeyError(mid)
        return meeting

    def settings(self, space=None):
        return settings_from_mapping({})

    def item_sinks(self):
        return {}


@pytest.fixture
def world(env):  # noqa: F811
    svc = AssignSvc.__new__(AssignSvc)
    svc.__dict__.update(env.svc.__dict__)
    env.svc = svc
    return env


def messages(env):
    return {c.id: len(c.messages) for c in env.bot.channels.values()}


async def test_taking_a_task_edits_its_card_in_place_and_pings_nobody(world):
    sink = await deliver(world)
    before = messages(world)
    card = next(m for m in world.chat.ordered() if "Budget" in m.content)
    edits = card.edits
    await asyncio.to_thread(assign, world.svc, world.meeting.id, "a3", "me", Actor(str(LUIS)))
    assert await sink.announce(world.meeting.id) == 1
    assert "<@11>" in card.content and card.edits == edits + 1 and "mscribe:tas:k3v7q2ab:a3" in card.view
    after = messages(world)
    assert after == before  # nothing re-posted, no mention message: Luis took it himself
    [panel] = world.bot.users[LUIS].dm.ordered()  # his existing DM panel was edited, not duplicated
    assert [b[0] for b in panel.view[2]] == ["a1", "a3"]
    assert "<@11> — 2" in world.chat.ordered()[-1].content  # the index counts
    assert pending_announcements(world.svc.repo, world.meeting.id) == {}
    again = await asyncio.to_thread(assign, world.svc, world.meeting.id, "a3", "me", Actor(str(LUIS)))
    assert not again.changed and await sink.announce(world.meeting.id) == 0


async def test_an_owner_s_assignment_mentions_the_new_assignee_once(world):
    sink = await deliver(world)
    boss = Actor("900", admin=True)  # an owner who is not Luis
    await asyncio.to_thread(assign, world.svc, world.meeting.id, "a3", str(LUIS), boss)
    await sink.announce(world.meeting.id)
    pings = [m for m in world.chat.ordered() if m.content.startswith("👤")]
    assert len(pings) == 1 and pings[0].content == "👤 <@11>: “Budget” is yours now."
    assert pings[0].sent_kwargs["allowed_mentions"] == {"users": ("11",), "roles": False, "everyone": False}
    await asyncio.to_thread(assign, world.svc, world.meeting.id, "a3", "none", boss)
    await asyncio.to_thread(assign, world.svc, world.meeting.id, "a3", str(LUIS), boss)
    await sink.announce(world.meeting.id)
    assert len([m for m in world.chat.ordered() if m.content.startswith("👤")]) == 1  # never twice for a task


async def test_the_new_assignee_gets_the_dm_panel_once_and_the_old_one_loses_it(world):
    world.bot.user(ANA)  # Ana's DMs open for this test
    sink = await deliver(world)
    boss = Actor("900", admin=True)
    await asyncio.to_thread(assign, world.svc, world.meeting.id, "a1", str(ANA), boss)
    await sink.announce(world.meeting.id)
    [ana] = world.bot.users[ANA].dm.ordered()
    assert [b[0] for b in ana.view[2]] == ["a1", "a2"]
    [luis] = world.bot.users[LUIS].dm.ordered()
    assert [b[0] for b in luis.view[2]] == []  # emptied in place
    await asyncio.to_thread(assign, world.svc, world.meeting.id, "a3", str(ANA), boss)
    await sink.announce(world.meeting.id)
    assert len(world.bot.users[ANA].dm.ordered()) == 1  # edited, never a second panel


async def test_nothing_is_posted_for_a_meeting_never_published(world):
    await asyncio.to_thread(assign, world.svc, world.meeting.id, "a3", "me", Actor(str(LUIS)))
    world.state["loop"] = asyncio.get_running_loop()
    sink = world.make()
    assert await sink.announce(world.meeting.id) == 1
    assert all(not c.messages for c in world.bot.channels.values())


# -- the buttons ---------------------------------------------------------------------------------
class ButtonSvc:
    def __init__(self):
        self.calls = []
        self.item = SimpleNamespace(id="a3", title="Budget", owner_speaker_id=None, owner_name=None)
        self.repo = SimpleNamespace(get_action_item=lambda mid, iid: self.item,
                                    task_history=lambda mid, iid: [{"undone": 0}], list_links=lambda space: [],
                                    get_meeting=lambda mid: None)
        self.meeting = SimpleNamespace(id="k3v7q2ab", space="main", human_speakers=(
            SimpleNamespace(user_id="10", name="Ana", google_user=""),))

    def require(self, mid):
        return self.meeting

    def assign_task(self, mid, iid, who, actor):
        self.calls.append(("assign", who, actor))
        if who == "boom":
            raise TaskAssignError("self_only")
        user = actor.user_id if who == "me" else None if who == "none" else who
        return TaskAssigned(mid, iid, "Budget", None, user, "Ana", True, 1, {"linear": "unmapped"})

    def undo_task_assignment(self, mid, iid, actor):
        self.calls.append(("undo", actor))
        return TaskAssigned(mid, iid, "Budget", "10", None, "", True, 2)


class AnnounceSink(Sink):
    async def announce(self, mid):
        self.calls.append(("announce", mid))
        return 1


@pytest.fixture
def buttons():
    svc, sink = ButtonSvc(), AnnounceSink()
    acts = ButtonActions(service=lambda: svc, settings=lambda space=None: settings_from_mapping({}),
                         owners=lambda space=None: (str(OWNER),), check_auth=lambda i: i.user.id != STRANGER,
                         sink=lambda: sink, project_view=lambda *a: None, move_view=lambda *a: None,
                         buttons_view=lambda specs: list(specs),
                         assign_view=lambda mid, iid, opts, extra: ("assign", tuple(opts), tuple(extra)))
    return SimpleNamespace(svc=svc, sink=sink, acts=acts)


def seeing(i, sees=True):
    i.permissions = SimpleNamespace(view_channel=sees)
    return i


async def test_i_ll_take_it_acts_as_the_clicker_whatever_the_values(buttons):
    i = seeing(FakeInteraction(ANA, values=["11"]))
    await buttons.acts.handle(i, "tak", "k3v7q2ab", "a3")
    [(_, who, actor)] = buttons.svc.calls
    assert who == "me" and actor == Actor("10", admin=False, authorized=True, sees=True)
    assert "“Budget” is yours now." in i.replies() and "Linear: that person has no Linear user linked" in i.replies()
    assert buttons.sink.calls == [("announce", "k3v7q2ab")]


async def test_what_a_click_proves_is_what_the_service_gets(buttons):
    stranger = seeing(FakeInteraction(STRANGER), sees=False)
    await buttons.acts.handle(stranger, "tak", "k3v7q2ab", "a3")
    assert buttons.svc.calls[-1][2] == Actor("99", admin=False, authorized=False, sees=False)
    owner = seeing(FakeInteraction(OWNER, values=["10"]))
    await buttons.acts.handle(owner, "asel", "k3v7q2ab", "a3")
    assert buttons.svc.calls[-1][1] == "10" and buttons.svc.calls[-1][2].admin is True


async def test_a_refusal_is_explained_and_nothing_is_announced(buttons):
    i = seeing(FakeInteraction(ANA, values=["boom"]))
    await buttons.acts.handle(i, "asel", "k3v7q2ab", "a3")
    assert "You can only take a task for yourself" in i.replies() and buttons.sink.calls == []


async def test_the_assign_button_offers_owners_the_pickers_and_undo(buttons):
    i = seeing(FakeInteraction(OWNER))
    await buttons.acts.handle(i, "tas", "k3v7q2ab", "a3")
    kind, options, extra = i.followup.sent[0]["view"]
    assert kind == "assign" and options == (("none", "Unassigned"), ("10", "Ana"))
    assert [b.custom_id for b in extra] == ["mscribe:tun:k3v7q2ab:a3"]
    undo = seeing(FakeInteraction(OWNER))
    await buttons.acts.handle(undo, "tun", "k3v7q2ab", "a3")
    assert buttons.svc.calls[-1][0] == "undo" and "has no assignee now" in undo.replies()


async def test_the_assign_button_lets_the_assignee_release_and_tells_others_whose_it_is(buttons):
    buttons.svc.item.owner_speaker_id = "10"
    ana = seeing(FakeInteraction(ANA))
    await buttons.acts.handle(ana, "tas", "k3v7q2ab", "a3")
    assert [b.custom_id for b in ana.followup.sent[0]["view"]] == ["mscribe:trl:k3v7q2ab:a3"]
    other = seeing(FakeInteraction(STRANGER))
    await buttons.acts.handle(other, "tas", "k3v7q2ab", "a3")
    assert "already belongs to <@10>" in other.replies() and "view" not in other.followup.sent[0]
    release = seeing(FakeInteraction(ANA, values=["11"]))
    await buttons.acts.handle(release, "trl", "k3v7q2ab", "a3")
    assert buttons.svc.calls[-1][1] == "none"


async def test_a_private_meeting_s_assign_buttons_work_only_inside_its_channel(buttons):
    buttons.sink.place = {"4242"}
    i = seeing(FakeInteraction(ANA))
    i.channel = SimpleNamespace(id=777)
    await buttons.acts.handle(i, "tak", "k3v7q2ab", "a3")
    assert buttons.svc.calls == [] and "This meeting is private" in i.replies()


async def test_the_agent_finds_the_card_a_message_replies_to():
    """DESIGN §16.3: the tools read which message the user's Discord message replies to, on the gateway loop."""
    from meeting_scribe import discord_ui

    reply = SimpleNamespace(reference=SimpleNamespace(message_id=8001))
    plain = SimpleNamespace(reference=None)
    channel = SimpleNamespace(fetch_message=lambda mid: _done({9001: reply, 9002: plain}[mid]))
    client = SimpleNamespace(get_channel=lambda cid: channel if cid == 7001 else None)
    runtime = _Runtime()
    discord_ui._STATES[runtime] = SimpleNamespace(adapter=SimpleNamespace(_client=client), loop=asyncio.get_running_loop())
    lookup = discord_ui.replied_to_for(runtime)
    assert await asyncio.to_thread(lookup, "7001", "9001") == "8001"
    assert await asyncio.to_thread(lookup, "7001", "9002") is None
    assert lookup("7001", "9001") is None  # never blocks the gateway loop on itself
    assert discord_ui.replied_to_for(_Runtime()) is None  # Discord not connected


class _Runtime:
    """Weak-referenceable, like the real runtime."""


async def _done(value):
    return value
