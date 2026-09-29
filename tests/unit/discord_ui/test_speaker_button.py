"""The "Who is <unidentified participant>?" button in Discord (DESIGN §4.1): who may press it, the
picker it offers and the assignment it runs (the same service call as the CLI and Desktop)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from meeting_scribe.config import settings_from_mapping
from meeting_scribe.discord_ui.actions import ButtonActions
from meeting_scribe.discord_ui.render import render_header, speaker_buttons
from meeting_scribe.domain.models import Notes, Speaker
from meeting_scribe.pipeline.speakers import AssignError, Assigned, Track

from .fakes import FakeInteraction
from .test_actions import OWNER, Sink, in_channel

ANA, LUIS, STRANGER = 10, 12, 99
MID, LABEL = "k3v7q2ab", "unidentified-1"


class Svc:
    def __init__(self):
        self.calls = []
        self.auth = []
        self.owner = None
        self.repo = SimpleNamespace(get_action_item=lambda mid, iid: None)
        self.meeting = SimpleNamespace(id=MID, speakers=(Speaker("10", "Ana"), Speaker("12", "Luis"),
                                                         Speaker(LABEL, "Unidentified participant"),
                                                         Speaker("77", "Bot", True)))

    def require(self, mid):
        return SimpleNamespace(**vars(self.meeting), human_speakers=tuple(s for s in self.meeting.speakers
                                                                          if not s.is_bot))

    def speaker_tracks(self, meeting):
        return [Track(LABEL, "Unidentified participant", 138, 2.5, 1250.0, self.owner)]

    def assign_speaker(self, mid, label, who, **auth):
        self.calls.append((mid, label, who))
        self.auth.append(auth)
        if who == "77":
            raise AssignError("unknown_person", who)
        return Assigned(label, who, "Luis", 138, 3, True, True)


@pytest.fixture
def env():
    svc, sink = Svc(), Sink()
    acts = ButtonActions(service=lambda: svc, settings=lambda space=None: settings_from_mapping({}),
                         owners=lambda space=None: (str(OWNER),), check_auth=lambda i: True, sink=lambda: sink,
                         project_view=lambda *a: None, move_view=lambda *a: None, buttons_view=lambda specs: list(specs),
                         speaker_view=lambda mid, label, opts: ("speakers", mid, label, tuple(opts)))
    return SimpleNamespace(svc=svc, sink=sink, acts=acts)


def test_the_notes_header_offers_one_button_per_open_track(meeting):
    m = meeting.__class__(**{**vars(meeting), "speakers": (*meeting.speakers, Speaker(LABEL, "Participante sin identificar"),
                                                           Speaker("unidentified-2", "Participante sin identificar 2"))})
    [b1, b2] = speaker_buttons(m, "es")
    assert b1.custom_id == f"mscribe:spk:{m.id}:{LABEL}" and b1.label == "¿Quién es Participante sin identificar?"
    specs = render_header(m, Notes(meeting_title="x", tldr="t", summary="s"), "es")
    assert specs[0].buttons == (b1, b2) and specs[0].mentions == ()  # a button pings nobody
    assert speaker_buttons(meeting, "es") == ()


async def test_an_owner_gets_the_picker_without_bots_or_tracks(env):
    i = FakeInteraction(OWNER)
    await env.acts.handle(i, "spk", MID, LABEL)
    view = i.followup.sent[0]["view"]
    assert view == ("speakers", MID, LABEL, (("unassigned", "Unidentified participant"), ("10", "Ana"), ("12", "Luis")))
    assert "138 line(s), 00:02–20:50" in i.replies() and i.followup.sent[0]["ephemeral"]


async def test_a_participant_is_only_offered_that_s_me(env):
    i = FakeInteraction(ANA)
    await env.acts.handle(i, "spk", MID, LABEL)
    [button] = i.followup.sent[0]["view"]
    assert button.custom_id == f"mscribe:sme:{MID}:{LABEL}" and button.label == "That's me"
    assert "ask an owner" in i.replies() and i.followup.sent[0]["ephemeral"]
    es = FakeInteraction(ANA)
    env.acts._settings = lambda space=None: settings_from_mapping({"ui_language": "es"})
    await env.acts.handle(es, "spk", MID, LABEL)
    assert es.followup.sent[0]["view"][0].label == "Soy yo"


async def test_that_s_me_assigns_the_clicker_whatever_the_values(env):
    i = FakeInteraction(ANA, values=["12"])  # a forged value is ignored
    await env.acts.handle(i, "sme", MID, LABEL)
    assert env.svc.calls == [(MID, LABEL, "10")] and env.svc.auth[-1] == {"actor": "10", "admin": False}


async def test_a_participant_confirming_someone_else_s_suggestion_gets_that_s_me_instead(env):
    env.svc.meeting.speakers = (*env.svc.meeting.speakers[:2],
                                Speaker(LABEL, "Unidentified participant", suggested_user="12"),
                                env.svc.meeting.speakers[3])
    i = FakeInteraction(ANA)
    await env.acts.handle(i, "scfm", MID, LABEL)
    assert env.svc.calls == [] and i.followup.sent[0]["view"][0].custom_id.startswith("mscribe:sme:")
    luis = FakeInteraction(LUIS)
    await env.acts.handle(luis, "scfm", MID, LABEL)
    assert env.svc.calls == [(MID, LABEL, "12")]


async def test_someone_who_was_not_there_cannot_name_a_voice(env):
    i = FakeInteraction(STRANGER)
    await env.acts.handle(i, "spk", MID, LABEL)
    await env.acts.handle(FakeInteraction(STRANGER, values=["12"]), "ssel", MID, LABEL)
    assert env.svc.calls == [] and "Only someone who was in the meeting" in i.replies()


async def test_picking_runs_the_assignment_and_reports_it(env):
    i = FakeInteraction(OWNER, values=["12"])
    await env.acts.handle(i, "ssel", MID, LABEL)
    assert env.svc.calls == [(MID, LABEL, "12")] and env.svc.auth[-1]["admin"] is True
    assert "unidentified-1 is now Luis: 138 transcript line(s) and 3 task(s) moved." in i.replies()
    assert "updated in place" in i.replies()
    bad = FakeInteraction(OWNER, values=["77"])
    await env.acts.handle(bad, "ssel", MID, LABEL)
    assert "is not one of this meeting's participants" in bad.replies()


@pytest.mark.parametrize("action", ["spk", "ssel"])
async def test_hermes_denial_blocks_participants_but_not_owners(env, action):
    env.acts._check_auth = lambda i: False
    denied = FakeInteraction(ANA, values=["12"])
    await env.acts.handle(denied, action, MID, LABEL)
    assert not env.svc.calls and not denied.followup.sent
    owner = FakeInteraction(OWNER, values=["12"])
    await env.acts.handle(owner, action, MID, LABEL)
    assert owner.followup.sent


async def test_an_assigned_track_offers_no_picker(env):
    env.svc.owner = "12"
    i = FakeInteraction(ANA)
    await env.acts.handle(i, "spk", MID, LABEL)
    assert "already assigned" in i.replies() and "view" not in i.followup.sent[0]


async def test_private_and_dm_meetings_keep_their_gates(env):
    env.sink.place = {"700"}
    outside = in_channel(ANA, 555)
    await env.acts.handle(outside, "spk", MID, LABEL)
    assert "use the buttons in its own channel" in outside.replies()
    inside = in_channel(ANA, 700)
    await env.acts.handle(inside, "ssel", MID, LABEL, ["12"])
    assert env.svc.calls == [(MID, LABEL, "12")]
    env.sink.place, env.sink.dm = None, {"10": "900"}
    other = FakeInteraction(ANA, dm=True)
    other.channel, other.channel_id = SimpleNamespace(id=901, parent_id=None), 901
    await env.acts.handle(other, "spk", MID, LABEL)
    assert "use the buttons in your own copy" in other.replies()
    own = FakeInteraction(ANA, dm=True)
    own.channel, own.channel_id = SimpleNamespace(id=900, parent_id=None), 900
    await env.acts.handle(own, "spk", MID, LABEL)
    assert own.followup.sent[0]["view"][0].custom_id.startswith("mscribe:sme:")
