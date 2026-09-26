from meeting_scribe.analyze.projects import (
    CallableCatalog, LearnedCatalog, ProjectResolver, gather_candidates, hints_for,
)
from meeting_scribe.domain.models import Candidate


class FakeRepo:
    def __init__(self, mapping):
        self.mapping = mapping

    def channel_project(self, channel_id):
        return self.mapping.get(channel_id)


def test_gather_candidates_dedups_and_survives_failing_catalog(meeting):
    ok = CallableCatalog("hermes", lambda m: [Candidate("hermes:p1", "Website", "hermes")])
    dup = CallableCatalog("kanban", lambda m: [Candidate("hermes:p1", "Website", "hermes"),
                                                Candidate("kanban:ops", "Ops", "kanban")])

    def broken(_m):
        raise RuntimeError("db locked")
    bad = CallableCatalog("linear", broken)
    cands, errors = gather_candidates([ok, dup, bad], meeting)
    assert [c.key for c in cands] == ["hermes:p1", "kanban:ops"]
    assert errors and "linear" in errors[0]


def test_hints(meeting):
    assert hints_for(meeting) == ["Acme", "Engineering", "Daily Sync"]


def test_learned_map_short_circuits(meeting):
    repo = FakeRepo({"200": {"project_key": "kanban:ops", "project_name": "Ops"}})
    cands = [Candidate("kanban:ops", "Ops", "kanban"), Candidate("hermes:p1", "Website", "hermes")]
    r = ProjectResolver(min_confidence=0.6, learned=repo)
    res = r.resolve(meeting, cands, llm_choice="Website", llm_confidence=0.99)
    assert res.candidate.key == "kanban:ops" and res.confidence == 1.0 and res.reason == "learned"
    assert r.learned_first(meeting, cands).key == "kanban:ops"


def test_llm_choice_needs_min_confidence_and_membership(meeting):
    cands = [Candidate("hermes:p1", "Website", "hermes")]
    r = ProjectResolver(min_confidence=0.6, learned=FakeRepo({}))
    assert r.resolve(meeting, cands, "website", 0.7).candidate.key == "hermes:p1"
    assert r.resolve(meeting, cands, "hermes:p1", 0.7).candidate.key == "hermes:p1"
    assert r.resolve(meeting, cands, "Website", 0.5).candidate is None
    assert r.resolve(meeting, cands, "Other", 0.9).candidate is None


def test_channel_name_hint_fallback(meeting):
    from dataclasses import replace
    m = replace(meeting, channel_name="website-sync", category_name="Website")
    cands = [Candidate("hermes:p1", "Website", "hermes")]
    res = ProjectResolver(min_confidence=0.6, learned=FakeRepo({})).resolve(m, cands, None, 0.0)
    assert res.candidate.key == "hermes:p1" and res.reason == "hint"


def test_learned_catalog_exposes_learned_projects(meeting):
    repo = FakeRepo({"200": {"project_key": "kanban:ops", "project_name": "Ops"}})
    assert LearnedCatalog(repo).candidates(meeting) == [Candidate("kanban:ops", "Ops", "learned")]
