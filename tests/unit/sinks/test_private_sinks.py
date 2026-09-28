"""Kanban / Linear never deliver a private meeting by themselves (DESIGN §19.2). Invented names only."""
from __future__ import annotations

from dataclasses import replace

from meeting_scribe import privacy
from meeting_scribe.domain.models import Candidate
from meeting_scribe.sinks.kanban import KanbanSink
from meeting_scribe.sinks.linear import LinearSink

from .test_kanban import FakeKanban
from .test_linear import FakeTransport, gql

RULE = {"meeting_routes": ["Daily Sync = #leadership-notes:private"]}


def test_kanban_auto_waits_for_a_button_on_a_private_meeting(tmp_path, repo, meeting, notes, settings_of):
    gw = FakeKanban()
    repo.sync_action_items(meeting.id, notes.action_items)
    sink = KanbanSink(settings_of(kanban__mode="auto", **RULE), repo, gw, owners=lambda space=None: ("11",),
                      project_for=lambda m, n, i: None)
    res = sink.deliver(meeting, notes, tmp_path)
    assert gw.created == [] and res.ok and res.skipped
    # a member pressed "Kanban" in the private channel: the task only, never the quote or the folder
    sink.deliver_item(meeting, notes, notes.action_items[0], tmp_path)
    [created] = gw.created
    assert created["title"] == "Enviar credenciales"
    assert "Yo envío las credenciales" not in created["body"] and str(tmp_path) not in created["body"]
    assert notes.meeting_title not in created["body"] and "reunión privada" in created["body"]


def test_kanban_sticky_private_record_counts_without_a_rule(tmp_path, repo, meeting, notes, settings_of):
    gw = FakeKanban()
    repo.sync_action_items(meeting.id, notes.action_items)
    privacy.remember(repo, meeting.id, "Daily Sync", "700")
    KanbanSink(settings_of(kanban__mode="auto"), repo, gw, owners=lambda space=None: ("11",),
               project_for=lambda m, n, i: None).deliver(meeting, notes, tmp_path)
    assert gw.created == []


def test_kanban_normal_rule_changes_nothing(tmp_path, repo, meeting, notes, settings_of):
    gw = FakeKanban()
    repo.sync_action_items(meeting.id, notes.action_items)
    KanbanSink(settings_of(kanban__mode="auto", meeting_routes=["Daily Sync = #team-notes"]), repo, gw,
               owners=lambda space=None: ("11",), project_for=lambda m, n, i: None).deliver(meeting, notes, tmp_path)
    assert len(gw.created) == 1 and "Yo envío las credenciales" in gw.created[0]["body"]


def test_linear_auto_waits_and_the_issue_carries_only_the_task(tmp_path, repo, meeting, notes, settings_of):
    t = FakeTransport()
    infra = Candidate("linear:prj_9", "Infra", "linear", {"project_id": "prj_9", "team_ids": ["team_2"]})
    repo.sync_action_items(meeting.id, notes.action_items)
    sink = LinearSink(settings_of(linear__mode="auto", **RULE), repo, lambda: gql(t), project_for=lambda m, n, i: infra)
    sink.deliver(meeting, notes, tmp_path)
    assert not [r for r in t.requests if "issueCreate" in r[2]["query"]]
    sink.deliver_item(meeting, notes, notes.action_items[0], tmp_path)
    [issue] = [r[2]["variables"]["input"] for r in t.requests if "issueCreate" in r[2]["query"]]
    assert "Yo envío las credenciales" not in issue["description"]
    assert notes.meeting_title not in issue["description"] and issue["assigneeId"] == "u_luis"


def test_other_meetings_are_not_affected(tmp_path, repo, meeting, notes, settings_of):
    gw = FakeKanban()
    other = replace(meeting, id="z9z9z9z9", channel_id="201", channel_name="Design")
    repo.save_meeting(other)
    repo.sync_action_items(other.id, notes.action_items)
    KanbanSink(settings_of(kanban__mode="auto", **RULE), repo, gw, owners=lambda space=None: ("11",),
               project_for=lambda m, n, i: None).deliver(other, notes, tmp_path)
    assert len(gw.created) == 1
