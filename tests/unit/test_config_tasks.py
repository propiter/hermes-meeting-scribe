"""Settings added by the task-delivery redesign (DESIGN §16)."""
from __future__ import annotations

from meeting_scribe.config import Settings, settings_from_mapping


def test_new_task_delivery_defaults():
    s = Settings.defaults()
    assert s.delivery_dm_assignees is True          # DMs are on unless disabled
    assert s.delivery_tasks_placement == "meeting"  # everything in one place unless asked otherwise
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


# -- delivery_tasks_placement replaces delivery_project_threads (DESIGN §16.1) ----------------------------
import pytest  # noqa: E402

from meeting_scribe.config import retired_hint, validate_value  # noqa: E402


@pytest.mark.parametrize("old, placement", [(True, "projects"), ("false", "projects_inline")])
def test_a_stored_delivery_project_threads_keeps_the_per_project_layout(old, placement):
    s = settings_from_mapping({"delivery_project_threads": old})
    assert s.delivery_tasks_placement == placement
    assert any("delivery_project_threads is retired" in w for w in s.warnings)


def test_the_new_key_wins_over_the_retired_one():
    s = settings_from_mapping({"delivery_project_threads": True, "delivery_tasks_placement": "meeting"})
    assert s.delivery_tasks_placement == "meeting" and not s.warnings


def test_a_space_override_of_the_retired_key_is_read_too():
    s = settings_from_mapping({}, space="team", overrides={"delivery_project_threads": False})
    assert s.delivery_tasks_placement == "projects_inline"
    assert any(w.startswith("space team: delivery_project_threads") for w in s.warnings)


def test_an_unreadable_retired_value_falls_back_to_the_default_with_a_warning():
    s = settings_from_mapping({"delivery_project_threads": "sometimes"})
    assert s.delivery_tasks_placement == "meeting" and s.warnings


def test_placement_is_validated_and_the_retired_key_explains_its_replacement():
    assert validate_value("delivery_tasks_placement", "projects") == "projects"
    with pytest.raises(ValueError):
        validate_value("delivery_tasks_placement", "everywhere")
    with pytest.raises(KeyError):
        validate_value("delivery_project_threads", "true")
    assert "delivery_tasks_placement" in retired_hint("delivery_project_threads")
    assert retired_hint("kanban_mode") == ""
