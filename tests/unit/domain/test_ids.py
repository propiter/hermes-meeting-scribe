import re

from meeting_scribe.domain.ids import action_item_id, idempotency_key, short_id, slugify


def test_short_id_shape_and_uniqueness():
    ids = {short_id() for _ in range(500)}
    assert len(ids) == 500
    assert all(re.fullmatch(r"[a-z2-7]{8}", i) for i in ids)


def test_slugify():
    assert slugify("Reunión de Diseño: ¿SMTP?") == "reunion-de-diseno-smtp"
    assert slugify("   ") == "meeting"
    assert len(slugify("x" * 200)) <= 40
    assert not slugify("a" * 39 + " b").endswith("-")


def test_idempotency_key_format():
    assert idempotency_key("abc12345", "a1b2") == "mtg:abc12345:a1b2"
    assert idempotency_key("abc12345", "a1b2", sink="linear") == "mtg:abc12345:a1b2:linear"


def test_action_item_id_is_stable_and_normalized():
    a = action_item_id("Send  the SMTP creds!", "10")
    assert a == action_item_id("send the smtp creds", "10")
    assert a != action_item_id("send the smtp creds", "11")
    assert re.fullmatch(r"a[0-9a-f]{10}", a)
