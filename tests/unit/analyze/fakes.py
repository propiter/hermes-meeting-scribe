from __future__ import annotations

from typing import Any, Callable, Mapping


class FakeLLM:
    """Records calls; ``responder(schema_name, text)`` returns the parsed JSON."""

    def __init__(self, responder: Callable[[str, str], Mapping[str, Any]]):
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, *, instructions, text, json_schema, schema_name):
        self.calls.append({"instructions": instructions, "text": text, "schema": json_schema,
                           "schema_name": schema_name})
        return self.responder(schema_name, text)
