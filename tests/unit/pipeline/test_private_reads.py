"""A private meeting does not exist for chats outside its private channel (DESIGN §19.2): the agent's
tools and the chat commands. The CLI (no chat session) and Desktop see everything."""
from __future__ import annotations

import json

import pytest

from meeting_scribe import privacy
from meeting_scribe.commands import Caller, MeetingCommands
from meeting_scribe.config import settings_from_mapping
from meeting_scribe.pipeline.service import MeetingService
from meeting_scribe.privacy import Reader
from meeting_scribe.tools import MeetingTools

from .test_commands import processed
from .test_runner import build

PRIVATE_CHANNEL = "700"
OUTSIDE = Caller(platform="discord", chat_id="555", user_id="10", thread_id="")
INSIDE = Caller(platform="discord", chat_id=PRIVATE_CHANNEL, user_id="10", thread_id="")
IN_ITS_THREAD = Caller(platform="discord", chat_id="9001", user_id="10", thread_id="9001",
                       parent_chat_id=PRIVATE_CHANNEL)


@pytest.fixture
def world(prepo, layout, clock, meeting):
    s = settings_from_mapping({"audio_retention": "none",
                               "meeting_routes": [f"Daily Sync = {PRIVATE_CHANNEL}:private"]})
    settings = lambda space=None: s  # noqa: E731
    runner, *_ = build(prepo, layout, settings, clock)
    service = MeetingService(prepo, layout, runner, settings, clock=clock, item_sinks=lambda: {},
                             catalogs=lambda: runner.stages.catalogs())
    mid = processed(service, runner, meeting)
    return service, MeetingCommands(service, settings, capture=lambda: None), mid


def test_recording_under_a_private_rule_marks_the_meeting_private_for_good(world):
    service, _, mid = world
    assert privacy.record(service.repo, mid) == {"rule": "Daily Sync", "channel": ""}
    assert service.is_private(service.repo.get_meeting(mid))


def test_tools_hide_a_private_meeting_outside_its_channel(world):
    service, _, mid = world
    outside = MeetingTools(lambda: service, reader=lambda: OUTSIDE.reader)
    assert json.loads(outside.search({"query": "informe"}))["results"] == []
    assert json.loads(outside.get({"meeting_id": mid})) == {"error": f"no meeting '{mid}'; use meeting_search"}
    for reader in (INSIDE.reader, IN_ITS_THREAD.reader, Reader()):  # its channel, its thread, the CLI
        tools = MeetingTools(lambda: service, reader=lambda r=reader: r)
        assert json.loads(tools.search({"query": "informe"}))["results"][0]["meeting_id"] == mid
        assert json.loads(tools.get({"meeting_id": mid}))["meeting"]["id"] == mid


def test_other_platforms_never_read_a_private_meeting(world):
    service, _, mid = world
    tools = MeetingTools(lambda: service, reader=lambda: Reader("telegram", frozenset({PRIVATE_CHANNEL})))
    assert "error" in json.loads(tools.get({"meeting_id": mid}))


def test_commands_hide_a_private_meeting_outside_its_channel(world):
    _, cmds, mid = world
    for text in ("list 5", "status", "search informe"):
        assert mid not in cmds.handle(text, OUTSIDE, "meeting")
    assert "Yo envío el informe" not in cmds.handle("search informe", OUTSIDE, "meeting")
    assert "No meeting" in cmds.handle(f"show {mid}", OUTSIDE, "meeting")
    assert "No meeting" in cmds.handle(f"reprocess {mid} analyze", OUTSIDE, "meeting")
    assert mid in cmds.handle("list 5", INSIDE, "meeting")
    assert "Informe semanal" in cmds.handle(f"show {mid}", IN_ITS_THREAD, "meeting")
    assert "Yo envío el informe" in cmds.handle("search informe", INSIDE, "meeting")


def test_a_private_meeting_published_in_a_forum_post_is_readable_there(world):
    service, _, mid = world
    privacy.remember(service.repo, mid, "", "720")
    service.repo.upsert_delivery(mid, "discord", f"mtg:{mid}:notes",
                                 external_id=json.dumps({"channel": "7201", "forum": "720", "message": 1}), url=None)
    post = Reader("discord", frozenset({"7201", "720"}))
    tools = MeetingTools(lambda: service, reader=lambda: post)
    assert json.loads(tools.get({"meeting_id": mid}))["meeting"]["id"] == mid
