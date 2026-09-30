"""``hermes meeting-scribe task list|assign|undo|history`` (DESIGN §16.2): the operator's rights, audited."""
from __future__ import annotations

import json

import pytest

from meeting_scribe.pipeline.task_assign import pending_announcements

from .test_cli import FakeRuntime, run
from .test_commands import make, processed


@pytest.fixture
def rt(prepo, layout, settings, clock, meeting):
    _, service, runner = make(prepo, layout, settings, clock)
    mid = processed(service, runner, meeting)  # one task, "a1" for Luis (11)
    r = FakeRuntime(service, {})
    r.owners = lambda space=None: ("900",)
    r.mid = mid
    return r


def test_list_assign_undo_and_history(rt, capsys):
    code, out = run(rt, ["task", "list", rt.mid], capsys)
    assert code == 0 and "a1: Enviar informe → Luis (11)" in out
    code, out = run(rt, ["task", "assign", rt.mid, "a1", "Ana"], capsys)
    assert code == 0 and "“Enviar informe” is now assigned to <@10>." in out
    assert rt.service().repo.get_action_item(rt.mid, "a1").owner_speaker_id == "10"
    assert pending_announcements(rt.service().repo, rt.mid)["a1"]["to"] == "10"  # Discord shows it next
    code, out = run(rt, ["task", "assign", rt.mid, "a1", "none"], capsys)
    assert code == 0 and "has no assignee now" in out
    code, out = run(rt, ["task", "undo", rt.mid, "a1"], capsys)
    assert code == 0 and rt.service().repo.get_action_item(rt.mid, "a1").owner_speaker_id == "10"
    code, out = run(rt, ["task", "history", rt.mid, "--json"], capsys)
    rows = json.loads(out)
    assert [(r["actor"], r["previous_user"], r["next_user"], r["undone"]) for r in rows] == [
        ("cli", "11", "10", 0), ("cli", "10", None, 1), ("cli", None, "10", 0)]


def test_me_is_the_only_owner_and_mistakes_are_explained(rt, capsys):
    code, out = run(rt, ["task", "assign", rt.mid, "a1", "me"], capsys)
    assert code == 0 and rt.service().repo.get_action_item(rt.mid, "a1").owner_speaker_id == "900"
    rt.owners = lambda space=None: ("900", "901")
    code, out = run(rt, ["task", "assign", rt.mid, "a1", "me"], capsys)
    assert code == 2 and "900, 901" in out
    code, out = run(rt, ["task", "assign", rt.mid, "zz", "Ana"], capsys)
    assert code == 2 and "zz" in out
    code, out = run(rt, ["task", "assign", rt.mid, "a1", "Nadie Conocido"], capsys)
    assert code == 2 and "Nadie Conocido" in out
