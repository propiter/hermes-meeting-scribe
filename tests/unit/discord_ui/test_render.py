"""Pure rendering of notes into Discord messages + button specs (DESIGN §8)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from meeting_scribe.discord_ui.render import (
    EMBED_LIMIT, MESSAGE_LIMIT, RenderOptions, custom_id, parse_custom_id, render_notes, split_text,
)
from meeting_scribe.domain.models import ActionItem, ActionStatus


def opts(**kw):
    base = dict(lang="en", kanban_on=True, linear_on=False, is_owner_item=lambda i: i.owner_speaker_id == "11",
                has_candidates=True)
    base.update(kw)
    return RenderOptions(**base)


def test_split_text_respects_limit_and_keeps_all_content():
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(100))
    parts = split_text(text, 500)
    assert all(len(p) <= 500 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts) == text.replace("\n", "")


def test_split_text_breaks_a_single_huge_line_on_words_then_chars():
    parts = split_text("word " * 1000 + "y" * 3000, 700)
    assert all(len(p) <= 700 for p in parts) and len(parts) > 5


def test_split_text_does_not_break_mentions():
    text = ("<@123456789012345678> " * 200).strip()
    for p in split_text(text, 333):
        assert p.count("<@") == p.count(">")


def test_limits_constants():
    assert MESSAGE_LIMIT == 2000 and EMBED_LIMIT == 4096


def test_custom_id_roundtrip_and_rejects_garbage():
    cid = custom_id("ok", "k3v7q2ab", "a0000000001")
    assert cid == "mscribe:ok:k3v7q2ab:a0000000001" and len(cid) <= 100
    assert parse_custom_id(cid) == ("ok", "k3v7q2ab", "a0000000001")
    assert parse_custom_id("mscribe:boom:k3v7q2ab:x") is None
    assert parse_custom_id("hermes:approve:1") is None


def test_summary_message_has_tldr_decisions_questions(meeting, notes):
    msgs = render_notes(meeting, notes, list(notes.action_items), opts())
    head = msgs[0].content
    assert "Migración SMTP" in head and "Migrar a SES." in head
    assert "Usar SES" in head and "¿Presupuesto?" in head
    assert all(len(m.content) <= MESSAGE_LIMIT for m in msgs)


def test_action_items_grouped_by_person_with_mentions(meeting, notes):
    msgs = render_notes(meeting, notes, list(notes.action_items), opts())
    body = "\n".join(m.content for m in msgs)
    assert "<@11>" in body and "Enviar credenciales" in body
    assert body.index("<@11>") < body.index("Enviar credenciales") < body.index("Unassigned") < body.index(
        "Revisar costos")


def test_buttons_per_item_owner_kanban_only_and_linear_when_active(meeting, notes):
    msgs = render_notes(meeting, notes, list(notes.action_items), opts(linear_on=True))
    ids = [b.custom_id for m in msgs for b in m.buttons]
    assert "mscribe:ok:k3v7q2ab:a0000000001" in ids      # owner item → Kanban
    assert "mscribe:ok:k3v7q2ab:a0000000002" not in ids  # not owner → no Kanban button
    assert "mscribe:lin:k3v7q2ab:a0000000002" in ids and "mscribe:no:k3v7q2ab:a0000000002" in ids
    bulk = msgs[-1]
    assert {b.custom_id for b in bulk.buttons} == {"mscribe:allk:k3v7q2ab:all", "mscribe:alll:k3v7q2ab:all",
                                                   "mscribe:prj:k3v7q2ab:all"}


def test_no_linear_buttons_when_inactive_and_no_kanban_when_off(meeting, notes):
    msgs = render_notes(meeting, notes, list(notes.action_items), opts(kanban_on=False))
    ids = " ".join(b.custom_id for m in msgs for b in m.buttons)
    assert ":lin:" not in ids and ":alll:" not in ids and ":ok:" not in ids and ":allk:" not in ids
    assert ":no:" in ids


def test_status_is_shown_and_finished_items_have_no_buttons(meeting, notes):
    items = [replace(notes.action_items[0], status=ActionStatus.DELIVERED),
             replace(notes.action_items[1], status=ActionStatus.DISMISSED)]
    msgs = render_notes(meeting, notes, items, opts(linear_on=True))
    body = "\n".join(m.content for m in msgs)
    assert "✅" in body and "~~Revisar costos~~" in body
    per_item = [b for m in msgs[:-1] for b in m.buttons]
    assert per_item == []


def test_many_items_split_into_messages_with_at_most_five_rows(meeting, notes):
    items = tuple(ActionItem(id=f"a{i:010d}", title=f"Task {i} " + "d" * 150, owner_speaker_id="11",
                             owner_name="Luis") for i in range(23))
    n = replace(notes, action_items=items)
    msgs = render_notes(meeting, n, list(items), opts(linear_on=True))
    for m in msgs:
        assert len(m.content) <= MESSAGE_LIMIT
        assert len({b.row for b in m.buttons}) <= 5 and len(m.buttons) <= 25
    ids = [b.custom_id for m in msgs for b in m.buttons if b.custom_id.startswith("mscribe:no:")]
    assert len(ids) == 23


def test_partial_meeting_and_empty_sections(meeting, notes):
    n = replace(notes, decisions=(), open_questions=(), action_items=())
    msgs = render_notes(replace(meeting, partial=True), n, [], opts())
    body = "\n".join(m.content for m in msgs)
    assert "interrupted" in body and "None." in body
    assert all(":allk:" not in b.custom_id for m in msgs for b in m.buttons)


@pytest.mark.parametrize("lang", ["en", "es"])
def test_language(meeting, notes, lang):
    msgs = render_notes(meeting, notes, list(notes.action_items), opts(lang=lang))
    assert ("Decisiones" in msgs[0].content) == (lang == "es")
