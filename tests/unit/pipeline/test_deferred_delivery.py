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

    def enabled(self, meeting):
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

        def enabled(self, meeting):
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


class NoDestinationSink(NotConnectedSink):
    """Connected, but no channel is configured/resolvable yet (DESIGN §19)."""

    def __init__(self):
        super().__init__()
        self.channel = None

    def deliver(self, meeting, notes, folder):
        self.calls += 1
        if self.channel is None:
            return SinkResult(self.name, False, errors=("waiting for a Discord channel: set one with "
                                                        "`hermes meeting-scribe config set ...`",),
                              deferred=True, waiting=True)
        return SinkResult(self.name, True, (f"https://discord/{self.channel}",))


def test_waiting_for_a_destination_has_no_deadline_and_resumes_when_configured(prepo, layout, settings, clock, meeting):
    from meeting_scribe.pipeline.runner import WAITING_KV
    from meeting_scribe.pipeline.service import MeetingService

    sink = NoDestinationSink()
    runner, *_ = build(prepo, layout, settings, clock, sinks=[sink])
    m, _folder = captured(prepo, layout, meeting)
    runner.enqueue(m.id)
    while runner.run_once():
        pass
    clock.advance(runner.DEFER_MAX_SECONDS * 4)  # far beyond the Discord-connecting cap
    for _ in range(5):
        while runner.run_once():
            pass
        clock.advance(runner.WAIT_SECONDS + 1)
    assert prepo.get_meeting(m.id).state is MeetingState.ANALYZED  # parked, not failed
    job = prepo.get_job(m.id)
    assert job.attempts == 0 and job.state == "queued"
    svc = MeetingService(prepo, layout, runner, settings, clock=clock, item_sinks=lambda: {}, catalogs=lambda: [])
    st = svc.status()
    assert m.id in st["waiting_destination"] and "config set" in st["waiting_destination"][m.id]
    row = next(r for r in st["recent"] if r["id"] == m.id)
    assert row["delivery"]["state"] == "waiting_destination"
    sink.channel = "notes"  # the user configured a channel: next cycle publishes by itself
    clock.advance(runner.WAIT_SECONDS + 1)
    while runner.run_once():
        pass
    assert prepo.get_meeting(m.id).state is MeetingState.DONE
    assert prepo.kv_prefix(WAITING_KV) == {}


def test_max_attempts_follows_settings(prepo, layout, settings, clock, meeting):
    runner, *_ = build(prepo, layout, settings, clock)
    box = {"n": 5}
    runner.max_attempts = lambda: box["n"]
    assert runner.max_attempts == 5
    box["n"] = 0
    assert runner.max_attempts == 1


class CountingSink:
    def __init__(self, name):
        self.name = name
        self.calls = 0

    def enabled(self, meeting):
        return True

    def deliver(self, meeting, notes, folder):
        self.calls += 1
        return SinkResult(self.name, True, (f"{self.name}:ok",))


def test_sinks_already_done_are_not_rerun_while_discord_waits(prepo, layout, settings, clock, meeting):
    """Review M4: every 2-minute retry used to rewrite Files/Obsidian and re-read Kanban/Linear claims."""
    from meeting_scribe.domain.models import Stage

    discord, files, kanban = NoDestinationSink(), CountingSink("files"), CountingSink("kanban")
    runner, *_ = build(prepo, layout, settings, clock, sinks=[discord, files, kanban])
    m, _folder = captured(prepo, layout, meeting)
    runner.enqueue(m.id)
    for _ in range(4):
        while runner.run_once():
            pass
        clock.advance(runner.WAIT_SECONDS + 1)
    assert discord.calls == 4 and files.calls == 1 and kanban.calls == 1
    discord.channel = "notes"
    while runner.run_once():
        pass
    assert prepo.get_meeting(m.id).state is MeetingState.DONE and files.calls == 1
    runner.reprocess(m.id, Stage.DELIVER)  # an explicit re-delivery runs every sink again
    while runner.run_once():
        pass
    assert files.calls == 2 and kanban.calls == 2
