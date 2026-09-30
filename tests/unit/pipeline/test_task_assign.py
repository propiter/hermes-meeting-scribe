"""Who a task belongs to, decided after the meeting (DESIGN §16.2): the rules shared by the card's
buttons, the agent's tools, the CLI and the Desktop; the audit, undo, and the Linear assignee."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from meeting_scribe import privacy
from meeting_scribe.domain.ids import idempotency_key
from meeting_scribe.domain.models import ActionItem, MeetingState, Notes, Speaker
from meeting_scribe.pipeline.service import MeetingService
from meeting_scribe.pipeline.task_assign import (ANNOUNCE_KV, Actor, TaskAssignError, find_card,
                                                 pending_announcements)
from meeting_scribe.storage.artifacts import read_notes

from .test_runner import build, drain

ANA, LUIS, OWNER, STRANGER = "10", "11", "900", "99"


class Analyzer:
    def analyze(self, meeting, utterances, candidates):
        return Notes(meeting_title="Semanal", tldr="t", summary="s", language="es",
                     action_items=(ActionItem(id="fix-mail", title="Arreglar el correo"),
                                   ActionItem(id="report", title="Enviar informe", owner_speaker_id=LUIS,
                                              owner_name="Luis")))


class FakeLinearSink:
    def __init__(self):
        self.calls = []
        self.result = "synced"

    def set_assignee(self, meeting, item):
        self.calls.append((item.id, item.owner_speaker_id))
        return self.result


@pytest.fixture
def world(prepo, layout, settings, clock, meeting):
    runner, *_ = build(prepo, layout, settings, clock)
    runner.stages.analyzer = Analyzer()
    linear = FakeLinearSink()
    service = MeetingService(prepo, layout, runner, settings, clock=clock, item_sinks=lambda: {"linear": linear},
                             catalogs=lambda: runner.stages.catalogs())
    live = service.begin_recording(replace(meeting, state=MeetingState.RECORDING, ended_at=None,
                                           speakers=(Speaker(ANA, "Ana"), Speaker(LUIS, "Luis"))))
    service.finish_recording(live.id)
    drain(runner)
    return service, live.id, linear


def test_a_participant_takes_an_unassigned_task(world):
    service, mid, _ = world
    done = service.assign_task(mid, "fix-mail", "me", Actor(ANA))
    assert done.changed and done.previous is None and done.user == ANA and done.name == "Ana"
    item = service.repo.get_action_item(mid, "fix-mail")
    assert item.owner_speaker_id == ANA and item.owner_name == "Ana"
    notes = read_notes(service.folder(service.require(mid)))
    assert next(a for a in notes.action_items if a.id == "fix-mail").owner_speaker_id == ANA
    [audit] = service.repo.task_history(mid, "fix-mail")
    assert (audit["actor"], audit["previous_user"], audit["next_user"]) == (ANA, None, ANA)
    assert pending_announcements(service.repo, mid) == {"fix-mail": {"to": ANA, "from": [], "actor": ANA,
                                                                     "ping": False, "seq": 1}}  # took it: nobody to ping


def test_assigning_twice_changes_nothing_the_second_time(world):
    service, mid, _ = world
    service.assign_task(mid, "fix-mail", "me", Actor(ANA))
    service.repo.kv_set(ANNOUNCE_KV + mid, None)  # Discord already showed it
    again = service.assign_task(mid, "fix-mail", f"<@{ANA}>", Actor(ANA))
    assert not again.changed and len(service.repo.task_history(mid)) == 1
    assert pending_announcements(service.repo, mid) == {}  # nothing to re-render, nobody re-mentioned


def test_a_non_participant_needs_to_be_authorized_and_see_the_task(world):
    service, mid, _ = world
    with pytest.raises(TaskAssignError) as exc:
        service.assign_task(mid, "fix-mail", "me", Actor(STRANGER))
    assert exc.value.code == "not_eligible"
    with pytest.raises(TaskAssignError):
        service.assign_task(mid, "fix-mail", "me", Actor(STRANGER, authorized=True))  # cannot see the card
    assert service.assign_task(mid, "fix-mail", "me", Actor(STRANGER, authorized=True, sees=True)).user == STRANGER


def test_a_participant_cannot_give_a_task_to_someone_else_nor_take_one_already_taken(world):
    service, mid, _ = world
    with pytest.raises(TaskAssignError) as other:
        service.assign_task(mid, "fix-mail", LUIS, Actor(ANA))
    with pytest.raises(TaskAssignError) as taken:
        service.assign_task(mid, "report", "me", Actor(ANA))
    with pytest.raises(TaskAssignError) as release:
        service.assign_task(mid, "report", "none", Actor(ANA))
    assert (other.value.code, taken.value.code, release.value.code) == ("self_only", "taken", "not_yours")
    assert service.repo.task_history(mid) == []


def test_the_assignee_releases_their_task_and_an_owner_reassigns_any(world):
    service, mid, _ = world
    freed = service.assign_task(mid, "report", "none", Actor(LUIS))
    assert freed.user is None and service.repo.get_action_item(mid, "report").owner_speaker_id is None
    boss = Actor(OWNER, admin=True)
    assert service.assign_task(mid, "report", "Ana", boss).user == ANA
    assert service.assign_task(mid, "report", "123456789012345678", boss).user == "123456789012345678"
    with pytest.raises(TaskAssignError) as unknown:
        service.assign_task(mid, "report", "Nadie Conocido", boss)
    assert unknown.value.code == "unknown_person"


def test_owners_undo_the_last_assignment_and_it_is_audited(world):
    service, mid, _ = world
    service.assign_task(mid, "fix-mail", "me", Actor(ANA))
    with pytest.raises(TaskAssignError) as exc:
        service.undo_task_assignment(mid, "fix-mail", Actor(ANA))
    assert exc.value.code == "owner_required"
    back = service.undo_task_assignment(mid, "fix-mail", Actor(OWNER, admin=True))
    assert back.user is None and service.repo.get_action_item(mid, "fix-mail").owner_speaker_id is None
    history = service.repo.task_history(mid, "fix-mail")
    assert [(h["actor"], h["previous_user"], h["next_user"], h["undone"]) for h in history] == [
        (ANA, None, ANA, 1), (OWNER, ANA, None, 0)]
    with pytest.raises(TaskAssignError) as never:
        service.undo_task_assignment(mid, "report", Actor(OWNER, admin=True))
    assert never.value.code == "nothing_to_undo"


def test_the_assignment_survives_a_new_analysis(world):
    service, mid, _ = world
    service.assign_task(mid, "fix-mail", "me", Actor(ANA))
    runner = service.runner
    from meeting_scribe.domain.models import Stage

    service.reprocess(mid, Stage.ANALYZE)
    drain(runner)
    assert service.repo.get_action_item(mid, "fix-mail").owner_speaker_id == ANA
    notes = read_notes(service.folder(service.require(mid)))  # the files say the same (notes.md, Obsidian)
    assert next(a for a in notes.action_items if a.id == "fix-mail").owner_speaker_id == ANA


def test_a_private_meeting_needs_its_channel_and_a_dm_meeting_refuses_chat(world):
    service, mid, _ = world
    privacy.remember(service.repo, mid, "rule", "4242")
    with pytest.raises(TaskAssignError) as exc:
        service.assign_task(mid, "fix-mail", "me", Actor(ANA))
    assert exc.value.code == "private_only"
    with pytest.raises(TaskAssignError):
        service.assign_task(mid, "fix-mail", ANA, Actor(OWNER, admin=True))  # owners too: only from inside
    assert service.assign_task(mid, "fix-mail", "me", Actor(ANA, sees=True)).user == ANA
    service.repo.kv_set(privacy.KV_PRIVATE + mid, json.dumps({"rule": "r", "mode": "dm", "recipients": [ANA]}))
    with pytest.raises(TaskAssignError) as dm:
        service.assign_task(mid, "report", ANA, Actor(OWNER, admin=True, sees=True))
    assert dm.value.code == "dm_meeting"
    assert service.assign_task(mid, "report", ANA, Actor("cli", local=True)).user == ANA


def test_without_identity_nothing_is_assigned(world):
    service, mid, _ = world
    for actor in (Actor(""), Actor("cli"), Actor("", admin=True)):
        with pytest.raises(TaskAssignError) as exc:
            service.assign_task(mid, "fix-mail", "me", actor)
        assert exc.value.code == "no_identity"


def test_a_task_already_in_linear_gets_its_new_assignee(world):
    service, mid, linear = world
    service.repo.record_delivery(mid, "linear", idempotency_key(mid, "fix-mail"), external_id="iss-1", url=None)
    done = service.assign_task(mid, "fix-mail", "me", Actor(ANA))
    assert linear.calls == [("fix-mail", ANA)] and done.sinks == {"linear": "synced"}
    linear.result = "unmapped"
    assert service.assign_task(mid, "fix-mail", LUIS, Actor(OWNER, admin=True)).sinks == {"linear": "unmapped"}
    assert service.assign_task(mid, "report", "none", Actor(LUIS)).sinks == {}  # never sent: nothing to sync


def test_tasks_resolve_by_id_prefix_and_card_message(world):
    service, mid, _ = world
    meeting = service.require(mid)
    ptr = {"channel": 555, "message": 123456789012345678, "target": ""}
    service.repo.upsert_delivery(mid, "discord", f"mtg:{mid}:task:fix-mail", external_id=json.dumps(ptr), url="")
    assert service.resolve_task(meeting, "fix-mail").id == "fix-mail"
    assert service.resolve_task(meeting, "rep").id == "report"
    assert service.resolve_task(meeting, "123456789012345678").id == "fix-mail"
    card = find_card(service.repo, "123456789012345678")
    assert (card.meeting_id, card.item_id, card.channel, card.dm_of) == (mid, "fix-mail", "555", "")
    with pytest.raises(TaskAssignError):
        service.resolve_task(meeting, "999")
