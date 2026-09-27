from pathlib import Path

import pytest
import yaml

from meeting_scribe.config import SPEC, Settings, effective_owners

ROOT = Path(__file__).resolve().parents[2]


def _getter(values):
    return lambda key, default=None: values.get(key, default)


def test_defaults_match_design():
    s = Settings.load(_getter({}))
    assert s.commands_aliases == ("meet", "rec")
    assert s.autojoin_enabled is True and s.autojoin_min_humans == 2 and s.autojoin_grace_seconds == 20
    assert s.autoleave_grace_seconds == 60 and s.limits_max_duration_minutes == 240
    assert s.audio_retention == "multitrack" and s.audio_bitrate_kbps == 48
    assert s.transcribe_model == "medium" and s.transcribe_language == "auto" and s.transcribe_beam_size == 5
    assert s.analysis_chunk_chars == 12000 and s.projects_min_confidence == 0.6
    assert s.delivery_discord_enabled is True
    assert s.kanban_mode == "approve" and s.linear_mode == "approve"
    assert s.obsidian_vault_path == "" and s.consent_announce is True
    assert s.warnings == ()


def test_coercion_of_string_values():
    s = Settings.load(_getter({"autojoin_enabled": "false", "autojoin_min_humans": "3",
                               "projects_min_confidence": "0.75", "commands_aliases": "meet, notes-rec",
                               "owners": 123}))
    assert s.autojoin_enabled is False and s.autojoin_min_humans == 3
    assert s.projects_min_confidence == 0.75
    assert s.commands_aliases == ("meet", "notes-rec")
    assert s.owners == ("123",)


@pytest.mark.parametrize("key,value", [
    ("audio_retention", "flac"), ("kanban_mode", "sometimes"), ("autojoin_min_humans", "many"),
    ("projects_min_confidence", 3.0), ("audio_bitrate_kbps", 0), ("ui_language", "fr"),
])
def test_invalid_values_fall_back_with_warning(key, value):
    s = Settings.load(_getter({key: value}))
    assert getattr(s, key) == SPEC[key].default
    assert any(key in w for w in s.warnings)


def test_aliases_are_normalized_and_primary_excluded():
    s = Settings.load(_getter({"commands_aliases": ["/Meet", "meeting", "rec", "bad name!"]}))
    assert s.commands_aliases == ("meet", "rec")


def test_cpu_threads_auto(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 16)
    assert Settings.load(_getter({})).effective_cpu_threads == 14
    assert Settings.load(_getter({"transcribe_cpu_threads": 4})).effective_cpu_threads == 4
    monkeypatch.setattr("os.cpu_count", lambda: 1)
    assert Settings.load(_getter({})).effective_cpu_threads == 1


def test_effective_owners():
    s = Settings.load(_getter({}))
    assert effective_owners(s, lambda name: "111, 222" if name == "DISCORD_ALLOWED_USERS" else None) == ("111",)
    s2 = Settings.load(_getter({"owners": ["9"]}))
    assert effective_owners(s2, lambda name: "111") == ("9",)
    assert effective_owners(s, lambda name: None) == ()


def test_plugin_yaml_config_schema_in_sync_with_settings():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    schema = manifest["config_schema"]
    assert set(schema) == set(SPEC)
    for key, spec in SPEC.items():
        assert schema[key]["type"] == spec.yaml_type, key
        assert schema[key]["default"] == spec.yaml_default, key
        assert schema[key].get("description"), key
        assert schema[key]["group"] == spec.group, key
        assert schema[key]["label"] == spec.label(key), key
        assert schema[key].get("format", "") == spec.format, key
        assert schema[key].get("minimum") == spec.minimum and schema[key].get("maximum") == spec.maximum, key
        if spec.choices:
            assert schema[key]["choices"] == list(spec.choices), key


def test_validate_value_for_cli():
    from meeting_scribe.config import validate_value
    assert validate_value("kanban_mode", "auto") == "auto"
    assert validate_value("autojoin_enabled", "no") is False
    assert validate_value("owners", "1, 2") == ["1", "2"]  # YAML-friendly list, not tuple
    with pytest.raises(ValueError, match="kanban_mode"):
        validate_value("kanban_mode", "sometimes")
    with pytest.raises(KeyError):
        validate_value("nope", "x")


# -- review finding 10: flat keys (Hermes' Desktop form reads settings[key] flat) --------------
def test_config_schema_keys_are_flat():
    assert all("." not in key for key in SPEC)


def test_legacy_nested_values_are_still_read():
    """Configs saved by 0.1 (``ctx.set_config("kanban.mode")`` -> nested YAML) keep working."""
    s = Settings.load(_getter({"kanban.mode": "auto", "delivery.discord.channel": "123"}))
    assert s.kanban_mode == "auto" and s.delivery_discord_channel == "123"


def test_flat_value_wins_over_legacy():
    s = Settings.load(_getter({"kanban_mode": "off", "kanban.mode": "auto"}))
    assert s.kanban_mode == "off"


def test_canonical_key_accepts_legacy_spelling():
    from meeting_scribe.config import canonical_key, validate_value

    assert canonical_key("kanban.mode") == "kanban_mode" == canonical_key("kanban_mode")
    assert validate_value("kanban.mode", "auto") == "auto"
    with pytest.raises(KeyError):
        canonical_key("nope")


def test_google_meet_and_transcript_settings_defaults_and_bounds():
    """DESIGN §17: Meet import is opt-in; the transcript attachment is on by default."""
    s = Settings.defaults()
    assert s.google_meet_enabled is False and s.google_meet_poll_minutes == 5
    assert s.google_meet_discord_channel == "" and s.delivery_discord_transcript is True
    from meeting_scribe.config import settings_from_mapping
    low = settings_from_mapping({"google_meet_poll_minutes": 0})
    assert low.google_meet_poll_minutes == 5 and low.warnings  # below the minimum: default + warning
