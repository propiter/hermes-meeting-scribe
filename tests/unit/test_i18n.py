import json
import re
from pathlib import Path

from meeting_scribe.i18n import LANGUAGES, t

I18N = Path(__file__).resolve().parents[2] / "meeting_scribe" / "i18n"


def _load(lang):
    return json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))


def test_languages_have_identical_keys():
    assert set(_load("es")) == set(_load("en"))
    assert LANGUAGES == ("en", "es")


def test_placeholders_match_between_languages():
    es, en = _load("es"), _load("en")
    for key in en:
        assert set(re.findall(r"{(\w+)}", es[key])) == set(re.findall(r"{(\w+)}", en[key])), key


def test_t_formats_and_falls_back():
    assert t("cmd.unknown", "es", sub="x") != t("cmd.unknown", "en", sub="x")
    assert "x" in t("cmd.unknown", "en", sub="x")
    assert t("cmd.unknown", "fr", sub="x") == t("cmd.unknown", "en", sub="x")
    assert t("no.such.key", "es") == "no.such.key"


def test_missing_format_arg_does_not_raise():
    assert "{sub}" in t("cmd.unknown", "en")


def test_every_t_call_in_code_uses_an_existing_key():
    code = Path(__file__).resolve().parents[2] / "meeting_scribe"
    keys = set(_load("en"))
    used = set()
    for py in code.rglob("*.py"):
        used |= set(re.findall(r"\bt\(\s*[\"']([a-z0-9_.]+)[\"']", py.read_text()))
    assert used, "expected t() usages"
    assert used - keys == set()
