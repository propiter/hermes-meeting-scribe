"""A provider that never answers must not wedge the worker (DESIGN §18): the attempt fails with a clear
error after ``analysis_timeout_seconds`` and the job enters the normal backoff."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from meeting_scribe.analyze.extract import LlmAnalyzer
from meeting_scribe.domain.models import MeetingState
from meeting_scribe.hermes_adapters import HermesStructuredLLM

from .test_runner import build, captured, drain


class Hung:
    def __init__(self):
        self.release = threading.Event()

    def complete_structured(self, **kw):
        self.release.wait(10)
        return SimpleNamespace(parsed=None, text="")


def test_hung_llm_fails_the_attempt_and_backs_off(prepo, layout, settings, clock, meeting):
    runner, *_ = build(prepo, layout, settings, clock)
    hung = Hung()
    runner.stages.analyzer = LlmAnalyzer(HermesStructuredLLM(lambda: hung, timeout=0.3), settings)
    m, _ = captured(prepo, layout, meeting)
    runner.enqueue(m.id)
    t0 = time.monotonic()
    drain(runner)
    assert time.monotonic() - t0 < 5
    job = prepo.get_job(m.id)
    assert job.state == "queued" and job.attempts == 1 and "analysis_timeout_seconds" in job.error
    assert prepo.get_meeting(m.id).state is MeetingState.TRANSCRIBED  # rewound to the analyze input
    hung.release.set()
