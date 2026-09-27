
import pytest

from meeting_scribe.analyze.extract import LlmAnalyzer, match_owner
from meeting_scribe.analyze.schemas import NOTES_SCHEMA
from meeting_scribe.config import settings_from_mapping
from meeting_scribe.domain.models import Candidate, Speaker, Utterance

from .fakes import FakeLLM

CANDS = [Candidate("hermes:p1", "Website", "hermes"), Candidate("linear:prj_9", "Infra", "linear")]


def full(**over):
    base = {"meeting_title": "Migración SMTP", "tldr": "Pasar a SES", "summary": "Resumen",
            "topics": [{"title": "SMTP", "points": ["SES"]}], "decisions": ["Usar SES"],
            "open_questions": [], "language": "es", "project": "Infra", "project_confidence": 0.8,
            "action_items": [
                {"title": "Enviar credenciales", "description": "SMTP", "owner_speaker_id": "11",
                 "owner_name": "Luis", "due": "2026-10-02", "project": "Infra", "project_confidence": 0.9,
                 "quote": "yo envío", "t0": 3.5},
                {"title": "Revisar costos", "description": "", "owner_speaker_id": None, "owner_name": "ana",
                 "due": "el viernes", "project": "Unknown", "project_confidence": 0.9, "quote": "", "t0": None},
            ]}
    base.update(over)
    return base


def analyzer(llm, **settings):
    return LlmAnalyzer(llm, lambda: settings_from_mapping(settings))


def test_single_call_for_short_meeting(meeting, utterances):
    llm = FakeLLM(lambda name, text: full())
    notes = analyzer(llm).analyze(meeting, utterances, CANDS)
    assert len(llm.calls) == 1 and llm.calls[0]["schema_name"] == "meeting_notes"
    assert llm.calls[0]["schema"] is NOTES_SCHEMA
    assert "<transcript>" in llm.calls[0]["text"] and "Luis (id=11)" in llm.calls[0]["text"]
    assert "Website" in llm.calls[0]["text"]  # candidates offered
    assert notes.meeting_title == "Migración SMTP" and notes.decisions == ("Usar SES",)
    a, b = notes.action_items
    assert a.owner_speaker_id == "11" and a.due == "2026-10-02" and a.project == "Infra"
    assert b.owner_speaker_id == "10" and b.owner_name == "Ana"  # fuzzy name match
    assert b.due is None  # not ISO -> not explicit
    assert b.project is None  # not a candidate
    assert a.id.startswith("a") and a.id != b.id
    assert notes.project == "Infra" and notes.language == "es"


def test_ids_stable_across_runs(meeting, utterances):
    llm = FakeLLM(lambda name, text: full())
    n1 = analyzer(llm).analyze(meeting, utterances, CANDS)
    n2 = analyzer(llm).analyze(meeting, utterances, CANDS)
    assert [a.id for a in n1.action_items] == [a.id for a in n2.action_items]


def test_low_confidence_project_unassigned(meeting, utterances):
    llm = FakeLLM(lambda n, t: full(project_confidence=0.3))
    assert analyzer(llm).analyze(meeting, utterances, CANDS).project is None


def test_prompt_marks_transcript_as_untrusted(meeting, utterances):
    llm = FakeLLM(lambda n, t: full())
    analyzer(llm).analyze(meeting, utterances, CANDS)
    instr = llm.calls[0]["instructions"].lower()
    assert "data" in instr and "never follow" in instr


def test_notes_language_setting(meeting, utterances):
    llm = FakeLLM(lambda n, t: full(language="es"))
    analyzer(llm, **{"analysis.language": "en"}).analyze(meeting, utterances, CANDS)
    assert "English" in llm.calls[0]["instructions"]


def test_map_reduce_for_long_meeting(meeting):
    utts = [Utterance(float(i), float(i) + 1, "10", "Ana", "palabra " * 60) for i in range(120)]

    def respond(name, text):
        if name == "meeting_chunk":
            return {"summary": "parte", "topics": [], "decisions": ["D"], "open_questions": [],
                    "action_items": [{"title": "Tarea X", "description": "", "owner_speaker_id": "10",
                                      "owner_name": "Ana", "due": None, "project": None,
                                      "project_confidence": 0, "quote": "", "t0": 1.0}]}
        return full(action_items=[{"title": "Tarea X", "description": "", "owner_speaker_id": "10",
                                   "owner_name": "Ana", "due": None, "project": None,
                                   "project_confidence": 0, "quote": "", "t0": 1.0}])
    llm = FakeLLM(respond)
    notes = analyzer(llm, **{"analysis.chunk_chars": 3000}).analyze(meeting, utts, CANDS)
    names = [c["schema_name"] for c in llm.calls]
    assert names[-1] == "meeting_notes" and names.count("meeting_chunk") == len(names) - 1 >= 2
    assert "<chunk_notes>" in llm.calls[-1]["text"]
    assert len(notes.action_items) == 1


def test_duplicate_items_are_collapsed(meeting, utterances):
    item = full()["action_items"][0]
    llm = FakeLLM(lambda n, t: full(action_items=[item, dict(item)]))
    assert len(analyzer(llm).analyze(meeting, utterances, CANDS).action_items) == 1


def test_malformed_llm_output_raises(meeting, utterances):
    llm = FakeLLM(lambda n, t: {"nope": 1})
    with pytest.raises(ValueError):
        analyzer(llm).analyze(meeting, utterances, CANDS)


def test_empty_transcript_skips_llm(meeting):
    llm = FakeLLM(lambda n, t: full())
    notes = analyzer(llm).analyze(meeting, [], CANDS)
    assert llm.calls == [] and notes.action_items == () and notes.meeting_title == "Daily Sync"


def test_match_owner():
    speakers = (Speaker("10", "Ana María"), Speaker("11", "Luis Pérez"))
    assert match_owner("11", None, speakers) == speakers[1]
    assert match_owner(None, "luis perez", speakers) == speakers[1]
    assert match_owner(None, "Luis", speakers) == speakers[1]  # unique first-name
    assert match_owner("99", "Nadie", speakers) is None
    assert match_owner(None, None, speakers) is None


def test_non_latin_items_are_not_merged(meeting, utterances):
    """Review finding 4: two Chinese tasks of the same owner used to collapse into one id."""
    items = [{"title": "报告", "owner_speaker_id": "11"}, {"title": "打电话给客户", "owner_speaker_id": "11"},
             {"title": "Отправить отчёт", "owner_speaker_id": "11"}]
    llm = FakeLLM(lambda name, text: full(action_items=items))
    notes = analyzer(llm).analyze(meeting, utterances, CANDS)
    assert [a.title for a in notes.action_items] == ["报告", "打电话给客户", "Отправить отчёт"]
    assert len({a.id for a in notes.action_items}) == 3


def test_cyrillic_owner_name_matches_speaker():
    speakers = [Speaker("11", "Иван Петров"), Speaker("12", "Ана")]
    assert match_owner(None, "иван петров", speakers).user_id == "11"
    assert match_owner(None, "Ана", speakers).user_id == "12"
