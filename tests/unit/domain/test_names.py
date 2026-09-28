"""Generic matching of spoken project names against Discord channel/category names (DESIGN §16).

Nothing here knows any server's naming: decoration is removed by Unicode category only, word
prefixes are configuration (``channel_name_ignore_prefixes``), and fixtures use invented names.
"""
from __future__ import annotations

import pytest

from meeting_scribe.domain.names import (
    clean_channel_name,
    levenshtein_ratio,
    match_name,
    rank_names,
    similarity,
)


@pytest.mark.parametrize("raw, clean", [
    ("orion", "orion"),
    ("『🚀』orion", "orion"),
    ("🟢┃nebula", "nebula"),
    ("【Atlas】", "Atlas"),
    ("「Atlas」・", "Atlas"),
    ("[orion] | ", "orion"),
    ("• Marketing Team •", "Marketing Team"),
    ("👩‍💻 nebula-app ✨", "nebula-app"),     # ZWJ sequence + trailing emoji
    ("1️⃣ orion", "orion"),                  # keycap sequence
    ("Nebula Ops - Research", "Nebula Ops - Research"),
    ("x-orion", "x-orion"),                 # letters are never stripped by code
    ("café", "café"),
    ("🎉", ""),
])
def test_clean_channel_name_is_category_based_and_keeps_letters_and_case(raw, clean):
    assert clean_channel_name(raw) == clean


def test_word_prefixes_are_configuration_not_code():
    assert clean_channel_name("team-orion") == "team-orion"
    assert clean_channel_name("team-orion", ignore_prefixes=("team",)) == "orion"
    assert clean_channel_name("🟢┃TEAM_orion", ignore_prefixes=("team",)) == "orion"
    assert clean_channel_name("teamwork", ignore_prefixes=("team",)) == "teamwork"  # whole word only


def test_levenshtein_ratio_bounds():
    assert levenshtein_ratio("abc", "abc") == 1.0
    assert levenshtein_ratio("", "abc") == 0.0
    assert levenshtein_ratio("orion", "orions") == pytest.approx(1 - 1 / 6)


@pytest.mark.parametrize("spoken, channel", [
    ("Nebulla", "nebula"), ("Orayon", "『🚀』orion"), ("ATLAS", "atlas-dev"), ("atlas", "【Atlas】"),
    ("Marketing Team", "marketing-team-general"), ("proyecto Orion", "orion"), ("Nébula", "🟢┃nebula"),
])
def test_transcription_errors_and_decorated_names_still_match(spoken, channel):
    assert similarity(spoken, channel) >= 0.8


@pytest.mark.parametrize("spoken, channel", [
    ("app", "nebula-app"),          # short common token must not match everything
    ("dev", "atlas-dev"),
    ("Orion", "general"), ("Atlas", "nebula"), ("", "orion"), ("Orion", "🎉"),
])
def test_short_tokens_and_unrelated_names_stay_below_threshold(spoken, channel):
    assert similarity(spoken, channel) < 0.8


def test_exact_channel_beats_a_channel_that_only_contains_the_name():
    ranked = rank_names("Atlas", ["atlas-dev", "general", "atlas"], 0.8)
    assert [n for n, _ in ranked] == ["atlas", "atlas-dev"]


def test_match_name_flags_close_candidates_as_uncertain():
    sure = match_name("Orion", ["orion", "nebula"], 0.8)
    assert sure is not None and sure.name == "orion" and not sure.uncertain
    close = match_name("Nebula", ["nebula-app", "nebula-web"], 0.8)
    assert close is not None and close.uncertain
    assert match_name("Zephyr", ["orion", "nebula"], 0.8) is None


def test_ignore_prefixes_reach_the_scorer():
    assert similarity("orion", "squad-orion-x", ignore_prefixes=("squad",)) >= 0.8


@pytest.mark.parametrize("spoken, channel", [("dev", "web"), ("api", "app"), ("ux", "ui"), ("ops", "orion")])
def test_short_spoken_names_only_match_exactly(spoken, channel):
    """A 2-3 letter name is too ambiguous for fuzzy matching, even as a weak guess (review 9)."""
    assert similarity(spoken, channel) < 0.5
    assert similarity(spoken, spoken.upper()) == 1.0
