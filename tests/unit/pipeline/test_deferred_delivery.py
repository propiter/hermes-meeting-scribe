"""A delivery that only waits for Discord to connect does not use up the job's attempts (finding 15:
the worker now starts when the gateway loads, possibly before Discord is connected)."""
from __future__ import annotations

from meeting_scribe.domain.models import MeetingState, SinkResult

from .test_runner import build, captured


class NotConnectedSink:
    name = "discord"

    def __init__(self):
        self.connected = False
        self.calls = 0

    def enabled(self):
        return True

    def deliver(self, meeting, notes, folder):
        self.calls += 1
        if not self.connected:
            return SinkResult(self.name, False, errors=("discord not connected yet; will retry",), deferred=True)
        return SinkResult(self.name, True, ("https://discord/x",))


def test_waiting_for_discord_does_not_fail_the_meeting(prepo, layout, settings, clock, meeting):
    sink = NotConnectedSink()
    runner, *_ = build(prepo, layout, settings, clock, sinks=[sink])
    m, _folder = captured(prepo, layout, meeting)
    runner.enqueue(m.id)
    for _ in range(10):  # far more than max_attempts
        while runner.run_once():
            pass
        clock.advance(runner.DEFER_SECONDS + 1)
    assert prepo.get_meeting(m.id).state is not MeetingState.FAILED
    job = prepo.get_job(m.id)
    assert job.attempts == 0 and job.state == "queued"
    sink.connected = True
    clock.advance(runner.DEFER_SECONDS + 1)
    while runner.run_once():
        pass
    assert prepo.get_meeting(m.id).state is MeetingState.DONE


def test_a_real_error_next_to_a_deferred_one_still_counts(prepo, layout, settings, clock, meeting):
    class Broken:
        name = "kanban"

        def enabled(self):
            return True

        def deliver(self, meeting, notes, folder):
            return SinkResult(self.name, False, errors=("boom",))
    runner, *_ = build(prepo, layout, settings, clock, sinks=[NotConnectedSink(), Broken()])
    m, _folder = captured(prepo, layout, meeting)
    runner.enqueue(m.id)
    while runner.run_once():
        pass
    assert prepo.get_job(m.id).attempts == 1


def test_deferral_is_bounded_when_discord_never_connects(prepo, layout, settings, clock, meeting):
    sink = NotConnectedSink()
    runner, *_ = build(prepo, layout, settings, clock, sinks=[sink])
    m, _folder = captured(prepo, layout, meeting)
    runner.enqueue(m.id)
    while runner.run_once():
        pass
    clock.advance(runner.DEFER_MAX_SECONDS + 1)
    for _ in range(10):
        while runner.run_once():
            pass
        clock.advance(max(runner.backoff) + 1)
    assert prepo.get_meeting(m.id).state is MeetingState.FAILED  # reported, not queued forever
