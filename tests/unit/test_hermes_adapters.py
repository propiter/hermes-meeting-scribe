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
