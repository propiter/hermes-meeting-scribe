"""Pure rendering of notes into Discord messages + button specs (DESIGN §8)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from meeting_scribe.discord_ui.render import (
    EMBED_LIMIT, MESSAGE_LIMIT, custom_id, parse_custom_id, render_header, split_text,
)


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
    msgs = render_header(meeting, notes, "en")
    head = msgs[0].content
    assert "Migración SMTP" in head and "Migrar a SES." in head
    assert "Usar SES" in head and "¿Presupuesto?" in head
    assert all(len(m.content) <= MESSAGE_LIMIT and m.buttons == () for m in msgs)


def test_summary_no_longer_lists_tasks_or_button_rows(meeting, notes):
    """0.1 appended every task and then all button rows; tasks now get one message each (§16)."""
    body = "\n".join(m.content for m in render_header(meeting, notes, "en"))
    assert "Enviar credenciales" not in body and "Revisar costos" not in body


def test_long_summary_is_split_under_the_limit(meeting, notes):
    n = replace(notes, decisions=tuple(f"Decision {i} " + "x" * 150 for i in range(40)))
    msgs = render_header(meeting, n, "en")
    assert len(msgs) > 1 and all(len(m.content) <= MESSAGE_LIMIT for m in msgs)


def test_partial_meeting_and_empty_sections(meeting, notes):
    n = replace(notes, decisions=(), open_questions=(), action_items=())
    body = "\n".join(m.content for m in render_header(replace(meeting, partial=True), n, "en"))
    assert "interrupted" in body and "None." in body


def test_custom_ids_of_the_new_actions_fit_discord_limits():
    for action, item in (("mine", "all"), ("pg", "m12"), ("pg", "a0"), ("tsel", "a0123456789")):
        cid = custom_id(action, "k3v7q2ab", item)
        assert parse_custom_id(cid) == (action, "k3v7q2ab", item) and len(cid) < 100


@pytest.mark.parametrize("lang", ["en", "es"])
def test_language(meeting, notes, lang):
    msgs = render_header(meeting, notes, lang)
    assert ("Decisiones" in msgs[0].content) == (lang == "es")
