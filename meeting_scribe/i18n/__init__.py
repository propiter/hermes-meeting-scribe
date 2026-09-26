"""User-facing strings (es/en). ``t`` never raises: a missing key returns the key itself and a
missing format argument leaves its ``{placeholder}`` visible, because a chat reply with a raw
placeholder is better than a crashed command handler."""
from __future__ import annotations

import json
import string
from functools import lru_cache
from pathlib import Path
from typing import Any

LANGUAGES: tuple[str, ...] = ("en", "es")
_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def _catalog(lang: str) -> dict[str, str]:
    return json.loads((_DIR / f"{lang}.json").read_text(encoding="utf-8"))


class _Missing(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def normalize_language(lang: str | None) -> str:
    code = (lang or "en").split("-")[0].split("_")[0].lower()
    return code if code in LANGUAGES else "en"


def t(key: str, lang: str | None = "en", **fmt: Any) -> str:
    template = _catalog(normalize_language(lang)).get(key) or _catalog("en").get(key) or key
    return string.Formatter().vformat(template, (), _Missing(fmt))
