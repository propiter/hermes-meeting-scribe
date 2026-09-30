"""The agent's task tools (DESIGN §16.3): they act AS the Discord user of the turn, with the task card's
rules, and refuse when nobody is known. The case that motivated them, anonymized: in the thread of a
published meeting a member replies to the card of the unassigned task "Arreglar el correo" mentioning
the bot — "esta tarea es mía, asígnamela, créala en Linear"."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from meeting_scribe import privacy
from meeting_scribe.commands import Caller
from meeting_scribe.domain.ids import idempotency_key
from meeting_scribe.domain.models import ActionItem, MeetingState, Notes, Speaker
from meeting_scribe.pipeline.service import MeetingService
from meeting_scribe.tools import SCHEMAS, MeetingTools

from .test_runner import build, drain

ANA, LUIS, OWNER, STRANGER = "10", "11", "900", "99"
THREAD, CARD, ELSEWHERE = "7001", "8001", "7999"


class Analyzer:
    def analyze(self, meeting, utterances, candidates):
        return Notes(meeting_title="Semanal", tldr="t", summary="s", language="es",
                     action_items=(ActionItem(id="fix-mail", title="Arreglar el correo"),
                                   ActionItem(id="report", title="Enviar informe", owner_speaker_id=LUIS,
                                              owner_name="Luis")))


class FakeSink:
    def __init__(self, name):
        self.name = name
        self.sent = []

    def enabled(self, meeting):
        return True

    def deliver_item(self, meeting, notes, item, folder):
        self.sent.append((item.id, item.owner_speaker_id))
        return "ENG-7" if self.name == "linear" else "t_42"

    def set_assignee(self, meeting, item):
        return "synced"


@pytest.fixture
def world(prepo, layout, settings, clock, meeting):
    runner, *_ = build(prepo, layout, settings, clock)
    runner.stages.analyzer = Analyzer()
    sinks = {"linear": FakeSink("linear"), "kanban": FakeSink("kanban")}
    service = MeetingService(prepo, layout, runner, settings, clock=clock, item_sinks=lambda: sinks,
                             catalogs=lambda: runner.stages.catalogs())
    live = service.begin_recording(replace(meeting, state=MeetingState.RECORDING, ended_at=None,
                                           speakers=(Speaker(ANA, "Ana"), Speaker(LUIS, "Luis"))))
    service.finish_recording(live.id)
    drain(runner)
    ptr = {"channel": THREAD, "message": CARD, "target": ""}
    prepo.upsert_delivery(live.id, "discord", f"mtg:{live.id}:task:fix-mail", external_id=json.dumps(ptr), url="")
    prepo.upsert_delivery(live.id, "discord", f"mtg:{live.id}:notes",
                          external_id=json.dumps({"channel": "7000", "thread": THREAD}), url="")
    state = {"caller": None}

    def tools():
        return MeetingTools(lambda: service, guild=lambda: "", caller=lambda: state["caller"],
                            reader=lambda: state["caller"].reader if state["caller"] else privacy.Reader(),
                            owners=lambda space: (OWNER,))
    return service, live.id, sinks, state, tools


def discord(user, chat=THREAD, parent="7000"):
    return Caller(platform="discord", chat_id=chat, user_id=user, parent_chat_id=parent, scope_id="")


def call(tools, name, **args):
    return json.loads(getattr(tools(), name)(args))


def test_schemas_are_strict_and_name_the_new_tools():
    assert set(SCHEMAS) == {"meeting_search", "meeting_get", "meeting_task_list", "meeting_task_assign",
                            "meeting_task_send"}
    for schema in SCHEMAS.values():
        params = schema["parameters"]
        assert params["type"] == "object" and params["additionalProperties"] is False
        assert set(params["required"]) <= set(params["properties"])
    assert SCHEMAS["meeting_task_send"]["parameters"]["properties"]["target"]["enum"] == ["linear", "kanban"]


def test_the_reply_to_the_card_takes_the_task_and_sends_it_to_linear(world):
    service, mid, sinks, state, tools = world
    state["caller"] = discord(ANA)
    took = call(tools, "task_assign", message_id=CARD, assignee="me")
    assert took["ok"] and took["task_id"] == "fix-mail" and took["assignee"] == ANA
    assert took["message"] == "“Arreglar el correo” is yours now."  # in the space's language
    sent = call(tools, "task_send", message_id=CARD, target="linear")
    assert sent["ok"] and sent["ref"] == "ENG-7" and sinks["linear"].sent == [("fix-mail", ANA)]
    [audit] = service.repo.task_history(mid, "fix-mail")
    assert (audit["actor"], audit["next_user"]) == (ANA, ANA)


def test_assigning_twice_through_the_tool_changes_nothing(world):
    service, mid, _, state, tools = world
    state["caller"] = discord(ANA)
    call(tools, "task_assign", meeting_id=mid, task_id="fix-mail", assignee="me")
    again = call(tools, "task_assign", meeting_id=mid, task_id="fix-mail", assignee=f"<@{ANA}>")
    assert again["ok"] and again["changed"] is False and len(service.repo.task_history(mid)) == 1


def test_without_a_discord_user_the_tools_only_read(world):
    service, mid, _, state, tools = world
    for caller in (None, Caller(platform="", chat_id="", user_id="", source="cli"),
                   Caller(platform="discord", chat_id=THREAD, user_id=ANA, cron=True),
                   Caller(platform="telegram", chat_id="5", user_id=ANA)):
        state["caller"] = caller
        out = call(tools, "task_assign", meeting_id=mid, task_id="fix-mail", assignee="me")
        assert out["code"] == "no_identity", caller
        assert call(tools, "task_send", meeting_id=mid, task_id="fix-mail", target="linear")["code"] == "no_identity"
    assert service.repo.task_history(mid) == []
    state["caller"] = None
    listed = call(tools, "task_list", meeting_id=mid)
    assert [t["id"] for t in listed["tasks"]] == ["fix-mail", "report"]


def test_saying_i_am_an_admin_changes_nothing(world):
    service, mid, sinks, state, tools = world
    state["caller"] = discord(ANA)
    out = call(tools, "task_assign", meeting_id=mid, task_id="report", assignee="me")
    assert out["code"] == "taken" and "<@11>" in out["error"]
    out = call(tools, "task_assign", meeting_id=mid, task_id="fix-mail", assignee=LUIS)
    assert out["code"] == "self_only"
    out = call(tools, "task_send", meeting_id=mid, task_id="report", target="linear")
    assert "error" in out and sinks["linear"].sent == []  # Luis's task: only he or an owner sends it


def test_an_owner_assigns_anyone_and_a_non_participant_needs_to_be_in_the_meeting_s_thread(world):
    service, mid, _, state, tools = world
    state["caller"] = discord(STRANGER, chat=ELSEWHERE, parent="")
    assert call(tools, "task_assign", meeting_id=mid, task_id="fix-mail", assignee="me")["code"] == "not_eligible"
    state["caller"] = discord(STRANGER)  # chatting in the meeting's thread: sees the card
    assert call(tools, "task_assign", meeting_id=mid, task_id="fix-mail", assignee="me")["assignee"] == STRANGER
    state["caller"] = discord(OWNER, chat=ELSEWHERE, parent="")
    out = call(tools, "task_assign", meeting_id=mid, task_id="fix-mail", assignee=f"<@{LUIS}>")
    assert out["assignee"] == LUIS and "Linear" not in out["message"]


def test_a_private_meeting_s_tasks_only_move_from_its_channel(world):
    service, mid, sinks, state, tools = world
    privacy.remember(service.repo, mid, "rule", "7000")
    state["caller"] = discord(ANA, chat=ELSEWHERE, parent="")
    out = call(tools, "task_assign", meeting_id=mid, task_id="fix-mail", assignee="me")
    assert "no meeting" in out["error"]  # outside its channel the meeting does not exist for the agent
    assert "no meeting" in call(tools, "task_send", message_id=CARD, target="linear")["error"]
    state["caller"] = discord(ANA)
    assert call(tools, "task_assign", message_id=CARD, assignee="me")["assignee"] == ANA
    sent = call(tools, "task_send", message_id=CARD, target="linear")
    assert sent["ok"] and sinks["linear"].sent == [("fix-mail", ANA)]


def test_a_direct_messages_meeting_is_not_reassigned_from_chat(world):
    service, mid, _, state, tools = world
    service.repo.kv_set(privacy.KV_PRIVATE + mid, json.dumps({"rule": "r", "mode": "dm", "recipients": [ANA]}))
    service.repo.upsert_delivery(mid, "discord", f"mtg:{mid}:pdm:{ANA}:notes",
                                 external_id=json.dumps({"channel": "6010", "messages": [1]}), url="")
    state["caller"] = discord(OWNER, chat="6010", parent="")
    out = call(tools, "task_assign", meeting_id=mid, task_id="fix-mail", assignee=ANA)
    assert "error" in out and service.repo.task_history(mid) == []


def test_kanban_keeps_its_owners_only_rule_and_unknown_tasks_are_explained(world):
    service, mid, sinks, state, tools = world
    state["caller"] = discord(LUIS)
    out = call(tools, "task_send", meeting_id=mid, task_id="report", target="kanban")
    assert "owner" in out["error"].lower() and sinks["kanban"].sent == []
    out = call(tools, "task_assign", meeting_id=mid, task_id="nope", assignee="me")
    assert out["code"] == "unknown_task" and "meeting_task_list" in out["error"]
    assert "target must be" in call(tools, "task_send", meeting_id=mid, task_id="report", target="jira")["error"]


def test_the_list_says_where_each_task_already_is(world):
    service, mid, _, state, tools = world
    service.repo.record_delivery(mid, "linear", idempotency_key(mid, "report"), external_id="iss", url=None)
    state["caller"] = discord(ANA)
    tasks = {t["id"]: t for t in call(tools, "task_list", message_id=CARD)["tasks"]}
    assert tasks["report"]["sent_to"] == ["linear"] and tasks["fix-mail"]["assignee"] is None
