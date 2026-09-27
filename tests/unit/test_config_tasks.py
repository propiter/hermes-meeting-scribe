"""Settings added by the task-delivery redesign (DESIGN §16)."""
from __future__ import annotations

from meeting_scribe.config import Settings, settings_from_mapping


def test_new_task_delivery_defaults():
    s = Settings.defaults()
    assert s.delivery_dm_assignees is True          # DMs are on unless disabled
    assert s.delivery_project_threads is True
    assert s.project_channels == ()
    assert s.project_match_min_score == 0.8
    assert s.channel_name_ignore_prefixes == ()


def test_project_channels_parses_name_to_channel_pairs():
    s = settings_from_mapping({"project_channels": ["Orion = 501", "Nebula App=502", "broken", "=9"]})
    assert s.project_channel_map() == {"orion": "501", "nebula app": "502"}


def test_min_score_is_bounded():
    s = settings_from_mapping({"project_match_min_score": 3})
    assert s.project_match_min_score == 0.8 and s.warnings


def test_project_channels_are_validated_on_write_and_normalized():
    import pytest
    from meeting_scribe.config import validate_value

    assert validate_value("project_channels", ["Proyecto Alfa = 111", "Beta=<#222>"]) == ["Proyecto Alfa=111",
                                                                                         "Beta=222"]
    assert validate_value("project_channels", []) == []
    for bad in (["no separator"], ["=111"], ["Alfa=general"], ["Alfa=<@123>"], ["x" * 101 + "=1"]):
        with pytest.raises(ValueError):
            validate_value("project_channels", bad)
    # Loading an old config stays lenient: a malformed row is ignored, not the whole list.
    s = settings_from_mapping({"project_channels": ["Alfa=111", "broken"]})
    assert s.project_channel_map() == {"alfa": "111"} and not s.warnings
