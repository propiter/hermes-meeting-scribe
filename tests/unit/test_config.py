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
    s = Settings.load(_getter({"autojoin.enabled": "false", "autojoin.min_humans": "3",
                               "projects.min_confidence": "0.75", "commands.aliases": "meet, nova-rec",
                               "owners": 123}))
    assert s.autojoin_enabled is False and s.autojoin_min_humans == 3
    assert s.projects_min_confidence == 0.75
    assert s.commands_aliases == ("meet", "nova-rec")
    assert s.owners == ("123",)


@pytest.mark.parametrize("key,value", [
    ("audio.retention", "flac"), ("kanban.mode", "sometimes"), ("autojoin.min_humans", "many"),
    ("projects.min_confidence", 3.0), ("audio.bitrate_kbps", 0), ("ui.language", "fr"),
])
def test_invalid_values_fall_back_with_warning(key, value):
    s = Settings.load(_getter({key: value}))
    assert getattr(s, key.replace(".", "_")) == SPEC[key].default
    assert any(key in w for w in s.warnings)


def test_aliases_are_normalized_and_primary_excluded():
    s = Settings.load(_getter({"commands.aliases": ["/Meet", "meeting", "rec", "bad name!"]}))
    assert s.commands_aliases == ("meet", "rec")


def test_cpu_threads_auto(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 16)
    assert Settings.load(_getter({})).effective_cpu_threads == 14
    assert Settings.load(_getter({"transcribe.cpu_threads": 4})).effective_cpu_threads == 4
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
        if spec.choices:
            assert schema[key]["choices"] == list(spec.choices), key


def test_validate_value_for_cli():
    from meeting_scribe.config import validate_value
    assert validate_value("kanban.mode", "auto") == "auto"
    assert validate_value("autojoin.enabled", "no") is False
    assert validate_value("owners", "1, 2") == ["1", "2"]  # YAML-friendly list, not tuple
    with pytest.raises(ValueError, match="kanban.mode"):
        validate_value("kanban.mode", "sometimes")
    with pytest.raises(KeyError):
        validate_value("nope", "x")
