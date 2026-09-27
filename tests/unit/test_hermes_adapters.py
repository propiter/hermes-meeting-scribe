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


# -- DESIGN §18: wall clock, max_tokens, one retry on non-JSON --------------------------------------
import threading
import time


class HangingLlm:
    """Never returns until released (a provider stuck in retries)."""

    def __init__(self):
        self.release = threading.Event()
        self.calls = 0

    def complete_structured(self, **kwargs):
        self.calls += 1
        self.release.wait(10)
        return SimpleNamespace(parsed={"late": True}, text="")


def test_a_hung_call_fails_the_attempt_after_the_wall_clock():
    from meeting_scribe.hermes_adapters import LlmTimeout

    llm = HangingLlm()
    t0 = time.monotonic()
    with pytest.raises(LlmTimeout, match="analysis_timeout_seconds"):
        HermesStructuredLLM(lambda: llm, timeout=0.3).complete_json(instructions="I", text="T", json_schema={},
                                                                   schema_name="x")
    assert time.monotonic() - t0 < 3 and llm.calls == 1  # no retry after a timeout
    llm.release.set()


def test_max_tokens_and_timeout_are_sent_and_read_per_call():
    llm = FakeLlm(parsed={"a": 1})
    box = {"t": 120, "m": 4096}
    adapter = HermesStructuredLLM(lambda: llm, timeout=lambda: box["t"], max_tokens=lambda: box["m"])
    adapter.complete_json(instructions="I", text="T", json_schema={}, schema_name="x")
    assert llm.kwargs["max_tokens"] == 4096 and llm.kwargs["timeout"] == 120
    box["m"] = 8192
    adapter.complete_json(instructions="I", text="T", json_schema={}, schema_name="x")
    assert llm.kwargs["max_tokens"] == 8192


class TruncatedOnceLlm:
    def __init__(self, fail_times=1):
        self.calls = []
        self.fail_times = fail_times

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.fail_times:
            return SimpleNamespace(parsed=None, text='{"meeting_title": "cut in the mid')
        return SimpleNamespace(parsed={"meeting_title": "ok"}, text="")


def test_non_json_reply_is_retried_once_with_a_stricter_instruction():
    llm = TruncatedOnceLlm()
    out = HermesStructuredLLM(lambda: llm).complete_json(instructions="I", text="T", json_schema={},
                                                         schema_name="x")
    assert out == {"meeting_title": "ok"} and len(llm.calls) == 2
    assert llm.calls[1]["instructions"].startswith("I") and "not valid JSON" in llm.calls[1]["instructions"]


def test_non_json_twice_fails_the_attempt():
    llm = TruncatedOnceLlm(fail_times=5)
    with pytest.raises(ValueError, match="not JSON"):
        HermesStructuredLLM(lambda: llm).complete_json(instructions="I", text="T", json_schema={}, schema_name="x")
    assert len(llm.calls) == 2
