from types import SimpleNamespace

import pytest

from meeting_scribe.hermes_adapters import HermesStructuredLLM, parse_json_text


class FakeLlm:
    def __init__(self, parsed=None, text=""):
        self.parsed, self.text, self.kwargs = parsed, text, None

    def complete_structured(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(parsed=self.parsed, text=self.text)


def test_structured_llm_uses_task_and_text_block():
    llm = FakeLlm(parsed={"a": 1})
    out = HermesStructuredLLM(lambda: llm).complete_json(instructions="I", text="T", json_schema={"type": "object"},
                                                         schema_name="meeting_notes")
    assert out == {"a": 1}
    kw = llm.kwargs
    assert kw["task"] == "meeting_scribe" and kw["input"] == [{"type": "text", "text": "T"}]
    assert kw["instructions"] == "I" and kw["schema_name"] == "meeting_notes"
    assert kw["json_schema"] == {"type": "object"}


def test_falls_back_to_text_json():
    llm = FakeLlm(parsed=None, text='```json\n{"b": 2}\n```')
    assert HermesStructuredLLM(lambda: llm).complete_json(instructions="", text="", json_schema={},
                                                          schema_name="x") == {"b": 2}


def test_unparseable_raises():
    with pytest.raises(ValueError):
        parse_json_text("sorry, I cannot")


class RejectingLlm:
    """Hermes raises ValueError when output does not match the schema (agent/plugin_llm.py)."""

    def __init__(self):
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if "json_schema" in kwargs:
            raise ValueError("Plugin LLM structured output did not match schema: 'quote' is a required property")
        return SimpleNamespace(parsed={"meeting_title": "x"}, text="")


def test_schema_rejection_retries_in_json_mode_without_schema():
    """Review finding 8: one schema violation must not fail the whole analyze stage."""
    llm = RejectingLlm()
    out = HermesStructuredLLM(lambda: llm).complete_json(instructions="I", text="T", json_schema={"type": "object"},
                                                         schema_name="meeting_notes")
    assert out == {"meeting_title": "x"}
    assert len(llm.calls) == 2 and llm.calls[1]["json_mode"] is True and "json_schema" not in llm.calls[1]
    assert llm.calls[1]["task"] == "meeting_scribe" and llm.calls[1]["schema_name"] == "meeting_notes"
