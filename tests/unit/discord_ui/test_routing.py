"""Per-task channel routing (DESIGN §16). Neutral, invented channel names only."""
from __future__ import annotations

from dataclasses import replace

import pytest

from meeting_scribe.discord_ui.routing import ChannelInfo, RouteContext, route_item
from meeting_scribe.domain.models import ActionItem

CHANNELS = [
    ChannelInfo("900", "Product", kind="category"),
    ChannelInfo("501", "『🚀』orion", category_id="900", position=1),
    ChannelInfo("502", "🟢┃nebula", category_id="900", position=2),
    ChannelInfo("503", "atlas-dev", position=3),
    ChannelInfo("504", "general", position=0),
    ChannelInfo("910", "Marketing Team", kind="category"),
    ChannelInfo("511", "announcements", category_id="910", position=5, can_post=False),
    ChannelInfo("512", "campaigns", category_id="910", position=6),
]


def ctx(**kw):
    base = dict(channel_map={}, min_score=0.8, ignore_prefixes=(), learned=lambda name: None)
    base.update(kw)
    return RouteContext(**base)


def item(**kw):
    return ActionItem(id="a1", title="Do it", **kw)


def test_confident_project_goes_to_its_channel(meeting):
    r = route_item(item(project="orion", project_confidence=0.9), meeting, CHANNELS, ctx())
    assert (r.channel_id, r.uncertain, r.reason) == ("501", False, "fuzzy")


@pytest.mark.parametrize("hint, channel", [("Nebulla", "502"), ("Orayon", "501"), ("ATLAS", "503")])
def test_misspelled_spoken_names_route_by_fuzzy_match(meeting, hint, channel):
    assert route_item(item(project_hint=hint), meeting, CHANNELS, ctx()).channel_id == channel


def test_discord_candidate_key_from_the_llm_is_used_directly(meeting):
    r = route_item(item(project="nebula", project_key="discord:502"), meeting, CHANNELS, ctx())
    assert (r.channel_id, r.reason, r.uncertain) == ("502", "candidate", False)


def test_explicit_config_beats_learned_beats_fuzzy(meeting):
    it = item(project="orion", project_confidence=0.9)
    learned = ctx(learned=lambda name: "503" if name == "orion" else None)
    assert route_item(it, meeting, CHANNELS, learned).reason == "learned"
    assert route_item(it, meeting, CHANNELS, learned).channel_id == "503"
    both = ctx(learned=learned.learned, channel_map={"orion": "504"})
    assert (route_item(it, meeting, CHANNELS, both).channel_id,
            route_item(it, meeting, CHANNELS, both).reason) == ("504", "config")


def test_category_match_uses_its_first_postable_text_channel(meeting):
    r = route_item(item(project="Marketing Team", project_confidence=0.9), meeting, CHANNELS, ctx())
    assert r.channel_id == "512" and r.project == "Marketing Team"


def test_unclear_project_goes_to_most_probable_channel_with_warning(meeting):
    close = [ChannelInfo("601", "nebula-app"), ChannelInfo("602", "nebula-web")]
    r = route_item(item(project_hint="Nebula"), meeting, close, ctx())
    assert r.channel_id in {"601", "602"} and r.uncertain


def test_weak_match_below_threshold_still_posts_but_flags_it(meeting):
    r = route_item(item(project_hint="Orionne Labs"), meeting, CHANNELS, ctx())
    assert r.channel_id == "501" and r.uncertain


def test_meeting_project_is_only_a_flagged_guess(meeting):
    r = route_item(item(), replace(meeting, project="orion"), CHANNELS, ctx())
    assert r.channel_id == "501" and r.uncertain


def test_no_candidate_falls_back_to_meeting_chat(meeting):
    for it in (item(), item(project_hint="Zephyr"), item(project_hint="app")):
        r = route_item(it, meeting, CHANNELS, ctx())
        assert r.channel_id is None and r.reason == "none"


def test_missing_permission_falls_back_and_remembers_the_wanted_channel(meeting):
    chans = [replace(c, can_post=False) if c.id == "502" else c for c in CHANNELS]
    r = route_item(item(project="nebula", project_confidence=0.9), meeting, chans, ctx())
    assert r.channel_id is None and r.reason == "no_permission" and r.wanted_channel_id == "502"


def test_config_map_to_unknown_channel_is_ignored(meeting):
    r = route_item(item(project="orion", project_confidence=0.9), meeting, CHANNELS, ctx(channel_map={"orion": "999"}))
    assert r.channel_id == "501"


def test_short_name_is_never_a_weak_guess(meeting):
    chans = [ChannelInfo("601", "web"), ChannelInfo("602", "api")]
    assert route_item(item(project_hint="dev"), meeting, chans, ctx()).channel_id is None
