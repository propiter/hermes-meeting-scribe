"""Shared Discord conversations through the REAL Hermes gateway (DESIGN §16.3).

A Discord thread is ONE Hermes session for every user by default (``thread_sessions_per_user: false``).
A message from a member that arrives while the owner's turn runs is executed by the gateway as a follow-up
(``_run_agent_queued_followup``) inside the owner's task, with the owner's ``HERMES_SESSION_*`` context.
So the plugin never trusts that identity to write there: it tells a shared session by its key (no user
slot, hermes-agent ``gateway/session.py`` ``build_session_key``) and posts a confirmation instead.

Run with ``scripts/test-integration.sh``; skipped when Hermes is not importable.
"""
from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("hermes_cli.plugins", reason="Hermes is not importable (set PYTHONPATH)")

from .test_hermes_plugin_load import TOOLS, hermes_home, manager  # noqa: E402,F401  (fixtures)
from .test_task_tools_hermes import _published_meeting  # noqa: E402

OWNER, MEMBER = "100000000000000900", "100000000000000099"
THREAD = "7001"


@pytest.fixture(autouse=True)
def _gateway_loaded():
    """Import Hermes' gateway BEFORE the plugin manager snapshots the tool registry: importing it (and
    constructing a runner) registers Hermes' own tools, which ``manager`` would otherwise count as leaked
    by the plugin."""
    import gateway.run  # noqa: F401
    import tools.process_registry  # noqa: F401


def _src(user, message):
    from gateway.config import Platform
    from gateway.session import SessionSource

    return SessionSource(platform=Platform.DISCORD, chat_id=THREAD, chat_type="thread", user_id=user,
                         user_name=user, thread_id=THREAD, message_id=message, parent_chat_id="7000")


def _bound_caller(config):
    """The plugin's view of a turn Hermes bound for ``OWNER`` in the thread, with ``config``."""
    from gateway.run import GatewayRunner
    from gateway.session import build_session_context, build_session_key
    from meeting_scribe.commands import caller_from_session

    runner = GatewayRunner(config=config)
    source = _src(OWNER, "900000000000000001")
    context = build_session_context(source, config, None)
    context.session_key = build_session_key(source, group_sessions_per_user=config.group_sessions_per_user,
                                            thread_sessions_per_user=config.thread_sessions_per_user)
    tokens = runner._set_session_env(context)
    try:
        return caller_from_session()
    finally:
        runner._clear_session_env(tokens)


def test_a_thread_is_one_session_for_everyone_and_the_plugin_knows_it(manager):
    from gateway.config import GatewayConfig
    from gateway.session import build_session_key

    shared = GatewayConfig()
    assert build_session_key(_src(OWNER, "1"), group_sessions_per_user=shared.group_sessions_per_user,
                             thread_sessions_per_user=shared.thread_sessions_per_user) == \
        build_session_key(_src(MEMBER, "2"), group_sessions_per_user=shared.group_sessions_per_user,
                          thread_sessions_per_user=shared.thread_sessions_per_user)
    assert not _bound_caller(shared).per_user_session
    isolated = GatewayConfig(thread_sessions_per_user=True)
    assert _bound_caller(isolated).per_user_session


def test_a_member_s_follow_up_in_the_owner_s_turn_never_writes_as_the_owner(manager, monkeypatch):
    """The review's case: the member asks, inside the owner's turn, to give a task to someone else. The tool
    must not run it with the owner's rights: it posts a confirmation, and nothing changes until someone
    presses ✅ (as themselves)."""
    import meeting_scribe.plugin as plugin
    from gateway.config import GatewayConfig
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import build_session_context, build_session_key
    from gateway.session_context import get_session_env
    from gateway.turn_context import TurnContext
    from tools.registry import registry

    service, meeting = _published_meeting()
    rt = next(iter(plugin.RUNTIMES.values()))
    monkeypatch.setattr("meeting_scribe.runtime.effective_owners", lambda settings, secret: (OWNER,))
    assert rt.owners() == (OWNER,)
    posts = []
    monkeypatch.setattr(plugin, "_proposer", lambda runtime: lambda *a: posts.append(a) or "5001")
    runner = GatewayRunner(config=GatewayConfig())
    seen = {}

    async def run_agent(**kw):  # the follow-up turn: the model calls the tool for the MEMBER's request
        seen["asker"] = kw["source"].user_id
        seen["env_user"] = get_session_env("HERMES_SESSION_USER_ID")
        seen["out"] = json.loads(registry.dispatch("meeting_task_assign", {
            "meeting_id": meeting.id, "task_id": "fix-mail", "assignee": "<@100000000000000055>"}))
        return {"final_response": "ok", "messages": []}

    async def text(**kw):
        return kw["event"].text

    async def noop(*a, **k):
        return None

    runner._run_agent = run_agent
    runner._prepare_profile_scoped_inbound_message_text = text
    runner._pinned_channel_inputs = lambda key, prompt, source, internal=False: (prompt, source)
    runner._persist_prompt_pins = noop
    runner._refresh_agent_cache_message_count = noop
    runner._delivery_adapter_for = lambda source: None
    runner._intake_adapter_for = lambda source: None

    async def scenario():
        owner = _src(OWNER, "900000000000000001")
        context = build_session_context(owner, runner.config, None)
        context.session_key = build_session_key(owner)
        tokens = runner._set_session_env(context)  # the owner's turn
        try:
            turn = TurnContext(session_key=context.session_key, source=owner, session_id="s", run_generation=1,
                               history=[])
            member_msg = MessageEvent(text="asígnale fix-mail a <@100000000000000055>",
                                      source=_src(MEMBER, "900000000000000002"), message_id="900000000000000002")
            await runner._run_agent_queued_followup(turn, None, member_msg.text, member_msg, "",
                                                    {"interrupted": True, "messages": []}, None)
        finally:
            runner._clear_session_env(tokens)

    asyncio.run(scenario())
    assert seen["asker"] == MEMBER  # the gateway knows the follow-up is the member's ...
    assert seen["env_user"] in (OWNER, MEMBER)  # ... the session identity may still say the owner: not trusted
    assert seen["out"]["status"] == "pending_confirmation", seen["out"]
    assert service.repo.task_history(meeting.id) == []  # nothing was done with anyone's rights
    [(chat, body, mid, pid, _lang)] = posts
    assert chat == THREAD and mid == meeting.id and "<@" not in body
    assert service.repo.get_task_proposal(pid)["state"] == "pending"


def test_whatever_identity_a_follow_up_runs_with_the_plugin_sees_a_shared_session(manager):
    """Hermes' side of the case (what the review measured: during the follow-up the session context still
    names the first user). The plugin does not depend on which user it names: in that turn the session is
    shared, so nobody's identity is trusted to write."""
    from gateway.config import GatewayConfig
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import build_session_context, build_session_key
    from gateway.turn_context import TurnContext
    from meeting_scribe.commands import caller_from_session

    runner = GatewayRunner(config=GatewayConfig())
    seen = {}

    async def run_agent(**kw):
        seen["source_user"] = kw["source"].user_id
        seen["caller"] = caller_from_session()
        return {"final_response": "ok", "messages": []}

    async def passthrough(**kw):
        return kw["event"].text

    async def noop(*a, **k):
        return None

    runner._run_agent = run_agent
    runner._prepare_profile_scoped_inbound_message_text = passthrough
    runner._pinned_channel_inputs = lambda key, prompt, source, internal=False: (prompt, source)
    runner._persist_prompt_pins = noop
    runner._refresh_agent_cache_message_count = noop
    runner._delivery_adapter_for = lambda source: None
    runner._intake_adapter_for = lambda source: None

    async def scenario():
        owner = _src(OWNER, "900000000000000001")
        context = build_session_context(owner, runner.config, None)
        context.session_key = build_session_key(owner)
        tokens = runner._set_session_env(context)
        try:
            turn = TurnContext(session_key=context.session_key, source=owner, session_id="s1", run_generation=1,
                               history=[])
            event = MessageEvent(text="asígnamela a mí", source=_src(MEMBER, "900000000000000002"),
                                 message_id="900000000000000002")
            await runner._run_agent_queued_followup(turn, None, event.text, event, "",
                                                    {"interrupted": True, "messages": []}, None)
        finally:
            runner._clear_session_env(tokens)

    asyncio.run(scenario())
    assert seen["source_user"] == MEMBER
    caller = seen["caller"]
    assert caller.platform == "discord" and caller.user_id in (OWNER, MEMBER)
    assert not caller.per_user_session  # so the write tools propose instead of acting
