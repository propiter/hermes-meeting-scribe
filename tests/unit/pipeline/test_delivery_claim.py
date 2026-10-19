"""A worker only claims a delivery it can actually make (DESIGN §16.2).

Every gateway that loads the plugin runs a worker, but only the one that owns the platform adapter
can deliver. A worker without it claims the delivery, gets ``deferred`` back and hands the job over,
so the job bounces between workers and the notes never reach the room — measured on a real delivery
that retried for 40 minutes, every attempt landing on the gateway with no Discord adapter.
"""
from __future__ import annotations

from meeting_scribe.domain.models import Stage

from .conftest import RecordingSink
from .test_runner import build


class OfflineSink(RecordingSink):
    """Backend not connected here: the adapter lives in another gateway's process."""

    def active(self) -> bool:
        return False


class OnlineSink(RecordingSink):
    def active(self) -> bool:
        return True


class SpeechlessSink(RecordingSink):
    """Cannot answer the probe at all (a fake, an older sink): must never block a delivery."""

    active = None  # type: ignore[assignment]


def _claim(prepo, layout, settings, clock, sinks, seen):
    runner, _, _, _ = build(prepo, layout, settings, clock, sinks=sinks)
    real = prepo.claim_next_job
    prepo.claim_next_job = lambda **kw: (seen.append(kw.get("skip_stages")), real(**kw))[1]
    runner._claim()
    return seen[-1]


def test_a_worker_without_a_connected_sink_leaves_the_delivery_alone(prepo, layout, settings, clock):
    seen: list = []
    assert Stage.DELIVER in _claim(prepo, layout, settings, clock, [OfflineSink()], seen)


def test_a_worker_with_a_connected_sink_claims_the_delivery(prepo, layout, settings, clock):
    seen: list = []
    assert Stage.DELIVER not in _claim(prepo, layout, settings, clock, [OnlineSink()], seen)


def test_one_connected_sink_is_enough(prepo, layout, settings, clock):
    seen: list = []
    assert Stage.DELIVER not in _claim(prepo, layout, settings, clock, [OfflineSink(), OnlineSink()], seen)


def test_a_sink_that_cannot_answer_never_blocks_delivery(prepo, layout, settings, clock):
    seen: list = []
    assert Stage.DELIVER not in _claim(prepo, layout, settings, clock, [SpeechlessSink()], seen)


def test_transcription_is_not_skipped_while_a_slot_is_free(prepo, layout, settings, clock):
    """The new skip must not disturb the existing one."""
    seen: list = []
    got = _claim(prepo, layout, settings, clock, [OnlineSink()], seen)
    assert Stage.TRANSCRIBE not in got
