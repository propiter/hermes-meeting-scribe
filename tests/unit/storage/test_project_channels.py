"""Learned project → Discord channel map (DESIGN §16: 📁 corrections teach routing)."""
from __future__ import annotations

from meeting_scribe.storage.repo import SCHEMA_VERSION, Repository


def test_learned_project_channel_roundtrip_and_overwrite(tmp_path):
    repo = Repository(tmp_path / "i.sqlite")
    assert SCHEMA_VERSION >= 3
    assert repo.project_channel("orion") is None
    repo.learn_project_channel("Orion ", "501")
    assert repo.project_channel("orion") == "501"
    repo.learn_project_channel("ORION", "777")
    assert repo.project_channel("orion") == "777"
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
