"""Button/select actions and their authorization (DESIGN §8)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.discord_ui.actions import ButtonActions
from meeting_scribe.domain.models import Candidate, SinkResult

from .fakes import FakeInteraction


class Svc:
    def __init__(self):
        self.calls = []
        self.fail = None

    def _rec(self, *a):
        self.calls.append(a)
        if self.fail:
            raise self.fail

    def approve_item(self, mid, iid, sink):
        self._rec("approve", mid, iid, sink)
        return "t_1"

    def approve_all(self, mid, sink):
        self._rec("all", mid, sink)
        return SinkResult(sink, True, ("t_1", "t_2"))

    def dismiss_item(self, mid, iid):
        self._rec("dismiss", mid, iid)

    def require(self, mid):
        return SimpleNamespace(id=mid, channel_id="200")

    def candidates(self, meeting):
        return [Candidate("hermes:p1", "Website", "hermes"), Candidate("kanban:ops", "Ops", "kanban")]

    def set_project(self, mid, project):
        self._rec("project", mid, project)
        return Candidate("hermes:p1", "Website", "hermes")


@pytest.fixture
def env():
    svc = Svc()
    refreshed = []
    allowed = {"50"}

    async def refresh(mid):
        refreshed.append(mid)

    acts = ButtonActions(service=lambda: svc, settings=lambda: settings_from_mapping({}), owners=lambda: ("11",),
                         check_auth=lambda i: str(i.user.id) in allowed, refresh=refresh,
                         project_view=lambda mid, cands: ("select", mid, tuple(c.key for c in cands)))
    return SimpleNamespace(svc=svc, acts=acts, refreshed=refreshed)


async def test_owner_approves_to_kanban_and_message_refreshes(env):
    i = FakeInteraction(11)
    await env.acts.handle(i, "ok", "k3v7q2ab", "a1")
    assert env.svc.calls == [("approve", "k3v7q2ab", "a1", "kanban")]
    assert env.refreshed == ["k3v7q2ab"] and i.response.deferred
    assert "t_1" in i.replies()


async def test_non_owner_cannot_approve_kanban_even_if_allowed(env):
    i = FakeInteraction(50)
    await env.acts.handle(i, "ok", "k3v7q2ab", "a1")
    await env.acts.handle(i, "allk", "k3v7q2ab", "all")
    assert env.svc.calls == [] and "owner" in i.replies().lower()


async def test_allowed_user_may_use_linear_and_dismiss(env):
    i = FakeInteraction(50)
    await env.acts.handle(i, "lin", "k3v7q2ab", "a1")
    await env.acts.handle(i, "no", "k3v7q2ab", "a2")
    await env.acts.handle(i, "alll", "k3v7q2ab", "all")
    assert [c[0] for c in env.svc.calls] == ["approve", "dismiss", "all"]
    assert env.svc.calls[0][3] == "linear" and env.svc.calls[2][2] == "linear"


async def test_strangers_are_rejected(env):
    i = FakeInteraction(99)
    for action in ("lin", "no", "prj", "psel"):
        await env.acts.handle(i, action, "k3v7q2ab", "a1", values=["hermes:p1"])
    assert env.svc.calls == [] and env.refreshed == []
    assert all(m.get("ephemeral") for m in i.response.sent)


async def test_project_button_offers_candidates_then_select_saves(env):
    i = FakeInteraction(11)
    await env.acts.handle(i, "prj", "k3v7q2ab", "all")
    assert i.response.deferred  # deferred before the (possibly slow) catalog lookup
    view = i.followup.sent[0]["view"]
    assert view == ("select", "k3v7q2ab", ("hermes:p1", "kanban:ops"))
    j = FakeInteraction(11, values=["hermes:p1"])
    await env.acts.handle(j, "psel", "k3v7q2ab", "all")
    assert env.svc.calls == [("project", "k3v7q2ab", "hermes:p1")]
    assert "Website" in j.replies() and env.refreshed == ["k3v7q2ab"]


async def test_service_errors_become_ephemeral_replies(env):
    env.svc.fail = ValueError("action item a1 was dismissed")
    i = FakeInteraction(11)
    await env.acts.handle(i, "ok", "k3v7q2ab", "a1")
    assert "dismissed" in i.replies()


async def test_approve_all_reports_errors(env):
    env.svc.approve_all = lambda mid, sink: SinkResult(sink, False, ("t_1",), errors=("a2: boom",))
    i = FakeInteraction(11)
    await env.acts.handle(i, "allk", "k3v7q2ab", "all")
    assert "a2: boom" in i.replies() and "1" in i.replies()
