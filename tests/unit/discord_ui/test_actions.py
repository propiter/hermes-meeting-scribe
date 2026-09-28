"""Button/select actions and their PER-TASK authorization (DESIGN §16).

Who may act on a task: its assignee and the owners. Anyone else gets an ephemeral "This task
belongs to @X" and nothing happens. Kanban only on the owners' own tasks. Unassigned: owners only.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.discord_ui.actions import ButtonActions
from meeting_scribe.domain.models import ActionItem, Candidate, SinkResult

from .fakes import FakeInteraction

OWNER, ANA, STRANGER, ALLOWED = 11, 10, 99, 50
ITEMS = {"a1": ActionItem(id="a1", title="Owner task", owner_speaker_id="11"),
         "a2": ActionItem(id="a2", title="Ana task", owner_speaker_id="10"),
         "a3": ActionItem(id="a3", title="Nobody's task")}


class Repo:
    def get_action_item(self, mid, iid):
        return ITEMS.get(iid)


class Svc:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.repo = Repo()

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
        return [Candidate("hermes:p1", "Website", "hermes")]

    def set_project(self, mid, project):
        self._rec("project", mid, project)
        return Candidate("hermes:p1", "Website", "hermes")


class Sink:
    def __init__(self):
        self.calls = []
        self.place = None  # a set of channel ids: the meeting is private
        self.dm = None  # {user id: DM channel id}: the meeting went by direct message only

    async def private_place(self, mid):
        return self.place

    async def dm_recipients(self, mid):
        return self.dm

    async def share(self, mid, iid, how):
        self.calls.append(("share", mid, iid, how))
        return "dm" if how == "dm" else "501"

    async def share_all(self, mid):
        from meeting_scribe.discord_ui.private_share import ShareReport

        self.calls.append(("share_all", mid))
        return ShareReport(dms=2, channels=1, failed=["Write the plan"])

    async def refresh_item(self, mid, iid):
        self.calls.append(("refresh_item", mid, iid))

    async def refresh(self, mid):
        self.calls.append(("refresh", mid))

    async def task_panel(self, mid, uid, scope, page, *, is_owner):
        self.calls.append(("panel", mid, uid, scope, page, is_owner))
        return ("panel", uid, scope, page)

    async def move_options(self, mid, iid, *, viewer):
        self.calls.append(("options", mid, iid, viewer))
        return [("502", "#nebula"), ("501", "#orion")]

    async def move_item(self, mid, iid, cid, *, viewer, learn):
        self.calls.append(("move", mid, iid, cid, learn))
        self.viewer = viewer
        return f"<#{cid}>"


@pytest.fixture
def env():
    svc, sink = Svc(), Sink()
    acts = ButtonActions(service=lambda: svc, settings=lambda space=None: settings_from_mapping({}), owners=lambda space=None: ("11",),
                         check_auth=lambda i: str(i.user.id) == str(ALLOWED), sink=lambda: sink,
                         project_view=lambda mid, cands: ("select", mid, tuple(c.key for c in cands)),
                         move_view=lambda mid, iid, opts: ("move", mid, iid, tuple(o[0] for o in opts)))
    return SimpleNamespace(svc=svc, sink=sink, acts=acts)


async def test_owner_approves_own_task_to_kanban_and_only_that_task_refreshes(env):
    i = FakeInteraction(OWNER)
    await env.acts.handle(i, "ok", "k3v7q2ab", "a1")
    assert env.svc.calls == [("approve", "k3v7q2ab", "a1", "kanban")]
    assert env.sink.calls == [("refresh_item", "k3v7q2ab", "a1")] and "t_1" in i.replies()


async def test_assignee_may_use_linear_and_dismiss_on_their_task(env):
    i = FakeInteraction(ANA)
    await env.acts.handle(i, "lin", "k3v7q2ab", "a2")
    await env.acts.handle(i, "no", "k3v7q2ab", "a2")
    assert [c[:3] for c in env.svc.calls] == [("approve", "k3v7q2ab", "a2"), ("dismiss", "k3v7q2ab", "a2")]


@pytest.mark.parametrize("user", [STRANGER, ALLOWED])  # even users Hermes authorizes
@pytest.mark.parametrize("action", ["lin", "no", "prj", "tsel"])
async def test_someone_else_is_told_whose_task_it_is(env, user, action):
    i = FakeInteraction(user, values=["501"])
    await env.acts.handle(i, action, "k3v7q2ab", "a2")
    assert env.svc.calls == [] and env.sink.calls == []
    assert "<@10>" in i.replies() and all(m.get("ephemeral") for m in i.response.sent)


async def test_owner_may_act_on_anyones_task_but_kanban_only_on_owner_tasks(env):
    i = FakeInteraction(OWNER)
    await env.acts.handle(i, "lin", "k3v7q2ab", "a2")
    await env.acts.handle(i, "ok", "k3v7q2ab", "a2")
    assert env.svc.calls == [("approve", "k3v7q2ab", "a2", "linear")]
    assert "Kanban" in i.replies()


async def test_assignee_who_is_not_an_owner_cannot_use_kanban(env):
    i = FakeInteraction(ANA)
    await env.acts.handle(i, "ok", "k3v7q2ab", "a2")
    assert env.svc.calls == []


async def test_unassigned_tasks_are_owners_only(env):
    i = FakeInteraction(ANA)
    await env.acts.handle(i, "no", "k3v7q2ab", "a3")
    assert env.svc.calls == [] and "owners" in i.replies().lower()
    await env.acts.handle(FakeInteraction(OWNER), "no", "k3v7q2ab", "a3")
    assert env.svc.calls == [("dismiss", "k3v7q2ab", "a3")]


async def test_unknown_task_is_a_friendly_error(env):
    i = FakeInteraction(OWNER)
    await env.acts.handle(i, "no", "k3v7q2ab", "zz")
    assert env.svc.calls == [] and "not found" in i.replies()


async def test_my_tasks_opens_an_ephemeral_panel_for_anyone(env):
    i = FakeInteraction(STRANGER)
    await env.acts.handle(i, "mine", "k3v7q2ab", "all")
    sent = i.followup.sent[0]
    assert sent["view"] == ("panel", "99", "m", 0) and sent["ephemeral"]
    j = FakeInteraction(OWNER)
    await env.acts.handle(j, "mine", "k3v7q2ab", "all")
    assert env.sink.calls[-1][5] is True  # owners get the "all tasks" switch


async def test_page_buttons_edit_the_panel_in_place(env):
    i = FakeInteraction(ANA, ephemeral=True)
    await env.acts.handle(i, "pg", "k3v7q2ab", "m2")
    assert i.original_edits == [{"view": ("panel", "10", "m", 2)}]
    j = FakeInteraction(ANA, ephemeral=True)
    await env.acts.handle(j, "pg", "k3v7q2ab", "a0")  # non-owners cannot switch to all tasks
    assert env.sink.calls[-1][3] == "a" and env.sink.calls[-1][5] is False


async def test_action_from_the_ephemeral_panel_rerenders_the_panel(env):
    i = FakeInteraction(ANA, ephemeral=True)
    await env.acts.handle(i, "no", "k3v7q2ab", "a2")
    assert i.response.defers == [{}]  # update-type defer: edit_original_response targets the panel
    assert i.original_edits and i.original_edits[0]["view"][0] == "panel"
    assert env.sink.calls[-1] == ("panel", "k3v7q2ab", "10", "m", 0, False)
    assert "dismissed" in i.replies().lower()


async def test_action_from_a_public_task_message_defers_ephemerally(env):
    i = FakeInteraction(ANA)
    await env.acts.handle(i, "no", "k3v7q2ab", "a2")
    assert i.response.defers == [{"ephemeral": True, "thinking": True}] and i.original_edits == []


async def test_move_offers_channels_then_select_moves_and_confirms(env):
    i = FakeInteraction(ANA)
    await env.acts.handle(i, "prj", "k3v7q2ab", "a2")
    assert i.followup.sent[0]["view"] == ("move", "k3v7q2ab", "a2", ("502", "501"))
    j = FakeInteraction(ANA, values=["502"])
    await env.acts.handle(j, "tsel", "k3v7q2ab", "a2")
    assert env.sink.calls[-1] == ("move", "k3v7q2ab", "a2", "502", False) and "<#502>" in j.replies()
    assert ("options", "k3v7q2ab", "a2", str(ANA)) in env.sink.calls and env.sink.viewer == str(ANA)
    await env.acts.handle(FakeInteraction(OWNER, values=["502"]), "tsel", "k3v7q2ab", "a2")
    assert env.sink.calls[-1][-1] is True  # only an owner's correction is learned for everyone


async def test_legacy_meeting_buttons_keep_working_for_owners(env):
    await env.acts.handle(FakeInteraction(OWNER), "allk", "k3v7q2ab", "all")
    await env.acts.handle(FakeInteraction(ALLOWED), "alll", "k3v7q2ab", "all")
    i = FakeInteraction(OWNER)
    await env.acts.handle(i, "prj", "k3v7q2ab", "all")
    assert i.followup.sent[0]["view"] == ("select", "k3v7q2ab", ("hermes:p1",))
    await env.acts.handle(FakeInteraction(OWNER, values=["hermes:p1"]), "psel", "k3v7q2ab", "all")
    assert [c[0] for c in env.svc.calls] == ["all", "all", "project"]
    s = FakeInteraction(STRANGER)
    await env.acts.handle(s, "alll", "k3v7q2ab", "all")
    await env.acts.handle(s, "allk", "k3v7q2ab", "all")
    assert len(env.svc.calls) == 3


async def test_service_errors_become_ephemeral_replies(env):
    from meeting_scribe.domain.errors import ItemDismissed
    env.svc.fail = ItemDismissed("action item a1 was dismissed")
    i = FakeInteraction(OWNER)
    await env.acts.handle(i, "ok", "k3v7q2ab", "a1")
    assert "dismissed" in i.replies() and "a1" not in i.replies() and env.sink.calls == []


@pytest.mark.parametrize("exc, expected", [
    (RuntimeError("HTTP 502 Bad Gateway from kanban.internal"), "could not be completed"),
    (KeyError("k3v7q2ab"), "couldn't find"),
    (LookupError("Unknown Channel 502"), "couldn't find"),
    (ValueError("invalid literal for int()"), "could not be completed"),
])
async def test_unexpected_errors_are_logged_not_shown(env, caplog, exc, expected):
    env.svc.fail = exc
    i = FakeInteraction(OWNER)
    await env.acts.handle(i, "ok", "k3v7q2ab", "a1")
    reply = i.replies()
    assert expected in reply and str(exc).strip("'") not in reply and type(exc).__name__ not in reply
    assert str(exc).strip("'") in caplog.text


async def test_known_errors_get_their_own_plain_sentence(env):
    from meeting_scribe.domain.errors import NotesNotReady, SinkUnavailable
    for exc, expected in ((SinkUnavailable("linear"), "Linear isn't connected"),
                          (NotesNotReady("meeting has no notes yet"), "aren't ready yet")):
        env.svc.fail = exc
        i = FakeInteraction(OWNER)
        await env.acts.handle(i, "ok", "k3v7q2ab", "a1")
        assert expected in i.replies() and "sink" not in i.replies()


async def test_panel_failure_hides_the_exception(env, caplog):
    async def broken(*a, **k):
        raise RuntimeError("sqlite3.OperationalError: database is locked")
    env.sink.task_panel = broken
    i = FakeInteraction(OWNER)
    await env.acts.handle(i, "mine", "k3v7q2ab", "all")
    assert "sqlite" not in i.replies() and "could not be completed" in i.replies()
    assert "database is locked" in caplog.text


async def test_approve_all_partial_failures_are_counted_not_listed(env, caplog):
    env.svc.approve_all = lambda mid, sink: SinkResult(sink, False, ("t_1",),
                                                       errors=("a2: RuntimeError: HTTP 500",))
    i = FakeInteraction(OWNER)
    await env.acts.handle(i, "allk", "k3v7q2ab", "all")
    assert "1 task(s) could not be sent" in i.replies() and "HTTP 500" not in i.replies()
    assert "HTTP 500" in caplog.text


# -- private meetings (DESIGN §19.2) --------------------------------------------------------------------
def in_channel(user, channel_id, *, can_see=True, parent=None):
    i = FakeInteraction(user)
    i.channel = SimpleNamespace(id=channel_id, parent_id=parent)
    i.permissions = SimpleNamespace(view_channel=can_see)
    return i


@pytest.mark.parametrize("action, how", [("shd", "dm"), ("shp", "project")])
async def test_a_member_of_the_private_channel_shares_one_task(env, action, how):
    env.sink.place = {"700"}
    i = in_channel(STRANGER, 700)  # not the owner, not the assignee: any member of the room decides
    await env.acts.handle(i, action, "k3v7q2ab", "a2")
    assert env.sink.calls == [("share", "k3v7q2ab", "a2", how)]
    assert ("Sent." if how == "dm" else "Published in <#501>.") in i.replies()
    assert i.response.defers and i.response.defers[0]["ephemeral"]


async def test_share_buttons_work_from_the_thread_of_the_private_channel(env):
    env.sink.place = {"700"}
    await env.acts.handle(in_channel(STRANGER, 9001, parent=700), "shd", "k3v7q2ab", "a2")
    assert env.sink.calls == [("share", "k3v7q2ab", "a2", "dm")]


@pytest.mark.parametrize("where, can_see, msg", [(555, True, "use the buttons in its own channel"),
                                                  (700, False, "Only people who can see this channel")])
@pytest.mark.parametrize("action", ["shd", "shp", "sha", "shc", "ok", "no", "mine"])
async def test_private_buttons_are_refused_outside_or_to_non_members(env, where, can_see, msg, action):
    env.sink.place = {"700"}
    i = in_channel(OWNER, where, can_see=can_see)
    await env.acts.handle(i, action, "k3v7q2ab", "a1")
    assert env.sink.calls == [] and env.svc.calls == [] and msg in i.replies()


async def test_private_buttons_are_refused_in_a_dm(env):
    env.sink.place = {"700"}
    i = FakeInteraction(OWNER, dm=True)
    i.channel = SimpleNamespace(id=700, parent_id=None)
    await env.acts.handle(i, "shd", "k3v7q2ab", "a1")
    assert env.sink.calls == [] and "own channel" in i.replies()


async def test_share_all_asks_first_and_then_reports(env):
    env.sink.place = {"700"}
    i = in_channel(STRANGER, 700)
    await env.acts.handle(i, "sha", "k3v7q2ab", "all")
    assert env.sink.calls == [] and "Share every open task?" in i.replies()
    assert i.response.sent[0]["ephemeral"]
    i2 = in_channel(STRANGER, 700)
    await env.acts.handle(i2, "shc", "k3v7q2ab", "all")
    assert env.sink.calls == [("share_all", "k3v7q2ab")]
    assert "2 sent to people, 1 published" in i2.replies() and "Write the plan" in i2.replies()


async def test_share_buttons_on_a_meeting_that_is_not_private_do_nothing(env):
    i = in_channel(STRANGER, 700)
    await env.acts.handle(i, "shp", "k3v7q2ab", "a1")
    assert env.sink.calls == [] and "not private" in i.replies()


async def test_normal_task_rules_still_apply_inside_the_private_channel(env):
    env.sink.place = {"700"}
    i = in_channel(STRANGER, 700)
    await env.acts.handle(i, "no", "k3v7q2ab", "a2")  # Ana's task: only Ana (or an owner) dismisses it
    assert env.svc.calls == [] and "<@10>" in i.replies()
    ok = in_channel(OWNER, 700)
    await env.acts.handle(ok, "ok", "k3v7q2ab", "a1")
    assert env.svc.calls == [("approve", "k3v7q2ab", "a1", "kanban")]
