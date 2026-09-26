import json

import yaml

from meeting_scribe.storage import artifacts as art


def test_atomic_write_leaves_no_tmp(tmp_path):
    p = tmp_path / "x" / "f.json"
    art.atomic_write_text(p, "hi")
    assert p.read_text() == "hi"
    assert [q.name for q in p.parent.iterdir()] == ["f.json"]


def test_meta_roundtrip(tmp_path, meeting):
    art.write_meta(tmp_path, meeting)
    assert art.read_meta(tmp_path) == meeting


def test_transcript_jsonl_roundtrip_and_sorted(tmp_path, utterances):
    art.write_transcript(tmp_path, list(reversed(utterances)))
    lines = (tmp_path / "transcript.jsonl").read_text().splitlines()
    assert [json.loads(line)["t0"] for line in lines] == [0.0, 3.5, 8.0]
    assert art.read_transcript(tmp_path) == utterances


def test_transcript_md(tmp_path, meeting, utterances):
    art.write_transcript_md(tmp_path, meeting, utterances, "es")
    md = (tmp_path / "transcript.md").read_text()
    assert md.startswith("# Transcripción — Daily Sync")
    assert "**[00:03] Luis:** Yo envío las credenciales el viernes." in md


def test_notes_md_has_obsidian_frontmatter(tmp_path, meeting, notes):
    art.write_notes(tmp_path, meeting, notes, "es")
    md = (tmp_path / "notes.md").read_text()
    assert md.startswith("---\n")
    fm = yaml.safe_load(md.split("---\n")[1])
    assert fm["meeting_id"] == "k3v7q2ab"
    assert fm["participants"] == ["Ana", "Luis"]
    assert fm["date"] == "2026-09-26"
    assert "meeting" in fm["tags"]
    assert "## Decisiones" in md and "- Usar SES" in md
    assert "Enviar credenciales" in md and "vence 2026-10-02" in md
    assert art.read_notes(tmp_path) == notes
    tasks = json.loads((tmp_path / "tasks.json").read_text())
    assert [t["id"] for t in tasks] == ["a0000000001", "a0000000002"]


def test_frontmatter_escapes_hostile_title(tmp_path, meeting, notes):
    from dataclasses import replace
    notes = replace(notes, meeting_title='x"\n---\nevil: true')
    art.write_notes(tmp_path, meeting, notes, "en")
    fm = yaml.safe_load((tmp_path / "notes.md").read_text().split("\n---\n")[0][4:])
    assert "evil" not in fm


def test_read_missing_returns_none(tmp_path):
    assert art.read_notes(tmp_path) is None
    assert art.read_transcript(tmp_path) == []
