"""Direct-messages-only meetings (a ``:dm`` rule, DESIGN §19.3): nothing in any channel, a full copy in
each participant's DM, idempotent, fail closed. Invented names only: the voice channel ``Leadership``
(200), public channels ``#orion`` (501), ``#nebula`` (502), ``#general-tasks`` (600); participants Ana
(10, DMs closed unless a test opens them) and Luis (11).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from meeting_scribe import privacy
from meeting_scribe.config import settings_from_mapping
from meeting_scribe.discord_ui.actions import ButtonActions
from meeting_scribe.discord_ui.auth import check_dm
from meeting_scribe.discord_ui.render import RenderOptions
from meeting_scribe.discord_ui.sink import DiscordNotesSink
from meeting_scribe.domain.models import ActionItem, Speaker, Utterance
from meeting_scribe.routes import load_routes, parse_route
from meeting_scribe.storage.artifacts import write_notes, write_transcript

from .fakes import FakeAdapter, FakeBot, FakeInteraction
from .test_task_sink import Svc, Views

ITEMS = (
    ActionItem(id="a1", title="Landing page", owner_speaker_id="11", owner_name="Luis", project="orion",
               project_key="discord:501", project_confidence=0.9),
    ActionItem(id="a2", title="Contract review", owner_speaker_id="10", owner_name="Ana", project="nebula",
               project_key="discord:502", project_confidence=0.9),
    ActionItem(id="a3", title="Budget", owner_speaker_id=None),
)
SUMMARY_WORD = "Usar SES"  # a decision of the ``notes`` fixture


class FileViews(Views):
    def file(self, name, data):
        return SimpleNamespace(filename=name, data=data)


@pytest.fixture
def env(tmp_path, meeting, notes):
    svc = Svc(tmp_path)
    m = replace(meeting, text_channel_id=None, channel_id="200", channel_name="Leadership", category_id="900",
                category_name="Board")
    n = replace(notes, action_items=ITEMS)
    svc.repo.save_meeting(m)
    write_notes(svc.folder(m), m, n, "es")
    write_transcript(svc.folder(m), [Utterance(0.0, 2.0, "11", "Luis", "Texto interno de la reunión.")])
    svc.repo.sync_action_items(m.id, n.action_items)
    bot = FakeBot()
    chat = bot.add(200, "Leadership")
    for cid, name in ((501, "orion"), (502, "nebula"), (600, "general-tasks")):
        bot.add(cid, name)
    luis, ana = bot.user(11), bot.user(10, dms_open=False)
    adapter = FakeAdapter(bot)
    cfg: dict = {"meeting_routes": ["Leadership = :dm"], "delivery_fallback_channel": "600"}
    state = {"loop": None}

    def make():
        s = settings_from_mapping(cfg)
        sink = DiscordNotesSink(
            settings=lambda space=None: s, service=lambda: svc, adapter=lambda: adapter, loop=lambda: state["loop"],
            options=lambda mm: RenderOptions(lang="en", kanban_on=True, linear_on=False,
                                             is_owner_item=lambda i: i.owner_speaker_id == "11"),
            views=FileViews(), timeout=5)
        return sink
    yield SimpleNamespace(svc=svc, meeting=m, notes=n, bot=bot, chat=chat, luis=luis, ana=ana, cfg=cfg,
                          state=state, make=make)
    svc.repo.close()


async def deliver(env):
    env.state["loop"] = asyncio.get_running_loop()
    return await asyncio.to_thread(env.make().deliver, env.meeting, env.notes, env.svc.folder(env.meeting))


def in_channels(env):
    return [m for c in env.bot.channels.values() if not c.is_dm for m in c.ordered()]


def texts(msgs):
    return "\n".join(m.content for m in msgs)


# -- rule syntax ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("entry", ["Leadership = :dm", "Leadership=dm", "Leadership = :directo", "Leadership = mensajes",
                                   "category:Board = :DM", "meet:weekly-* = dm"])
def test_dm_rule_forms_parse_to_the_dm_mode_without_a_channel(entry):
    rule = parse_route(entry)
    assert rule.dm and rule.private and rule.channel == "" and rule.mode == "dm"
    assert rule.text.endswith("=:dm")


@pytest.mark.parametrize("entry", ["Leadership = #x:dm", "Leadership = 700:dm", "Leadership = :dm:private",
                                   "Leadership = dm:private"])
def test_dm_with_a_channel_or_another_option_is_refused(entry):
    with pytest.raises(ValueError, match="takes no channel"):
        parse_route(entry)


def test_a_channel_called_dm_is_written_with_a_hash():
    rule = parse_route("Leadership = #dm")
    assert not rule.dm and rule.channel == "dm"
    assert parse_route("Leadership = dm-notes").channel == "dm-notes"


def test_unreadable_entry_mentioning_dm_fails_closed_for_every_meeting():
    rules, warnings = load_routes(["Leadership :dm"])
    assert len(rules) == 1 and rules[0].kind == "any" and rules[0].private and rules[0].error
    assert warnings


# -- delivery ------------------------------------------------------------------------------------------
async def test_dm_meeting_is_never_sent_to_any_channel_and_each_reachable_participant_gets_it_all(env):
    res = await deliver(env)
    assert res.ok, res.errors
    assert in_channels(env) == []
    body = texts(env.luis.dm.ordered())
    assert SUMMARY_WORD in body and "Landing page" in body and "Contract review" in body  # index lists all
    files = [m.file.filename for m in env.luis.dm.ordered() if getattr(m, "file", None)]
    assert files and files[0].endswith(".md")
    own = [m for m in env.luis.dm.ordered() if m.content.startswith(("⏳", "🕓", "📌")) or "Landing page**" in m.content]
    assert own and all("Contract review**" not in m.content for m in own)  # only HIS task has its own message
    rec = privacy.record(env.svc.repo, env.meeting.id)
    assert rec["mode"] == "dm" and rec["recipients"] == ["10", "11"]
    assert env.ana.dm.ordered() == []
    note = env.svc.repo.kv_get(privacy.DM_UNREACHABLE_KV + env.meeting.id)
    assert "10" in note


async def test_own_task_message_has_its_buttons_and_no_send_to_someone(env):
    await deliver(env)
    task = next(m for m in env.luis.dm.ordered() if "**Landing page**" in m.content)
    ids = task.view or ()
    assert any(":ok:" in c for c in ids) and any(":no:" in c for c in ids)
    assert any(":shp:" in c for c in ids) and not any(":shd:" in c for c in ids)
    index = next(m for m in env.luis.dm.ordered() if "📋" in m.content)
    assert not index.view  # nothing acts from the index


async def test_reprocess_edits_the_existing_direct_messages(env):
    await deliver(env)
    first = [(m.id, m.content) for m in env.luis.dm.ordered()]
    await deliver(env)
    await env.make().refresh(env.meeting.id)
    assert [m.id for m in env.luis.dm.ordered()] == [i for i, _ in first]
    assert in_channels(env) == []


async def test_a_task_given_to_someone_else_leaves_the_old_assignee_dm(env):
    await deliver(env)
    env.svc.repo.sync_action_items(env.meeting.id, (replace(ITEMS[0], owner_speaker_id="10", owner_name="Ana"),
                                                    *ITEMS[1:]))
    await deliver(env)
    assert not any("**Landing page**" in m.content for m in env.luis.dm.ordered())


async def test_everyone_with_closed_dms_leaves_the_delivery_waiting_never_in_a_channel(env):
    env.luis.dms_open = False
    res = await deliver(env)
    assert not res.ok and res.waiting and "direct message" in res.errors[0]
    assert in_channels(env) == []


async def test_no_discord_participant_waits_with_the_reason(env):
    env.svc.repo.save_meeting(replace(env.meeting, speakers=(Speaker("gmeet:users/1", "Marta Ruiz"),)))
    env.meeting = env.svc.repo.get_meeting(env.meeting.id)
    res = await deliver(env)
    assert not res.ok and res.waiting and "/meeting link" in res.errors[0]
    assert in_channels(env) == [] and privacy.record(env.svc.repo, env.meeting.id)["recipients"] == []


async def test_meet_participant_mapped_by_a_person_link_gets_the_copy(env):
    env.svc.repo.save_meeting(replace(env.meeting, speakers=(Speaker("gmeet:users/1", "Luis  Pérez"),)))
    env.meeting = env.svc.repo.get_meeting(env.meeting.id)
    env.svc.repo.set_link(env.meeting.space, "11", name="luis pérez")
    res = await deliver(env)
    assert res.ok and SUMMARY_WORD in texts(env.luis.dm.ordered()) and in_channels(env) == []


async def test_rule_added_later_withdraws_the_public_copies(env):
    env.cfg["meeting_routes"] = []
    res = await deliver(env)
    assert res.ok and SUMMARY_WORD in texts(in_channels(env))
    env.cfg["meeting_routes"] = ["Leadership = :dm"]
    res = await deliver(env)
    assert res.ok
    assert SUMMARY_WORD not in texts(in_channels(env)) and "Landing page" not in texts(in_channels(env))
    assert SUMMARY_WORD in texts(env.luis.dm.ordered())


async def test_the_anchor_keeps_the_meeting_in_dms_after_the_rule_goes(env):
    await deliver(env)
    env.cfg["meeting_routes"] = []
    res = await deliver(env)
    assert res.ok and in_channels(env) == []
    env.ana.dms_open = True  # not a participant it was anchored with? she was: now she gets it
    await deliver(env)
    assert SUMMARY_WORD in texts(env.ana.dm.ordered())


async def test_a_meeting_anchored_to_a_private_channel_is_not_turned_into_dms(env):
    privacy.anchor(env.svc.repo, env.meeting.id, "700")
    assert not privacy.is_dm(env.svc.repo, settings_from_mapping(env.cfg), env.meeting)
    with pytest.raises(ValueError):
        privacy.anchor_dm(env.svc.repo, env.meeting.id, "Leadership", ["11"])
    res = await deliver(env)
    assert not res.ok and res.waiting and env.luis.dm.ordered() == []


# -- readers (fail closed) -----------------------------------------------------------------------------
async def test_readers_see_it_only_from_the_recipients_dms(env):
    await deliver(env)
    s = settings_from_mapping(env.cfg)
    repo = env.svc.repo
    assert privacy.allowed_places(repo, env.meeting, s) == {str(env.luis.dm.id)}
    assert privacy.Reader("discord", frozenset({str(env.luis.dm.id)})).may_read(repo, s, env.meeting)
    assert not privacy.Reader("discord", frozenset({"200"})).may_read(repo, s, env.meeting)
    assert not privacy.Reader(cron=True).may_read(repo, s, env.meeting)
    assert not privacy.Reader("telegram", frozenset({str(env.luis.dm.id)})).may_read(repo, s, env.meeting)
    assert privacy.Reader.operator().may_read(repo, s, env.meeting)


def test_before_delivery_a_dm_meeting_is_readable_from_nowhere(env):
    s = settings_from_mapping(env.cfg)
    assert privacy.is_private(env.svc.repo, s, env.meeting)
    assert privacy.allowed_places(env.svc.repo, env.meeting, s) == set()
    assert not privacy.Reader("discord", frozenset({"200"})).may_read(env.svc.repo, s, env.meeting)


# -- buttons -------------------------------------------------------------------------------------------
def _click(uid, channel_id, *, guild=False):
    i = FakeInteraction(uid, dm=not guild)
    i.channel = SimpleNamespace(id=channel_id)
    return i


@pytest.mark.parametrize("uid,channel,action,item,ok", [
    (11, 91011, "no", "11", True),       # his copy, his task
    (11, 91011, "shp", "11", True),      # share HIS task to its project channel
    (11, 91011, "no", "10", False),      # someone else's task
    (10, 91011, "no", "10", False),      # someone else's copy
    (11, 555, "no", "11", False),        # not his DM
    (11, 91011, "sha", None, False),     # meeting-wide buttons: refused
    (11, 91011, "shd", "11", False),     # sending a task to someone: not in DM meetings
    (11, 91011, "mine", None, True),
])
def test_check_dm(uid, channel, action, item, ok):
    it = ActionItem(id="a1", title="x", owner_speaker_id=item) if item else None
    verdict = check_dm(_click(uid, channel), {"11": "91011", "10": "91010"}, action, it, "en")
    assert verdict.allowed is ok


def test_check_dm_refuses_a_click_from_a_server_channel():
    it = ActionItem(id="a1", title="x", owner_speaker_id="11")
    assert not check_dm(_click(11, 91011, guild=True), {"11": "91011"}, "no", it, "en").allowed


async def test_owner_cannot_act_on_another_participants_task_in_a_dm_meeting(env):
    await deliver(env)
    sink = env.make()
    calls = []
    svc = SimpleNamespace(repo=env.svc.repo, dismiss_item=lambda *a: calls.append(a))
    acts = ButtonActions(service=lambda: svc, settings=lambda space=None: settings_from_mapping(env.cfg),
                         owners=lambda space=None: ("11",), check_auth=lambda i: True, sink=lambda: sink,
                         project_view=lambda *a: None, move_view=lambda *a: None)
    i = _click(11, env.luis.dm.id)
    await acts.handle(i, "no", env.meeting.id, "a2")  # Ana's task, pressed by an owner
    assert calls == [] and i.response.sent
    i = _click(11, env.luis.dm.id)
    await acts.handle(i, "no", env.meeting.id, "a1")
    assert calls == [(env.meeting.id, "a1")]


async def test_share_own_task_to_its_project_channel_posts_the_task_only(env):
    await deliver(env)
    sink = env.make()
    done = await sink.share(env.meeting.id, "a1", "project")
    assert done == "501"
    posted = texts(env.bot.channels[501].ordered())
    assert "Landing page" in posted and SUMMARY_WORD not in posted
    assert texts(env.bot.channels[200].ordered()) == ""


def test_participants_skips_names_that_match_two_links(env):
    env.svc.repo.set_link(env.meeting.space, "11", name="Sam")
    env.svc.repo.set_link(env.meeting.space, "12", name="sam")
    m = replace(env.meeting, speakers=(Speaker("gmeet:users/9", "Sam"), Speaker("11", "Luis")))
    found, unmapped = privacy.participants(env.svc.repo, m)
    assert found == ["11"] and unmapped == ["Sam"]


def test_dm_record_is_json(env):
    privacy.anchor_dm(env.svc.repo, env.meeting.id, "Leadership", ["11", "11", "10"])
    raw = json.loads(env.svc.repo.kv_get(privacy.KV_PRIVATE + env.meeting.id))
    assert raw == {"rule": "Leadership", "channel": "", "mode": "dm", "recipients": ["11", "10"]}


def test_a_dm_rule_is_private_for_every_other_sink_before_any_delivery(env):
    """Kanban/Linear ``auto`` becomes ``approve`` and nothing is learned: the ItemSink gate is is_private."""
    s = settings_from_mapping(env.cfg)
    assert privacy.record(env.svc.repo, env.meeting.id) is None
    assert privacy.is_private(env.svc.repo, s, env.meeting) and privacy.is_dm(env.svc.repo, s, env.meeting)
