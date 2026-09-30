"""The agent's task tools through the REAL Hermes loader and tool registry (DESIGN §16.3): registered in
the plugin's toolset with closed schemas, and acting only as the Discord user Hermes bound to the turn
(``gateway.session_context``) — with no Discord user bound they refuse to write.

Run with ``scripts/test-integration.sh``; skipped when Hermes is not importable.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("hermes_cli.plugins", reason="Hermes is not importable (set PYTHONPATH)")

from .test_hermes_plugin_load import TOOLS, hermes_home, manager  # noqa: E402,F401  (fixtures)

WRITE_TOOLS = ("meeting_task_assign", "meeting_task_send")


def _published_meeting():
    """A processed meeting with an unassigned task and its card, written straight into the plugin's store."""
    import meeting_scribe.plugin as plugin
    from meeting_scribe.domain.models import ActionItem, Meeting, MeetingState, Notes, Speaker
    from meeting_scribe.storage.artifacts import write_notes

    rt = next(iter(plugin.RUNTIMES.values()))
    service = rt.service()
    meeting = Meeting(id="t4sk0001", guild_id="", channel_id="200", channel_name="Weekly",
                      started_at=datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc), state=MeetingState.DONE,
                      title="Weekly", speakers=(Speaker("10", "Ana"),), space=rt.default_space())
    meeting = service.runner.stages.persist(meeting)
    notes = Notes(meeting_title="Weekly", tldr="t", summary="s",
                  action_items=(ActionItem(id="fix-mail", title="Fix the mail"),))
    write_notes(service.folder(meeting), meeting, notes, "en")
    service.repo.sync_action_items(meeting.id, notes.action_items)
    service.repo.upsert_delivery(meeting.id, "discord", f"mtg:{meeting.id}:task:fix-mail",
                                 external_id=json.dumps({"channel": "7001", "message": "8001"}), url="")
    return service, replace(meeting)


def test_task_tools_are_registered_with_closed_schemas(manager):
    from tools.registry import registry

    loaded = manager._plugins["meeting-scribe"]
    assert set(loaded.tools_registered) == TOOLS
    for name in TOOLS:
        entry = registry.get_entry(name)
        assert entry is not None and entry.toolset == "meeting_scribe", name
        params = registry.get_schema(name)["parameters"]
        assert params["type"] == "object" and params["additionalProperties"] is False, name


def test_without_a_discord_user_in_the_session_the_write_tools_refuse(manager):
    from tools.registry import registry

    service, meeting = _published_meeting()
    out = json.loads(registry.dispatch("meeting_task_assign", {"meeting_id": meeting.id, "task_id": "fix-mail",
                                                               "assignee": "me"}))
    assert out["code"] == "no_identity"
    out = json.loads(registry.dispatch("meeting_task_send", {"message_id": "8001", "target": "linear"}))
    assert out["code"] == "no_identity"
    assert service.repo.task_history(meeting.id) == []
    listed = json.loads(registry.dispatch("meeting_task_list", {"meeting_id": meeting.id}))
    assert [t["id"] for t in listed["tasks"]] == ["fix-mail"]  # reading still works


def test_a_cron_or_other_platform_session_cannot_write_either(manager):
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.registry import registry

    service, meeting = _published_meeting()
    for kwargs in ({"platform": "discord", "chat_id": "7001", "user_id": "10", "cron_session": "1"},
                   {"platform": "telegram", "chat_id": "7001", "user_id": "10"},
                   {"source": "cli"}):
        tokens = set_session_vars(**kwargs)
        try:
            out = json.loads(registry.dispatch("meeting_task_assign", {"message_id": "8001", "assignee": "me"}))
        finally:
            clear_session_vars(tokens)
        assert out["code"] == "no_identity", kwargs
    assert service.repo.task_history(meeting.id) == []


def test_the_discord_user_bound_by_hermes_is_who_takes_the_task(manager):
    """The identity comes from Hermes' own session binding (what the gateway sets for each Discord turn), in
    a conversation Hermes keys to this user alone (``thread_sessions_per_user: true``: the user slot last)."""
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.registry import registry

    service, meeting = _published_meeting()
    tokens = set_session_vars(platform="discord", chat_id="7001", chat_type="thread", user_id="10", cron_session="",
                              session_key="agent:main:discord:thread:7001:7001:10")
    try:
        out = json.loads(registry.dispatch("meeting_task_assign", {"message_id": "8001", "assignee": "me"}))
        other = json.loads(registry.dispatch("meeting_task_assign", {"message_id": "8001", "assignee": "<@11>"}))
    finally:
        clear_session_vars(tokens)
    assert out["ok"] and out["assignee"] == "10" and out["task_id"] == "fix-mail"
    assert other["code"] == "self_only"  # asking for someone else never widens what the user may do
    [audit] = service.repo.task_history(meeting.id)
    assert (audit["actor"], audit["next_user"]) == ("10", "10")
