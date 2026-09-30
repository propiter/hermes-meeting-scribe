import json

import pytest

from meeting_scribe.domain.models import ActionStatus, Candidate, Speaker
from meeting_scribe.sinks.linear import (
    LinearError, LinearGraphQL, LinearMcp, LinearSink, match_linear_user, select_backend,
)

TEAMS = [{"id": "team_1", "key": "ENG", "name": "Engineering"}, {"id": "team_2", "key": "OPS", "name": "Ops"}]
USERS = [{"id": "u_ana", "name": "Ana María Ruiz", "displayName": "ana", "email": "ana@x.io", "active": True},
         {"id": "u_luis", "name": "Luis Pérez", "displayName": "luisp", "email": "luis@x.io", "active": True},
         {"id": "u_old", "name": "Luis Perez", "displayName": "old", "email": "o@x.io", "active": False}]


class FakeTransport:
    def __init__(self):
        self.requests = []

    def __call__(self, url, headers, body):
        req = json.loads(body)
        self.requests.append((url, headers, req))
        q = req["query"]
        if "issueUpdate" in q:
            return {"data": {"issueUpdate": {"success": True}}}
        if "issueCreate" in q:
            inp = req["variables"]["input"]
            return {"data": {"issueCreate": {"success": True, "issue": {
                "id": "iss_1", "identifier": "ENG-1", "url": "https://linear.app/x/issue/ENG-1", "_in": inp}}}}
        if "projects" in q:
            return {"data": {"projects": {"nodes": [{"id": "prj_9", "name": "Infra",
                                                     "teams": {"nodes": [{"id": "team_2"}]}}]}}}
        if "teams" in q:
            return {"data": {"teams": {"nodes": TEAMS}}}
        if "users" in q:
            return {"data": {"users": {"nodes": USERS}}}
        if "viewer" in q:
            return {"data": {"viewer": {"id": "me", "name": "Pedro", "email": "p@x.io"}}}
        raise AssertionError(q)


def gql(transport=None):
    return LinearGraphQL(lambda: "lin_api_KEY", transport=transport or FakeTransport())


def test_graphql_auth_header_has_no_bearer():
    t = FakeTransport()
    gql(t).viewer()
    url, headers, _ = t.requests[0]
    assert url == "https://api.linear.app/graphql" and headers["Authorization"] == "lin_api_KEY"


def test_graphql_errors_raise():
    client = LinearGraphQL(lambda: "k", transport=lambda u, h, b: {"errors": [{"message": "bad"}]})
    with pytest.raises(LinearError, match="bad"):
        client.teams()


def test_graphql_lists_and_create():
    client = gql()
    assert [t["key"] for t in client.teams()] == ["ENG", "OPS"]
    assert client.projects()[0]["team_ids"] == ["team_2"]
    issue = client.create_issue({"teamId": "team_1", "title": "T"})
    assert issue["url"].endswith("ENG-1") and issue["id"] == "iss_1"


def test_match_linear_user_precedence():
    ana = Speaker("10", "Ana")
    assert match_linear_user(ana, USERS, link={"linear_user_id": "u_luis"})["id"] == "u_luis"
    assert match_linear_user(ana, USERS, link={"email": "ANA@x.io"})["id"] == "u_ana"
    assert match_linear_user(ana, USERS, link=None)["id"] == "u_ana"  # displayName exact
    assert match_linear_user(Speaker("11", "Luis Perez"), USERS, None)["id"] == "u_luis"  # fuzzy, active only
    assert match_linear_user(Speaker("12", "Zed"), USERS, None) is None


class FakeMcp:
    def __init__(self, tools):
        self.tools = tools
        self.calls = []

    def __call__(self, server, tool, args):
        self.calls.append((server, tool, args))
        if tool not in self.tools:
            return {"ok": False, "error": f"Unknown tool {tool}"}
        return {"ok": True, "result": self.tools[tool](args)}


def test_mcp_backend_discovers_tool_names():
    mcp = FakeMcp({"save_issue": lambda a: json.dumps({"id": "i9", "url": "https://linear.app/i9"}),
                   "list_teams": lambda a: json.dumps({"teams": TEAMS})})
    backend = LinearMcp(mcp)
    assert backend.create_issue({"teamId": "team_1", "title": "x"})["url"] == "https://linear.app/i9"
    assert [c[1] for c in mcp.calls] == ["create_issue", "save_issue"]
    backend.create_issue({"teamId": "team_1", "title": "y"})
    assert mcp.calls[-1][1] == "save_issue"  # remembered
    assert [t["key"] for t in backend.teams()] == ["ENG", "OPS"]
    assert mcp.calls[-1][2] == {}


def test_select_backend():
    assert isinstance(select_backend(lambda: "k", None), LinearGraphQL)
    assert isinstance(select_backend(lambda: None, FakeMcp({})), LinearMcp)
    assert select_backend(lambda: None, None) is None


def _sink(repo, settings, backend, resolve=lambda m, n, i: None):
    return LinearSink(settings, repo, lambda: backend, project_for=resolve)


def test_sink_inactive_without_backend(repo, meeting, settings_of):
    sink = LinearSink(settings_of(linear__mode="auto"), repo, lambda: None, project_for=lambda m, n, i: None)
    assert sink.enabled(meeting) is False
    assert _sink(repo, settings_of(linear__mode="off"), gql()).enabled(meeting) is False


def test_sink_auto_creates_issue_with_team_project_assignee(tmp_path, repo, meeting, notes, settings_of):
    t = FakeTransport()
    infra = Candidate("linear:prj_9", "Infra", "linear", {"project_id": "prj_9", "team_ids": ["team_2"]})
    repo.sync_action_items(meeting.id, notes.action_items)
    sink = _sink(repo, settings_of(linear__mode="auto"), gql(t), resolve=lambda m, n, i: infra)
    res = sink.deliver(meeting, notes, tmp_path)
    creates = [r[2]["variables"]["input"] for r in t.requests if "issueCreate" in r[2]["query"]]
    assert len(creates) == 2 and res.ok
    first = creates[0]
    assert first["teamId"] == "team_2" and first["projectId"] == "prj_9" and first["assigneeId"] == "u_luis"
    assert first["dueDate"] == "2026-10-02" and "Yo envío las credenciales" in first["description"]
    assert "assigneeId" not in creates[1]
    sink.deliver(meeting, notes, tmp_path)
    assert len([r for r in t.requests if "issueCreate" in r[2]["query"]]) == 2  # idempotent
    assert repo.get_delivery("linear", "mtg:k3v7q2ab:a0000000001")["url"].endswith("ENG-1")


def test_sink_default_team_and_approve_mode(tmp_path, repo, meeting, notes, settings_of):
    t = FakeTransport()
    repo.sync_action_items(meeting.id, notes.action_items)
    sink = _sink(repo, settings_of(linear__mode="approve", linear__default_team="OPS"), gql(t))
    sink.deliver(meeting, notes, tmp_path)
    assert not [r for r in t.requests if "issueCreate" in r[2]["query"]]
    repo.set_item_sink_status(meeting.id, "a0000000002", "linear", "approved")
    sink.deliver(meeting, notes, tmp_path)
    creates = [r[2]["variables"]["input"] for r in t.requests if "issueCreate" in r[2]["query"]]
    assert len(creates) == 1 and creates[0]["teamId"] == "team_2" and "projectId" not in creates[0]


def test_sink_without_team_reports_error(tmp_path, repo, meeting, notes, settings_of):
    repo.sync_action_items(meeting.id, notes.action_items)
    res = _sink(repo, settings_of(linear__mode="auto"), gql()).deliver(meeting, notes, tmp_path)
    assert not res.ok and "team" in res.errors[0]


def test_learned_link_wins(tmp_path, repo, meeting, notes, settings_of):
    t = FakeTransport()
    repo.set_link("main", "11", linear_user_id="u_ana")
    _sink(repo, settings_of(linear__mode="auto", linear__default_team="ENG"), gql(t)).deliver_item(
        meeting, notes, notes.action_items[0], tmp_path)
    create = [r[2]["variables"]["input"] for r in t.requests if "issueCreate" in r[2]["query"]][0]
    assert create["assigneeId"] == "u_ana"


class PagedTransport:
    """Linear connections are paginated (``pageInfo``); 600 users arrive in three pages."""

    def __init__(self, total: int = 600, page: int = 250):
        self.total, self.page, self.calls = total, page, []

    def __call__(self, url, headers, body):
        req = json.loads(body)
        self.calls.append(req)
        field = next(f for f in ("users", "projects", "teams") if f + "(" in req["query"])
        after = int(req["variables"].get("after") or 0)
        first = int(req["variables"]["first"])
        ids = range(after, min(after + first, self.total))
        node = {"users": lambda i: {"id": f"u{i}", "name": f"User {i}", "displayName": f"u{i}", "email": "",
                                    "active": True},
                "projects": lambda i: {"id": f"p{i}", "name": f"P{i}", "teams": {"nodes": [{"id": "t"}]}},
                "teams": lambda i: {"id": f"t{i}", "key": f"K{i}", "name": f"T{i}"}}[field]
        end = after + len(ids)
        return {"data": {field: {"nodes": [node(i) for i in ids],
                                 "pageInfo": {"hasNextPage": end < self.total, "endCursor": str(end)}}}}


@pytest.mark.parametrize("method", ["users", "projects", "teams"])
def test_graphql_lists_follow_pagination(method):
    """Review finding 11: workspaces with more than 250 users/projects lost matches silently."""
    t = PagedTransport()
    rows = getattr(LinearGraphQL(lambda: "k", transport=t), method)()
    assert len(rows) == 600 and len(t.calls) == 3
    assert t.calls[1]["variables"]["after"] == "250"


def test_user_on_page_three_is_matched():
    users = LinearGraphQL(lambda: "k", transport=PagedTransport()).users()
    assert match_linear_user(Speaker("1", "User 599"), users, None)["id"] == "u599"


def test_an_issue_already_created_follows_the_task_s_new_assignee(tmp_path, repo, meeting, notes, settings_of):
    """DESIGN §16.2: reassigning a task that is already in Linear moves the issue to the mapped user; a
    person without a Linear user leaves the issue unassigned (never with the previous assignee)."""
    from dataclasses import replace

    t = FakeTransport()
    repo.sync_action_items(meeting.id, notes.action_items)
    sink = _sink(repo, settings_of(linear__mode="auto", linear__default_team="ENG"), gql(t))
    item = notes.action_items[1]
    assert sink.set_assignee(meeting, item) == "not_sent"  # nothing in Linear yet: nothing to change
    sink.deliver(meeting, notes, tmp_path)
    ana = replace(item, owner_speaker_id="10", owner_name="Ana María Ruiz")
    assert sink.set_assignee(meeting, ana) == "synced"
    stranger = replace(item, owner_speaker_id="55", owner_name="Nadie Conocido")
    assert sink.set_assignee(meeting, stranger) == "unmapped"
    assert sink.set_assignee(meeting, replace(item, owner_speaker_id=None, owner_name=None)) == "cleared"
    updates = [r[2]["variables"] for r in t.requests if "issueUpdate" in r[2]["query"]]
    assert [u["input"]["assigneeId"] for u in updates] == ["u_ana", None, None]
    assert {u["id"] for u in updates} == {"iss_1"}


def test_mcp_backend_updates_the_assignee():
    mcp = FakeMcp({"update_issue": lambda a: json.dumps({"ok": True})})
    LinearMcp(mcp).update_issue("iss_1", {"assigneeId": "u_ana"})
    assert mcp.calls == [("linear", "update_issue", {"id": "iss_1", "assignee": "u_ana", "assigneeId": "u_ana"})]
