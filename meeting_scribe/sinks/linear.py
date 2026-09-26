"""Linear sink (DESIGN §8).

Backends, selected per call (keys can be added while the gateway runs):
  A. GraphQL ``https://api.linear.app/graphql`` with ``LINEAR_API_KEY`` (personal key, sent as
     ``Authorization: <key>`` — no ``Bearer``). stdlib ``urllib``; no new dependency.
  B. Hermes MCP server named ``linear`` via ``ctx.call_mcp`` when allowlisted. Tool names differ
     between Linear MCP versions, so they are discovered defensively and remembered.
  Neither → inactive; the sink is skipped silently and ``doctor`` explains why.
Person matching: learned link (id, then email) > exact displayName/name > fuzzy (difflib ≥ 0.85),
active users only.
"""
from __future__ import annotations

import difflib
import json
import re
import unicodedata
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from ..config import Settings
from ..domain.models import ActionItem, Meeting, Notes, Speaker
from ..i18n import t
from ..storage.artifacts import fmt_ts
from .base import DeliveryStore, ItemSink, ProjectFor

API_URL = "https://api.linear.app/graphql"
FUZZY = 0.85
Transport = Callable[[str, Mapping[str, str], bytes], Mapping[str, Any]]
McpCaller = Callable[[str, str, dict], Mapping[str, Any]]


class LinearError(RuntimeError):
    pass


class LinearBackend(Protocol):
    def teams(self) -> list[dict[str, Any]]: ...

    def users(self) -> list[dict[str, Any]]: ...

    def projects(self) -> list[dict[str, Any]]: ...

    def create_issue(self, issue: Mapping[str, Any]) -> dict[str, Any]: ...


def _urllib_transport(url: str, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:
    req = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise LinearError(f"Linear HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}") from exc
    except urllib.error.URLError as exc:
        raise LinearError(f"Linear unreachable: {exc.reason}") from exc


class LinearGraphQL:
    def __init__(self, api_key: Callable[[], Optional[str]], transport: Transport = _urllib_transport) -> None:
        self._key = api_key
        self._transport = transport

    def _q(self, query: str, variables: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        key = self._key()
        if not key:
            raise LinearError("LINEAR_API_KEY is not set")
        body = json.dumps({"query": query, "variables": dict(variables or {})}).encode("utf-8")
        data = self._transport(API_URL, {"Authorization": key, "Content-Type": "application/json"}, body)
        if data.get("errors"):
            raise LinearError("; ".join(str(e.get("message")) for e in data["errors"]))
        return data.get("data") or {}

    def viewer(self) -> dict[str, Any]:
        return dict(self._q("query { viewer { id name email } }")["viewer"])

    def teams(self) -> list[dict[str, Any]]:
        return list(self._q("query { teams(first: 250) { nodes { id key name } } }")["teams"]["nodes"])

    def users(self) -> list[dict[str, Any]]:
        q = "query { users(first: 250) { nodes { id name displayName email active } } }"
        return list(self._q(q)["users"]["nodes"])

    def projects(self) -> list[dict[str, Any]]:
        q = "query { projects(first: 250) { nodes { id name teams { nodes { id } } } } }"
        return [{"id": p["id"], "name": p["name"], "team_ids": [x["id"] for x in p["teams"]["nodes"]]}
                for p in self._q(q)["projects"]["nodes"]]

    def create_issue(self, issue: Mapping[str, Any]) -> dict[str, Any]:
        q = ("mutation($input: IssueCreateInput!) { issueCreate(input: $input) { success "
             "issue { id identifier url } } }")
        res = self._q(q, {"input": dict(issue)})["issueCreate"]
        if not res.get("success"):
            raise LinearError("issueCreate returned success=false")
        return dict(res["issue"])


def _mcp_payload(result: Mapping[str, Any]) -> Any:
    if not result.get("ok", True):
        raise LinearError(str(result.get("error")))
    payload = result.get("structuredContent") or result.get("result")
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except ValueError:
            return payload
    return payload


def _nodes(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(x) for x in payload if isinstance(x, Mapping)]
    if isinstance(payload, Mapping):
        for k in (*keys, "nodes", "items", "results"):
            if isinstance(payload.get(k), list):
                return _nodes(payload[k])
    return []


class LinearMcp:
    """Backend over the user's ``linear`` MCP server (``ctx.call_mcp``)."""

    CANDIDATES = {"create": ("create_issue", "save_issue", "linear_create_issue"),
                  "teams": ("list_teams", "get_teams", "linear_list_teams"),
                  "users": ("list_users", "get_users", "linear_list_users"),
                  "projects": ("list_projects", "get_projects", "linear_list_projects")}

    def __init__(self, call: McpCaller, server: str = "linear") -> None:
        self._call = call
        self._server = server
        self._resolved: dict[str, str] = {}

    def _invoke(self, op: str, args: dict) -> Any:
        names = [self._resolved[op]] if op in self._resolved else list(self.CANDIDATES[op])
        last = ""
        for name in names:
            result = self._call(self._server, name, args)
            if result.get("ok", True):
                self._resolved[op] = name
                return _mcp_payload(result)
            last = str(result.get("error"))
            if not re.search(r"unknown|not found|no such", last, re.IGNORECASE):
                raise LinearError(last)
        raise LinearError(f"linear MCP has no {op} tool ({last})")

    def teams(self) -> list[dict[str, Any]]:
        return _nodes(self._invoke("teams", {}), "teams")

    def users(self) -> list[dict[str, Any]]:
        return _nodes(self._invoke("users", {}), "users")

    def projects(self) -> list[dict[str, Any]]:
        return _nodes(self._invoke("projects", {}), "projects")

    def create_issue(self, issue: Mapping[str, Any]) -> dict[str, Any]:
        args = {"title": issue["title"], "team": issue["teamId"], "teamId": issue["teamId"],
                "description": issue.get("description", "")}
        for src, dst in (("assigneeId", "assignee"), ("projectId", "project"), ("dueDate", "dueDate")):
            if issue.get(src):
                args[dst] = issue[src]
        payload = self._invoke("create", args)
        data = payload.get("issue", payload) if isinstance(payload, Mapping) else {}
        return {"id": str(data.get("id") or data.get("identifier") or ""), "url": data.get("url"),
                "identifier": data.get("identifier")}


def select_backend(api_key: Callable[[], Optional[str]], mcp: Optional[McpCaller]) -> Optional[LinearBackend]:
    if api_key():
        return LinearGraphQL(api_key)
    if mcp is not None:
        return LinearMcp(mcp)
    return None


def _norm(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.sub(r"[^a-z0-9@. ]+", " ", s).split())


def match_linear_user(speaker: Speaker, users: Sequence[Mapping[str, Any]],
                      link: Optional[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    active = [u for u in users if u.get("active", True)]
    if link:
        if link.get("linear_user_id"):
            hit = next((u for u in users if u.get("id") == link["linear_user_id"]), None)
            if hit:
                return hit
        if link.get("email"):
            hit = next((u for u in active if _norm(u.get("email", "")) == _norm(link["email"])), None)
            if hit:
                return hit
    wanted = _norm((link or {}).get("name") or speaker.name)
    for field in ("displayName", "name"):
        exact = [u for u in active if _norm(u.get(field) or "") == wanted]
        if len(exact) == 1:
            return exact[0]
    scored = sorted(((max(difflib.SequenceMatcher(None, wanted, _norm(u.get(f) or "")).ratio()
                          for f in ("displayName", "name")), u) for u in active), key=lambda x: -x[0])
    if scored and scored[0][0] >= FUZZY and (len(scored) == 1 or scored[1][0] < scored[0][0]):
        return scored[0][1]
    return None


class LinearSink(ItemSink):
    name = "linear"

    def __init__(self, settings: Callable[[], Settings], store: DeliveryStore,
                 backend: Callable[[], Optional[LinearBackend]], *, project_for: ProjectFor) -> None:
        super().__init__(settings, store, project_for)
        self._backend = backend
        self._link = getattr(store, "get_link", lambda _id: None)

    def mode(self) -> str:
        return self._settings().linear_mode

    def active(self) -> bool:
        return self._backend() is not None

    def _team(self, backend: LinearBackend, team_ids: Sequence[str]) -> str:
        if team_ids:
            return str(team_ids[0])
        wanted = self._settings().linear_default_team.strip()
        if wanted:
            for team in backend.teams():
                if wanted.lower() in (str(team.get("id")).lower(), str(team.get("key")).lower(),
                                      str(team.get("name")).lower()):
                    return str(team["id"])
        raise LinearError("no Linear team: resolve a Linear project or set linear.default_team")

    def _create(self, meeting: Meeting, notes: Notes, item: ActionItem, folder: Path,
                key: str) -> tuple[str, Optional[str]]:
        backend = self._backend()
        if backend is None:
            raise LinearError("Linear is not connected")
        cand = self._project_for(meeting, notes, item)
        ref = cand.ref if cand is not None and cand.source == "linear" else {}
        lang = notes.language or self._settings().ui_language
        when = meeting.started_at + timedelta(seconds=item.t0 or 0)
        issue: dict[str, Any] = {
            "teamId": self._team(backend, list(ref.get("team_ids") or ())), "title": item.title,
            "description": t("sink.linear_issue_body", lang, description=item.description or item.title,
                             quote=item.quote or "-", title=notes.meeting_title or meeting.title,
                             date=when.date().isoformat(), ts=fmt_ts(item.t0 or 0)) + f"\n\n`{key}`"}
        if ref.get("project_id"):
            issue["projectId"] = ref["project_id"]
        if item.due:
            issue["dueDate"] = item.due
        if item.owner_speaker_id:
            speaker = next((s for s in meeting.speakers if s.user_id == item.owner_speaker_id),
                           Speaker(item.owner_speaker_id, item.owner_name or ""))
            user = match_linear_user(speaker, backend.users(), self._link(item.owner_speaker_id))
            if user:
                issue["assigneeId"] = user["id"]
        created = backend.create_issue(issue)
        return str(created.get("id")), created.get("url")
