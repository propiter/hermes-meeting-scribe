"""Review finding 8 against Hermes' REAL structured-output validator (jsonschema)."""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.integration
plugin_llm = pytest.importorskip("agent.plugin_llm", reason="Hermes is not importable (set PYTHONPATH)")
pytest.importorskip("jsonschema")

from meeting_scribe.analyze.schemas import CHUNK_SCHEMA, NOTES_SCHEMA  # noqa: E402

ITEM = {"title": "a", "description": "", "owner_speaker_id": None, "owner_name": None, "due": None,
        "project": None, "project_confidence": 0, "quote": "", "t0": None}
BASE = {"meeting_title": "x", "tldr": "", "summary": "", "topics": [], "decisions": [], "open_questions": [],
        "language": "es", "project": None, "project_confidence": 0}


@pytest.mark.parametrize("item", [
    {k: v for k, v in ITEM.items() if k != "quote"},        # model omits one field
    {**ITEM, "project_confidence": None},
    {**ITEM, "t0": "12:30"},                               # string timestamp
    {"title": "only a title"},
])
def test_normalisable_variants_pass_hermes_validation(item):
    parsed, kind = plugin_llm._parse_structured_text(text=json.dumps({**BASE, "action_items": [item]}),
                                                     json_mode=False, json_schema=NOTES_SCHEMA)
    assert kind == "json" and parsed["action_items"][0]["title"]


def test_chunk_schema_tolerates_missing_lists():
    parsed, kind = plugin_llm._parse_structured_text(text=json.dumps({"summary": "s", "action_items": []}),
                                                     json_mode=False, json_schema=CHUNK_SCHEMA)
    assert kind == "json"


def test_extra_field_fails_validation_so_the_adapter_falls_back_to_plain_json():
    with pytest.raises(ValueError):
        plugin_llm._parse_structured_text(text=json.dumps({**BASE, "action_items": [{**ITEM, "priority": "high"}]}),
                                          json_mode=False, json_schema=NOTES_SCHEMA)


def test_item_without_title_is_still_rejected():
    with pytest.raises(ValueError):
        plugin_llm._parse_structured_text(text=json.dumps({**BASE, "action_items": [{"owner_name": "x"}]}),
                                          json_mode=False, json_schema=NOTES_SCHEMA)
