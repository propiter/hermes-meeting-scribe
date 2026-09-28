"""``meeting_routes`` rules (DESIGN §19.2): parser, validation, matching. Invented names only."""
from __future__ import annotations

from dataclasses import replace

import pytest

from meeting_scribe.config import settings_from_mapping, validate_value
from meeting_scribe.domain.models import SOURCE_GOOGLE_MEET
from meeting_scribe.routes import load_routes, match_route, parse_route, validate_entries


@pytest.mark.parametrize("entry, kind, ref, channel, private", [
    ("Leadership = #leadership-notes:private", "voice", "Leadership", "leadership-notes", True),
    ("Leadership=#leadership-notes:privada", "voice", "Leadership", "leadership-notes", True),
    ("#Design room = design-meetings", "voice", "Design room", "design-meetings", False),
    ("123456789012 = <#223456789012>", "voice", "123456789012", "223456789012", False),
    ("category:Board = 323456789012:private", "category", "Board", "323456789012", True),
    ("categoría:Board = #board", "category", "Board", "board", False),
    ("meet:ABC-*-XYZ = #meet-notes", "meet", "abc-*-xyz", "meet-notes", False),
])
def test_parse_route(entry, kind, ref, channel, private):
    r = parse_route(entry)
    assert (r.kind, r.ref, r.channel, r.private, r.error) == (kind, ref, channel, private, "")


@pytest.mark.parametrize("entry, msg", [
    ("Leadership", "expected 'origin = #channel'"),
    ("= #notes", "expected 'origin = #channel'"),
    ("Leadership = #notes:secret", "unknown option 'secret'"),
    ("Leadership = ", "expected the notes channel"),
    ("category: = #notes", "category id or name"),
    ("meet: = #notes", "Google Meet code"),
    ("<#1234> = #notes", "not a mention"),
])
def test_parse_errors_are_clear(entry, msg):
    with pytest.raises(ValueError, match=msg):
        parse_route(entry)


def test_validate_canonical_and_rejects_duplicates():
    assert validate_entries(["Leadership = leadership-notes:privado", "category:Board=1234"]) == [
        "Leadership=#leadership-notes:private", "category:Board=1234"]
    with pytest.raises(ValueError, match="appears twice"):
        validate_entries(["Leadership = #a", "leadership = #b"])
    assert validate_value("meeting_routes", "Leadership=#a:private, Design=#b") == [
        "Leadership=#a:private", "Design=#b"]
    with pytest.raises(ValueError, match="meeting_routes: .*unknown option"):
        validate_value("meeting_routes", ["Leadership=#a:hidden"])


def test_lenient_load_fails_closed():
    rules, warnings = load_routes(["Leadership = #notes:hidden", "=nothing", "Design=#design"])
    assert [r.origin for r in rules] == ["Leadership", "Design"]
    broken = rules[0]
    assert broken.private and broken.channel == "" and "unknown option" in broken.error
    assert any("kept private" in w for w in warnings) and any("ignored" in w for w in warnings)
    s = settings_from_mapping({"meeting_routes": ["Leadership = #notes:hidden"]})
    assert any("meeting_routes" in w for w in s.warnings)


def test_match_voice_by_name_and_id_category_and_precedence(meeting):
    m = replace(meeting, channel_id="200", channel_name="🔊┃Leadership", category_id="900", category_name="Board")
    rules = load_routes(["category:Board = #board-notes", "leadership = #lead:private"])[0]
    assert match_route(m, rules).origin == "leadership"  # voice rule beats category, whatever the order
    assert match_route(m, load_routes(["200 = #x", "Leadership = #y"])[0]).channel == "x"  # first wins
    assert match_route(m, load_routes(["category:900 = #c"])[0]).channel == "c"
    assert match_route(replace(m, category_id=None, category_name=""), load_routes(["category:Board=#c"])[0]) is None
    assert match_route(m, load_routes(["Design = #g"])[0]) is None


def test_meet_rules_only_match_meet_meetings(meeting):
    meet = replace(meeting, source=SOURCE_GOOGLE_MEET, guild_id="", channel_id="gmeet:spaces-xyz",
                   channel_name="abc-defg-hij", title="Weekly board review")
    rules = load_routes(["meet:ABC-* = #meet-notes:private"])[0]
    assert match_route(meet, rules).private
    assert match_route(meet, load_routes(["meet:*board* = #b"])[0]).channel == "b"  # by title
    assert match_route(meet, load_routes(["meet:spaces-x?z = #r"])[0]).channel == "r"  # by room id
    assert match_route(meeting, rules) is None  # a Discord meeting never matches a Meet rule
    assert match_route(meet, load_routes(["abc-defg-hij = #v"])[0]) is None  # nor a Meet one a voice rule
