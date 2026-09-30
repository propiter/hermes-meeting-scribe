"""Task changes the agent proposes in a SHARED Discord conversation (DESIGN §16.3).

A thread is one Hermes session for everyone by default: a message from Luis that arrives while Ana's turn
runs is handled inside Ana's turn, with Ana's session identity. So there the write tools never act on the
turn's identity: they post the change with ✅ Confirm / ✖ Cancel, and the change is made by whoever presses
✅ — as that person (Discord's interaction proves who), with the task card's rules."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from meeting_scribe import privacy
from meeting_scribe.commands import Caller
from meeting_scribe.config import settings_from_mapping
from meeting_scribe.discord_ui.actions import ButtonActions
from meeting_scribe.pipeline import task_proposals
from meeting_scribe.storage.repo import Repository

from .test_task_tools import ANA, LUIS, OWNER, STRANGER, THREAD, call, discord, world  # noqa: F401  (fixture)


class Poster:
    def __init__(self):
        self.posts = []

    def __call__(self, chat, text, meeting_id, pid, lang):
        self.posts.append((chat, text, meeting_id, pid))
        return "5001"


class Sink:
    def __init__(self):
        self.place = None
        self.dm = None
        self.calls = []

    async def private_place(self, mid):
        return self.place

    async def dm_recipients(self, mid):
        return self.dm

    async def announce(self, mid):
        self.calls.append(("announce", mid))
        return 1

    async def refresh_item(self, mid, iid):
        self.calls.append(("refresh", mid, iid))


class Message:
    def __init__(self, content):
        self.content = content
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)
        self.content = kw.get("content", self.content)


class Click:
    """A click on the proposal message: who pressed, in which chat, and what Discord says they can see."""

    def __init__(self, user, *, chat=THREAD, name="", sees=True, message=None):
        self.user = SimpleNamespace(id=int(user), roles=[], display_name=name or f"user{user}")
        self.channel_id = int(chat)
        self.channel = SimpleNamespace(id=int(chat), parent_id=7000)
        self.guild = SimpleNamespace(id=100)
        self.permissions = SimpleNamespace(view_channel=sees)
        self.message = message or Message("proposal")
        self.data = {}
        self.sent, self.followups = [], []
        outer = self

        class Response:
            async def send_message(self, content="", **kw):
                outer.sent.append(content)

            async def defer(self, **kw):
                return None

        class Followup:
            async def send(self, content="", **kw):
                outer.followups.append(content)

        self.response, self.followup = Response(), Followup()

    def replies(self):
        return "\n".join(self.sent + self.followups)


@pytest.fixture
def shared(world):
    service, mid, sinks, state, tools = world
    poster, sink = Poster(), Sink()

    def make():
        base = tools()
        base._proposer = lambda: poster
        return base

    acts = ButtonActions(service=lambda: service, settings=lambda space=None: settings_from_mapping({}),
                         owners=lambda space=None: (OWNER,), check_auth=lambda i: str(i.user.id) != STRANGER,
                         sink=lambda: sink, project_view=lambda *a: None, move_view=lambda *a: None)
    return SimpleNamespace(service=service, mid=mid, sinks=sinks, state=state, tools=make, poster=poster, sink=sink,
                           acts=acts)


def press(env, click, action, pid):
    asyncio.run(env.acts.handle(click, action, env.mid, pid))


def test_the_session_is_the_user_s_alone_only_with_their_slot_in_the_key():
    """How the plugin tells (hermes-agent ``gateway/session.py`` ``build_session_key``): a thread shared by
    everyone has no user slot; per-user sessions end with the user id; a Discord DM is one person's."""
    thread = Caller(platform="discord", chat_id="7001", user_id="10", chat_type="thread",
                    session_key="agent:main:discord:thread:7001:7001")
    assert not thread.per_user_session
    assert Caller(platform="discord", chat_id="7001", user_id="10", chat_type="thread",
                  session_key="agent:main:discord:thread:7001:7001:10").per_user_session
    assert Caller(platform="discord", chat_id="7000", user_id="10", chat_type="group",
                  session_key="agent:main:discord:group:7000:10").per_user_session
    assert not Caller(platform="discord", chat_id="7000", user_id="10", chat_type="group",
                      session_key="agent:main:discord:group:7000").per_user_session
    assert Caller(platform="discord", chat_id="6001", user_id="10", chat_type="dm",
                  session_key="agent:main:discord:dm:6001").per_user_session
    assert not Caller(platform="discord", chat_id="7001", user_id="10", chat_type="thread").per_user_session  # unknown


def test_in_a_shared_thread_the_tools_propose_and_change_nothing(shared):
    shared.state["caller"] = discord(ANA, shared=True)
    out = call(shared.tools, "task_assign", meeting_id=shared.mid, task_id="fix-mail", assignee=f"<@{LUIS}>")
    assert out["status"] == "pending_confirmation" and "Confirm" in out["message"]
    sent = call(shared.tools, "task_send", meeting_id=shared.mid, task_id="report", target="linear")
    assert sent["status"] == "pending_confirmation"
    assert shared.service.repo.task_history(shared.mid) == [] and shared.sinks["linear"].sent == []
    (chat, text, mid, pid), (_, send_text, _, _) = shared.poster.posts
    assert chat == THREAD and mid == shared.mid
    assert "Arreglar el correo" in text and "Luis" in text and "<@" not in text  # nobody is notified
    assert "Linear" in send_text
    row = shared.service.repo.get_task_proposal(pid)
    assert (row["state"], row["kind"], row["arg"], row["session_user"]) == ("pending", "assign", f"<@{LUIS}>", ANA)


def test_confirm_acts_as_whoever_presses_it_with_the_card_s_rules(shared):
    """The turn's identity says Ana, an owner in the review's case; whoever presses decides what may happen."""
    shared.state["caller"] = discord(OWNER, shared=True)  # the session still says: the owner
    call(shared.tools, "task_assign", meeting_id=shared.mid, task_id="fix-mail", assignee="<@55>")
    pid = shared.poster.posts[-1][3]
    member = Click(ANA, name="Ana")
    press(shared, member, "pok", pid)
    assert "only take a task for yourself" in member.replies()
    assert shared.service.repo.task_history(shared.mid) == []  # refused: nothing changed ...
    assert shared.service.repo.get_task_proposal(pid)["state"] == "pending"  # ... and an owner may still confirm
    boss = Click(OWNER, name="Boss")
    press(shared, boss, "pok", pid)
    [audit] = shared.service.repo.task_history(shared.mid)
    assert (audit["actor"], audit["next_user"]) == (OWNER, "55")
    row = shared.service.repo.get_task_proposal(pid)
    assert (row["state"], row["decided_by"]) == ("done", OWNER) and row["result"]
    assert "Confirmed by Boss" in boss.message.content and boss.message.edits[-1]["view"] is None
    again = Click(OWNER)
    press(shared, again, "pok", pid)
    assert "already confirmed or cancelled" in again.replies() and len(shared.service.repo.task_history(shared.mid)) == 1


def test_me_is_whoever_confirms(shared):
    shared.state["caller"] = discord(ANA, shared=True)
    call(shared.tools, "task_assign", meeting_id=shared.mid, task_id="fix-mail", assignee="me")
    assert "whoever presses Confirm" in shared.poster.posts[-1][1]
    press(shared, Click(LUIS), "pok", shared.poster.posts[-1][3])
    [audit] = shared.service.repo.task_history(shared.mid)
    assert (audit["actor"], audit["next_user"]) == (LUIS, LUIS)
    assert ("announce", shared.mid) in shared.sink.calls


def test_send_is_the_linear_button_pressed_by_the_confirmer(shared):
    shared.state["caller"] = discord(LUIS, shared=True)
    call(shared.tools, "task_send", meeting_id=shared.mid, task_id="report", target="linear")
    pid = shared.poster.posts[-1][3]
    stranger = Click(ANA)
    press(shared, stranger, "pok", pid)
    assert "belongs to" in stranger.replies() and shared.sinks["linear"].sent == []  # the card's own (ephemeral) words
    press(shared, Click(LUIS), "pok", pid)
    assert shared.sinks["linear"].sent == [("report", LUIS)] and ("refresh", shared.mid, "report") in shared.sink.calls


def test_a_proposal_expires_is_used_once_and_only_in_its_chat(shared, monkeypatch):
    shared.state["caller"] = discord(ANA, shared=True)
    call(shared.tools, "task_assign", meeting_id=shared.mid, task_id="fix-mail", assignee="me")
    pid = shared.poster.posts[-1][3]
    elsewhere = Click(ANA, chat="7999")
    press(shared, elsewhere, "pok", pid)
    assert "not valid here" in elsewhere.replies()
    later = time.time() + task_proposals.TTL_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    late = Click(ANA)
    press(shared, late, "pok", pid)
    assert "expired" in late.replies() and shared.service.repo.task_history(shared.mid) == []
    assert shared.service.repo.get_task_proposal(pid)["state"] == "expired"


def test_cancel_is_audited_and_ends_it(shared):
    shared.state["caller"] = discord(ANA, shared=True)
    call(shared.tools, "task_assign", meeting_id=shared.mid, task_id="fix-mail", assignee="me")
    pid = shared.poster.posts[-1][3]
    stranger = Click(STRANGER)  # Hermes does not let them use the bot
    press(shared, stranger, "pno", pid)
    assert shared.service.repo.get_task_proposal(pid)["state"] == "pending"
    luis = Click(LUIS, name="Luis")
    press(shared, luis, "pno", pid)
    row = shared.service.repo.get_task_proposal(pid)
    assert (row["state"], row["decided_by"]) == ("cancelled", LUIS) and "Cancelled by Luis" in luis.message.content
    press(shared, Click(ANA), "pok", pid)
    assert shared.service.repo.task_history(shared.mid) == []


def test_a_proposal_survives_a_restart(shared, tmp_path):
    shared.state["caller"] = discord(ANA, shared=True)
    call(shared.tools, "task_assign", meeting_id=shared.mid, task_id="fix-mail", assignee="me")
    pid = shared.poster.posts[-1][3]
    reopened = Repository(shared.service.layout.db_path())
    try:
        assert task_proposals.get(reopened, pid).state == "pending"
    finally:
        reopened.close()


def test_a_private_meeting_s_proposal_needs_its_channel_and_a_dm_meeting_gets_none(shared):
    privacy.remember(shared.service.repo, shared.mid, "rule", "7000")
    shared.sink.place = {"7000", THREAD}
    shared.state["caller"] = discord(ANA, chat="7999", parent="", shared=True)
    out = call(shared.tools, "task_assign", meeting_id=shared.mid, task_id="fix-mail", assignee="me")
    assert out["code"] == "unknown_meeting" and shared.poster.posts == []  # nothing posted outside its channel
    shared.state["caller"] = discord(ANA, shared=True)
    call(shared.tools, "task_assign", meeting_id=shared.mid, task_id="fix-mail", assignee="me")
    pid = shared.poster.posts[-1][3]
    blind = Click(LUIS, sees=False)
    press(shared, blind, "pok", pid)
    assert shared.service.repo.task_history(shared.mid) == []
    import json

    shared.service.repo.kv_set(privacy.KV_PRIVATE + shared.mid,
                               json.dumps({"rule": "r", "mode": "dm", "recipients": [ANA]}))
    out = call(shared.tools, "task_assign", meeting_id=shared.mid, task_id="fix-mail", assignee="me")
    assert "error" in out and len(shared.poster.posts) == 1


def test_a_subagent_proposes_and_cron_or_no_discord_cannot_even_do_that(shared):
    from dataclasses import replace

    shared.state["caller"] = replace(discord(ANA), delegated=True)  # its own session says Ana: not proof
    assert call(shared.tools, "task_assign", meeting_id=shared.mid, task_id="fix-mail",
                assignee="me")["status"] == "pending_confirmation"
    shared.state["caller"] = replace(discord(ANA, shared=True), cron=True)
    assert call(shared.tools, "task_assign", meeting_id=shared.mid, task_id="fix-mail",
                assignee="me")["code"] == "no_identity"
    assert len(shared.poster.posts) == 1


def test_without_discord_connected_the_agent_is_told_to_use_the_card(shared):
    shared.state["caller"] = discord(ANA, shared=True)
    tools = shared.tools()
    tools._proposer = lambda: None
    import json

    out = json.loads(tools.task_assign({"meeting_id": shared.mid, "task_id": "fix-mail", "assignee": "me"}))
    assert out["code"] == "confirm_unavailable" and "card" in out["error"]
