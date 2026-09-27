"""Config schema for UIs (group, label/help i18n, channel references by id or name)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from meeting_scribe.config import (
    GROUPS, SPEC, SCHEMA_VERSION, channel_ref, config_schema, settings_from_mapping, validate_value,
)

ROOT = Path(__file__).resolve().parents[2]


def _catalog(lang):
    return json.loads((ROOT / "meeting_scribe" / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))


def test_every_option_has_a_known_group():
    assert all(opt.group in GROUPS for opt in SPEC.values())
    assert "llm" in GROUPS  # virtual group, read from Hermes' auxiliary config


@pytest.mark.parametrize("lang", ["en", "es"])
def test_every_option_and_group_has_label_and_help_in_i18n(lang):
    cat = _catalog(lang)
    for key in SPEC:
        assert cat.get(f"cfg.{key}.label"), (lang, key)
        assert cat.get(f"cfg.{key}.help"), (lang, key)
    for group in GROUPS:
        assert cat.get(f"cfg.group.{group}"), (lang, group)


def test_new_delivery_and_analysis_defaults():
    s = settings_from_mapping({})
    assert s.delivery_discord_guild == "" and s.delivery_fallback_channel == ""
    assert s.delivery_auto_channel_names == ("general", "meetings", "meeting-notes", "notes", "reuniones", "notas")
    assert s.analysis_timeout_seconds == 600 and s.analysis_max_tokens == 8192
    assert s.warnings == ()


@pytest.mark.parametrize("raw,expected", [
    ("123456789012345678", "123456789012345678"), ("<#123456789012345678>", "123456789012345678"),
    ("#meeting-notes", "meeting-notes"), ("meeting-notes", "meeting-notes"), ("  #Team Notes ", "Team Notes"),
    ("", ""),
])
def test_channel_settings_accept_id_mention_or_name(raw, expected):
    for key in ("delivery_discord_channel", "google_meet_discord_channel", "delivery_fallback_channel"):
        assert validate_value(key, raw) == expected


@pytest.mark.parametrize("raw", ["<@123>", "<@&5>", "#", "x" * 101, "two\nlines"])
def test_channel_settings_reject_non_channels(raw):
    with pytest.raises(ValueError, match="delivery_discord_channel"):
        validate_value("delivery_discord_channel", raw)


def test_channel_ref_classifies():
    assert channel_ref("123") == ("id", "123")
    assert channel_ref("notes") == ("name", "notes")
    assert channel_ref("") == ("", "")


def test_guild_setting_accepts_id_or_name():
    assert validate_value("delivery_discord_guild", "42") == "42"
    assert validate_value("delivery_discord_guild", "Example Team") == "Example Team"


def test_schema_json_is_versioned_and_complete():
    doc = config_schema(lang="en")
    assert doc["version"] == SCHEMA_VERSION and doc["plugin"] == "meeting-scribe"
    keys = {f["key"] for f in doc["fields"]}
    assert keys == set(SPEC) | {"llm_provider", "llm_model", "llm_base_url", "llm_fallback_chain"}
    by_key = {f["key"]: f for f in doc["fields"]}
    assert by_key["llm_fallback_chain"]["storage"] == "hermes"
    assert by_key["llm_fallback_chain"]["path"] == "auxiliary.meeting_scribe.fallback_chain"
    assert by_key["kanban_mode"]["storage"] == "plugin"
    ch = by_key["google_meet_discord_channel"]
    assert ch["format"] == "discord_channel" and ch["group"] == "google_meet" and ch["label"] and ch["help"]
    assert by_key["analysis_timeout_seconds"]["minimum"] == 30
    assert by_key["audio_retention"]["choices"] == ["multitrack", "mixed", "none"]
    assert [g["key"] for g in doc["groups"]] == list(GROUPS)
    assert json.loads(json.dumps(doc)) == doc  # JSON-serialisable


def test_schema_labels_follow_language():
    en = {f["key"]: f["label"] for f in config_schema(lang="en")["fields"]}
    es = {f["key"]: f["label"] for f in config_schema(lang="es")["fields"]}
    assert en["delivery_fallback_channel"] != es["delivery_fallback_channel"]


def test_manifest_carries_group_and_format():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())["config_schema"]
    assert manifest["delivery_discord_channel"]["group"] == "delivery"
    assert manifest["delivery_discord_channel"]["format"] == "discord_channel"
    assert manifest["analysis_timeout_seconds"]["minimum"] == 30
