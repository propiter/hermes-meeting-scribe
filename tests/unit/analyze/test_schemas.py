"""Strict structured-output providers (Anthropic, OpenAI ``strict``) answer HTTP 400 to any schema with
an open object: "For 'object' type, 'additionalProperties' must be explicitly set to false"."""
from __future__ import annotations

from typing import Any, Iterator

import pytest

from meeting_scribe.analyze.schemas import CHUNK_SCHEMA, NOTES_SCHEMA


def _objects(node: Any, path: str = "$") -> Iterator[tuple[str, dict]]:
    if isinstance(node, dict):
        types = node.get("type")
        if types == "object" or (isinstance(types, list) and "object" in types):
            yield path, node
        for key, value in node.items():
            yield from _objects(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _objects(value, f"{path}[{i}]")


@pytest.mark.parametrize("schema", [NOTES_SCHEMA, CHUNK_SCHEMA], ids=["notes", "chunk"])
def test_every_object_is_closed_for_strict_providers(schema):
    objects = list(_objects(schema))
    assert len(objects) >= 3  # root, action item, topic
    assert [p for p, o in objects if o.get("additionalProperties") is not False] == []
