"""Regression tests for review findings 1 (per-sink approval) and 6 (claim-first delivery)."""
from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.domain.ids import idempotency_key
from meeting_scribe.domain.models import ActionItem, MeetingState, Notes, Speaker
from meeting_scribe.sinks.base import DeliveryInProgress
from meeting_scribe.sinks.kanban import KanbanSink
from meeting_scribe.sinks.linear import LinearSink


class FakeLinear:
    def __init__(self, delay: float = 0.0, existing: dict | None = None):
        self.created: list[dict] = []
        self.searched: list[str] = []
        self.delay = delay
        self.existing = existing
        self._lock = threading.Lock()

    def teams(self):
        return [{"id": "team1", "key": "ENG", "name": "Eng"}]

    def users(self):
        return []

    def projects(self):
        return []

    def find_issue_by_marker(self, marker):
        self.searched.append(marker)
        if self.existing and marker in self.existing.get("description", ""):
            return {"id": self.existing["id"], "url": self.existing["url"]}
        return None

    def create_issue(self, issue):
        time.sleep(self.delay)
        with self._lock:
            self.created.append(issue)
            return {"id": f"iss{len(self.created)}", "url": "u"}


class FakeKanban:
    def __init__(self):
        self.created: list[dict] = []

    def create_task(self, **kw):
        self.created.append(kw)
        return f"t_{len(self.created)}"

    def list_boards(self):
        return []


def _setup(repo, meeting, **cfg):
    m = replace(meeting, speakers=(Speaker("11", "Luis"),), state=MeetingState.ANALYZED)
    repo.save_meeting(m)
    item = ActionItem(id="a1", title="Enviar informe", owner_speaker_id="11")
    notes = Notes("t", "t", "s", action_items=(item,))
    repo.sync_action_items(m.id, notes.action_items)
    s = settings_from_mapping({"linear_default_team": "ENG", "owners": ["11"], **cfg})
    return m, notes, (lambda: s)


def test_kanban_auto_does_not_make_linear_approve_auto(tmp_path, repo, meeting):
    """Finding 1: Kanban delivering an item used to count as 'approved' for Linear."""
    m, notes, s = _setup(repo, meeting, **{"kanban_mode": "auto", "linear_mode": "approve"})
    lb = FakeLinear()
    kanban = KanbanSink(s, repo, FakeKanban(), owners=lambda: ("11",), project_for=lambda *a: None)
    linear = LinearSink(s, repo, lambda: lb, project_for=lambda *a: None)
    for sink in (kanban, linear):  # Runtime.sinks() order
        sink.deliver(m, notes, tmp_path)
    assert lb.created == []


def test_approving_for_kanban_is_not_approving_for_linear(tmp_path, repo, meeting):
    m, notes, s = _setup(repo, meeting, **{"kanban_mode": "approve", "linear_mode": "approve"})
    lb = FakeLinear()
    kanban = KanbanSink(s, repo, FakeKanban(), owners=lambda: ("11",), project_for=lambda *a: None)
    linear = LinearSink(s, repo, lambda: lb, project_for=lambda *a: None)
    repo.set_item_sink_status(m.id, "a1", "kanban", "approved")
    kanban.deliver_item(m, notes, notes.action_items[0], tmp_path)
    linear.deliver(m, notes, tmp_path)  # e.g. the pipeline's deliver stage on retry/reprocess
    assert lb.created == []
    repo.set_item_sink_status(m.id, "a1", "linear", "approved")
    linear.deliver(m, notes, tmp_path)
    assert len(lb.created) == 1


def test_concurrent_approvals_create_one_linear_issue(tmp_path, repo, meeting):
    """Finding 6: double click / Approve-all overlapping a click created two issues."""
    m, notes, s = _setup(repo, meeting, **{"linear_mode": "approve"})
    lb = FakeLinear(delay=0.2)
    sink = LinearSink(s, repo, lambda: lb, project_for=lambda *a: None)
    results: list[object] = []

    def approve() -> None:
        try:
            results.append(sink.deliver_item(m, notes, notes.action_items[0], tmp_path))
        except DeliveryInProgress as exc:
            results.append(exc)

    threads = [threading.Thread(target=approve) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(lb.created) == 1
    assert sorted(type(r).__name__ for r in results) == ["DeliveryInProgress", "str"]
    assert sink.deliver_item(m, notes, notes.action_items[0], tmp_path) == "iss1"


def test_crash_between_create_and_record_reconciles_by_marker(tmp_path, repo, meeting):
    """Finding 6: a claim left pending by a crash is taken over, and Linear is searched first."""
    m, notes, s = _setup(repo, meeting, **{"linear_mode": "auto"})
    key = idempotency_key(m.id, "a1")
    claim = repo.claim_delivery(m.id, "linear", key)  # the crashed attempt
    assert claim.kind == "new"
    repo.release_delivery("linear", key, claim.token)  # its process died (claim abandoned)
    lb = FakeLinear(existing={"id": "iss_prev", "url": "u_prev", "description": f"body\n\n`{key}`"})
    sink = LinearSink(s, repo, lambda: lb, project_for=lambda *a: None)
    assert sink.deliver_item(m, notes, notes.action_items[0], tmp_path) == "iss_prev"
    assert lb.created == [] and lb.searched == [f"`{key}`"]
    assert repo.get_delivery("linear", key)["external_id"] == "iss_prev"


def test_failed_create_keeps_the_claim_for_reconciliation(tmp_path, repo, meeting):
    m, notes, s = _setup(repo, meeting, **{"linear_mode": "auto"})

    class Timeout(FakeLinear):
        def create_issue(self, issue):
            super().create_issue(issue)  # the server created it...
            raise TimeoutError("read timed out")  # ...but we never saw the answer

    lb = Timeout(existing=None)
    sink = LinearSink(s, repo, lambda: lb, project_for=lambda *a: None)
    with pytest.raises(TimeoutError):
        sink.deliver_item(m, notes, notes.action_items[0], tmp_path)
    key = idempotency_key(m.id, "a1")
    assert repo.get_delivery("linear", key) is None
    lb.existing = {"id": "iss1", "url": "u", "description": f"`{key}`"}
    lb.create_issue = FakeLinear.create_issue.__get__(lb)
    assert sink.deliver_item(m, notes, notes.action_items[0], tmp_path) == "iss1"
    assert len(lb.created) == 1  # the retry found the issue instead of creating a second


def test_linear_graphql_finds_issue_by_marker():
    import json

    from meeting_scribe.sinks.linear import LinearGraphQL

    seen = []

    def transport(url, headers, body):
        req = json.loads(body)
        seen.append(req)
        return {"data": {"issues": {"nodes": [
            {"id": "x", "identifier": "ENG-1", "url": "u1", "description": "other"},
            {"id": "y", "identifier": "ENG-2", "url": "u2", "description": "a\n\n`mtg:m:a1`"}]}}}

    hit = LinearGraphQL(lambda: "k", transport=transport).find_issue_by_marker("`mtg:m:a1`")
    assert hit == {"id": "y", "identifier": "ENG-2", "url": "u2"}
    assert seen[0]["variables"]["filter"] == {"description": {"contains": "`mtg:m:a1`"}}
