import re

import pytest

from meeting_scribe.domain.ids import action_item_id, idempotency_key, short_id, slugify


def test_short_id_shape_and_uniqueness():
    ids = {short_id() for _ in range(500)}
    assert len(ids) == 500
    assert all(re.fullmatch(r"[a-z2-7]{8}", i) for i in ids)


def test_slugify():
    assert slugify("Reunión de Diseño: ¿SMTP?") == "reunion-de-diseno-smtp"
    assert slugify("   ") == "meeting"
    assert len(slugify("x" * 200)) <= 60
    assert not slugify("a" * 39 + " b").endswith("-")


REAL_MEET_TITLE = "Google Meet · 2026-09-27 07:45 · gmj-bcgo-bqf"


def test_slug_keeps_a_meet_code_whole():
    """The default Meet title must not be cut in the middle of its meeting code."""
    assert slugify(REAL_MEET_TITLE) == "google-meet-2026-09-27-07-45-gmj-bcgo-bqf"


@pytest.mark.parametrize("max_len", range(12, 41))
def test_slug_cuts_on_word_boundaries_only(max_len):
    slug = slugify(REAL_MEET_TITLE, max_len)
    assert len(slug) <= max_len
    words = ["google", "meet", "2026-09-27", "07", "45", "gmj-bcgo-bqf"]
    joined = ["-".join(words[:i]) for i in range(1, len(words) + 1)]
    assert slug in joined  # a prefix made of whole words: the code is whole or absent


def test_slug_hard_cuts_only_a_single_oversized_word():
    assert slugify("x" * 200, 40) == "x" * 40
    assert slugify("Planning " + "y" * 80, 40) == "planning"

def test_idempotency_key_format():
    assert idempotency_key("abc12345", "a1b2") == "mtg:abc12345:a1b2"
    assert idempotency_key("abc12345", "a1b2", sink="linear") == "mtg:abc12345:a1b2:linear"


def test_action_item_id_is_stable_and_normalized():
    a = action_item_id("Send  the SMTP creds!", "10")
    assert a == action_item_id("send the smtp creds", "10")
    assert a != action_item_id("send the smtp creds", "11")
    assert re.fullmatch(r"a[0-9a-f]{10}", a)


def test_action_item_ids_keep_latin_ids_stable():
    """Review finding 4: the Unicode-preserving fold must not change ids of existing Latin items."""
    assert action_item_id("Revisar la migración SMTP", "11") == action_item_id("revisar la migracion smtp", "11")
    assert action_item_id("Send the SMTP creds", "10") == "a" + __import__("hashlib").sha1(
        b"send the smtp creds|10").hexdigest()[:10]


@pytest.mark.parametrize("a,b", [
    ("Отправить отчёт", "Позвонить клиенту"),
    ("报告", "打电话给客户"),
    ("レポートを送る", "レポートを贈る"),
    ("ارسال التقرير", "الاتصال بالعميل"),
])
def test_non_latin_titles_get_distinct_ids(a, b):
    assert action_item_id(a, "11") != action_item_id(b, "11")


def test_non_latin_ids_are_normalised():
    assert action_item_id("ＡＢＣ 报告！", "11") == action_item_id("abc 报告", "11")  # NFKC + casefold
    assert action_item_id("ОТЧЁТ", "11") == action_item_id("отчёт", "11")
    assert action_item_id("か", "1") != action_item_id("が", "1")  # dakuten is part of the letter
