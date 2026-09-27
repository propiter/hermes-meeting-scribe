"""Per-task projects (DESIGN §16): every action item carries its own project, and the raw spoken
name survives as ``project_hint`` when it is not a known candidate (fuzzy channel routing needs it)."""
from __future__ import annotations

from meeting_scribe.analyze.extract import LlmAnalyzer
from meeting_scribe.analyze.schemas import NOTES_SCHEMA
from meeting_scribe.config import settings_from_mapping
from meeting_scribe.domain.models import ActionItem, Candidate

from .fakes import FakeLLM
from .test_extract import full

CANDS = [Candidate("discord:501", "orion", "discord", {"channel_id": "501"}),
         Candidate("discord:502", "nebula", "discord", {"channel_id": "502"}),
         Candidate("linear:prj_9", "Infra", "linear")]


def run(meeting, utterances, items):
    llm = FakeLLM(lambda n, t: full(project=None, project_confidence=0, action_items=items))
    return LlmAnalyzer(llm, lambda: settings_from_mapping({})).analyze(meeting, utterances, CANDS), llm


def test_two_tasks_of_one_meeting_get_different_projects(meeting, utterances):
    notes, _ = run(meeting, utterances, [
        {"title": "Landing de Orion", "project": "orion", "project_confidence": 0.9},
        {"title": "Contrato Nebula", "project": "nebula", "project_confidence": 0.85}])
    a, b = notes.action_items
    assert (a.project, a.project_key) == ("orion", "discord:501")
    assert (b.project, b.project_key) == ("nebula", "discord:502")


def test_unknown_spoken_project_is_kept_as_hint_not_as_project(meeting, utterances):
    notes, _ = run(meeting, utterances, [{"title": "Revisar Nebulla", "project": "Nebulla (discord)",
                                          "project_confidence": 0.9}])
    item = notes.action_items[0]
    assert item.project is None and item.project_key is None and item.project_hint == "Nebulla"


def test_low_confidence_candidate_keeps_the_name_as_hint(meeting, utterances):
    notes, _ = run(meeting, utterances, [{"title": "x", "project": "orion", "project_confidence": 0.2}])
    item = notes.action_items[0]
    assert item.project is None and item.project_hint == "orion" and item.project_confidence == 0.2


def test_action_item_roundtrip_keeps_key_and_hint():
    item = ActionItem(id="a1", title="t", project="orion", project_key="discord:501", project_hint="Orayon")
    assert ActionItem.from_dict(item.to_dict()) == item
    assert ActionItem.from_dict({"id": "a1", "title": "t"}).project_hint is None  # old rows


def test_prompt_asks_for_a_project_per_task_and_lists_discord_channels(meeting, utterances):
    _, llm = run(meeting, utterances, [])
    call = llm.calls[0]
    assert "orion (discord)" in call["text"]
    assert "each action item" in call["instructions"] and "misspell" in call["instructions"]


def test_schema_stays_lenient_for_the_new_field():
    props = NOTES_SCHEMA["properties"]["action_items"]["items"]
    assert props["required"] == ["title"] and "project_hint" in props["properties"]
