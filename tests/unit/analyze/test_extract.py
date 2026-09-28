
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
    return LlmAnalyzer(llm, lambda space=None: settings_from_mapping(settings))


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
    analyzer(llm, **{"analysis_language": "en"}).analyze(meeting, utterances, CANDS)
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
    notes = analyzer(llm, **{"analysis_chunk_chars": 3000}).analyze(meeting, utts, CANDS)
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


@pytest.mark.parametrize("patch,expect_t0", [
    ({"quote": None}, 3.5), ({"priority": "high"}, 3.5), ({"project_confidence": None}, 3.5),
    ({"t0": "12:30"}, 750.0), ({"t0": "1:02:03"}, 3723.0), ({"t0": "3.5"}, 3.5), ({"t0": "soon"}, None),
    ({"owner_speaker_id": 11}, 3.5),
])
def test_normaliser_handles_loose_llm_variants(meeting, utterances, patch, expect_t0):
    """Review finding 8: the loosened schema lets these through; the normaliser must cope."""
    item = {"title": "Enviar credenciales", "owner_speaker_id": "11", "t0": 3.5, **patch}
    llm = FakeLLM(lambda name, text: full(action_items=[item], topics=None, decisions=None, language=None))
    notes = analyzer(llm).analyze(meeting, utterances, CANDS)
    (a,) = notes.action_items
    assert a.title == "Enviar credenciales" and a.t0 == expect_t0 and a.quote == ""
    assert a.owner_speaker_id == "11" and notes.topics == () and notes.decisions == ()


def test_schemas_only_require_what_extract_needs():
    item = NOTES_SCHEMA["properties"]["action_items"]["items"]
    assert item["required"] == ["title"]
    assert NOTES_SCHEMA["required"] == ["meeting_title", "tldr", "summary", "action_items"]


@pytest.mark.parametrize("single", [True, False])
def test_instructions_spell_out_every_output_key(meeting, utterances, single):
    """E2E finding: with a non-strict json_schema a real model OMITTED optional keys (owner_name,
    quote, t0, decisions, open_questions), so no task reached Kanban. The schema must stay tolerant
    (review finding 8), so the instructions carry the full shape, which works on any provider."""
    from meeting_scribe.analyze.schemas import CHUNK_SCHEMA

    llm = FakeLLM(lambda n, t: full() if n == "meeting_notes" else {"summary": "s", "action_items": []})
    utts = utterances if single else utterances * 400
    analyzer(llm, analysis_chunk_chars=2000).analyze(meeting, utts, CANDS)
    for call in llm.calls:
        schema = NOTES_SCHEMA if call["schema_name"] == "meeting_notes" else CHUNK_SCHEMA
        keys = set(schema["properties"]) | set(schema["properties"]["action_items"]["items"]["properties"])
        missing = [k for k in keys if f'"{k}"' not in call["instructions"]]
        assert not missing, (call["schema_name"], missing)
        assert "Always include every key" in call["instructions"]


def test_candidate_echoed_with_its_source_suffix_still_matches(meeting, utterances):
    """E2E finding: the model copied the candidate line format and answered "Chatio (hermes)"."""
    def resp(name, text):
        data = full(project="Website (hermes)", project_confidence=0.9)
        data["action_items"][0]["project"] = "Infra (source: linear)"
        return data
    notes = analyzer(FakeLLM(resp)).analyze(meeting, utterances, CANDS)
    assert notes.project == "Website" and notes.action_items[0].project == "Infra"


def test_prompt_gives_meeting_date_so_explicit_dates_get_a_year(meeting, utterances):
    """E2E finding: "antes del treinta de septiembre" came back as due=null — the model had no year."""
    llm = FakeLLM(lambda n, t: full())
    analyzer(llm).analyze(meeting, utterances, CANDS)
    call = llm.calls[0]
    assert f"<meeting_date>{meeting.started_at.date().isoformat()}</meeting_date>" in call["text"]
    assert "without a year" in call["instructions"]


def test_prompt_asks_for_decisions_and_open_questions(meeting, utterances):
    """E2E finding: the model omitted both although the meeting had them. The schema stays tolerant
    (Hermes validates strictly, review finding 8), so the instructions must ask for them."""
    llm = FakeLLM(lambda n, t: full())
    analyzer(llm).analyze(meeting, utterances, CANDS)
    assert "decisions lists every agreement" in llm.calls[0]["instructions"]
    assert "open_questions every question left unresolved" in llm.calls[0]["instructions"]


def test_missing_decisions_still_normalise_to_empty(meeting, utterances):
    data = full()
    del data["decisions"], data["open_questions"]
    notes = analyzer(FakeLLM(lambda n, t: data)).analyze(meeting, utterances, CANDS)
    assert notes.decisions == () and notes.open_questions == ()


# Anonymised from a real meeting: the transcript says the name as it sounds ("Yoana"), the
# participant is written "Johanna …"; one participant never spoke (no audio track).
TEAM = (Speaker("21", "Marco Aldana"), Speaker("22", "Johanna Quintero"), Speaker("23", "🐺 Ramón Vidal"),
        Speaker("24", "Kristofer V", aliases=("kris.v",)))


@pytest.mark.parametrize("said,uid", [
    ("Yoana", "22"), ("Yohana", "22"), ("johanna", "22"), ("Johana Quintero", "22"),
    ("Cristofer", "24"), ("Christopher", "24"), ("kris.v", "24"),
    ("Ramon", "23"), ("Ramón Vidal", "23"), ("ramón", "23"), ("Marco", "21"), ("Aldana", "21"),
])
def test_owner_tolerates_spelling_accents_emoji_and_nicknames(said, uid):
    assert match_owner(None, said, TEAM).user_id == uid


@pytest.mark.parametrize("said", ["Pedro", "Mar", "Yo", "el equipo", ""])
def test_owner_unknown_or_too_short_stays_unassigned(said):
    assert match_owner(None, said, TEAM) is None


def test_ambiguous_owner_stays_unassigned():
    two = (Speaker("1", "Ana Ruiz"), Speaker("2", "Ana Gómez"), Speaker("3", "Luis Paz"), Speaker("4", "Luisa Mora"))
    assert match_owner(None, "Ana", two) is None  # two Ana: never guess
    assert match_owner(None, "Ana Gómez", two).user_id == "2"
    assert match_owner(None, "Luis", two).user_id == "3"  # Luisa is someone else
    assert match_owner(None, "Luisa", two).user_id == "4"
    near = (Speaker("5", "Yohana"), Speaker("6", "Johanna"))
    assert match_owner(None, "Yoana", near) is None  # both sound like it


def test_bots_are_never_owners():
    assert match_owner(None, "Scribe", (Speaker("9", "Scribe", is_bot=True),)) is None


def test_prompt_lists_every_participant_with_id_and_the_spoken_name_resolves(meeting, utterances):
    from dataclasses import replace
    m = replace(meeting, speakers=TEAM)
    item = {"title": "Preparar la demo", "owner_speaker_id": None, "owner_name": "Yoana"}
    llm = FakeLLM(lambda n, t: full(action_items=[item]))
    notes = analyzer(llm).analyze(m, utterances, CANDS)
    text = llm.calls[0]["text"]
    assert "<participants>" in text and "- id=24: Kristofer V (also: kris.v)" in text
    assert "- id=22: Johanna Quintero" in text
    assert "<participants>" in llm.calls[0]["instructions"]
    (a,) = notes.action_items
    assert (a.owner_speaker_id, a.owner_name) == ("22", "Johanna Quintero")
