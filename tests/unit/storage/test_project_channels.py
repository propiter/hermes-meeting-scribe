"""Learned project → Discord channel map (DESIGN §16: 📁 corrections teach routing)."""
from __future__ import annotations

from meeting_scribe.storage.repo import SCHEMA_VERSION, Repository


def test_learned_project_channel_roundtrip_and_overwrite(tmp_path):
    repo = Repository(tmp_path / "i.sqlite")
    assert SCHEMA_VERSION >= 3
    assert repo.project_channel("main", "orion") is None
    repo.learn_project_channel("main", "Orion ", "501")
    assert repo.project_channel("main", "orion") == "501"
    repo.learn_project_channel("main", "ORION", "777")
    assert repo.project_channel("main", "orion") == "777"
    assert repo.project_channel("other", "orion") is None  # learned per space (DESIGN §23)
    repo.close()


def test_delivery_pointers_by_prefix(tmp_path, meeting):
    repo = Repository(tmp_path / "i.sqlite")
    repo.upsert_delivery(meeting.id, "discord", f"mtg:{meeting.id}:task:a1", external_id="{}", url=None)
    repo.upsert_delivery(meeting.id, "discord", f"mtg:{meeting.id}:notes", external_id="{}", url=None)
    keys = [r["key"] for r in repo.list_deliveries(meeting.id, sink="discord", prefix=f"mtg:{meeting.id}:task:")]
    assert keys == [f"mtg:{meeting.id}:task:a1"]
    repo.delete_delivery("discord", f"mtg:{meeting.id}:task:a1")
    assert repo.list_deliveries(meeting.id, sink="discord", prefix=f"mtg:{meeting.id}:task:") == []
    repo.close()


def test_a_moved_item_keeps_its_project_across_reanalysis(tmp_path, meeting):
    from dataclasses import replace

    from meeting_scribe.domain.models import ActionItem
    repo = Repository(tmp_path / "db.sqlite")
    repo.save_meeting(meeting)
    a = ActionItem(id="a1", title="Landing", project="orion", project_key="discord:501")
    repo.sync_action_items(meeting.id, (a,))
    repo.set_item_override(meeting.id, "a1", project="nebula", project_key="discord:502")
    repo.sync_action_items(meeting.id, (replace(a, title="Landing page"),))  # re-analysis
    got = repo.get_action_item(meeting.id, "a1")
    assert (got.title, got.project, got.project_key, got.project_confidence) == ("Landing page", "nebula",
                                                                                 "discord:502", 1.0)
    assert repo.list_action_items(meeting.id)[0].project_key == "discord:502"
    repo.close()
