"""A voice given to its person on a meeting whose delivery never completed (DESIGN §4.1).

The correction must not mark the meeting DONE without delivering it: the meeting is delivered for
real (a normal DELIVER, not an edit of a publication that does not exist) and reads DONE only then.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from meeting_scribe.discord_ui.sink import DiscordNotesSink
from meeting_scribe.domain.models import KV_MOVE_FROM_DM, MeetingState, SinkResult, Speaker, Stage
from meeting_scribe.pipeline.service import MeetingService
from meeting_scribe.pipeline.stages import REPUBLISH_KV

from .conftest import RecordingSink
from .test_runner import build, drain
from .test_speaker_assign import LABEL, Analyzer, TrackTranscriber


class Publication(RecordingSink):
    """Discord-like: ``republish`` only edits what exists (``skipped`` when nothing was published)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.published = False
        self.republished = 0

    def deliver(self, meeting, notes, folder):
        res = super().deliver(meeting, notes, folder)
        self.published = self.published or res.ok
        return res

    def republish(self, meeting, notes, folder):
        self.republished += 1
        return SinkResult(self.name, True, ("url",)) if self.published else \
            SinkResult(self.name, True, skipped=("not published yet",))


def _world(prepo, layout, settings, clock, meeting, sink):
    runner, _, _, _ = build(prepo, layout, settings, clock, transcriber=TrackTranscriber(), sinks=[sink])
    runner.stages.analyzer = Analyzer()
    runner.max_attempts = 1
    service = MeetingService(prepo, layout, runner, settings, clock=clock, item_sinks=lambda: {},
                             catalogs=lambda: runner.stages.catalogs())
    live = service.begin_recording(replace(meeting, state=MeetingState.RECORDING, ended_at=None,
                                           speakers=(Speaker("10", "Ana"), Speaker("11", "Luis"),
                                                     Speaker(LABEL, "Participante sin identificar"))))
    service.finish_recording(live.id, missing_audio=("11",))
    drain(runner)
    return service, runner, live.id


@pytest.fixture
def failed(prepo, layout, settings, clock, meeting):
    sink = Publication(name="discord", fail_times=1)  # the first delivery fails for good (1 attempt)
    service, runner, mid = _world(prepo, layout, settings, clock, meeting, sink)
    assert service.require(mid).state is MeetingState.FAILED
    return service, runner, sink, mid


def test_a_correction_on_a_never_delivered_meeting_delivers_it(failed):
    service, runner, sink, mid = failed
    done = service.assign_speaker(mid, LABEL, "11", actor="cli", admin=True)
    assert (done.redeliver, done.deliver) == (False, True)
    assert service.repo.kv_get(REPUBLISH_KV + mid) is None and service.repo.kv_get(KV_MOVE_FROM_DM + mid) is None
    drain(runner)
    assert service.require(mid).state is MeetingState.DONE
    assert sink.published and sink.republished == 0 and len(sink.calls) == 2


def test_it_stays_pending_while_the_delivery_keeps_failing(failed):
    service, runner, sink, mid = failed
    sink.fail_times = 99
    service.assign_speaker(mid, LABEL, "11", actor="cli", admin=True)
    drain(runner)
    assert service.require(mid).state is MeetingState.FAILED
    assert service.repo.get_job(mid).failed_stage is Stage.DELIVER


def test_a_published_meeting_is_still_edited_in_place(prepo, layout, settings, clock, meeting):
    sink = Publication(name="discord")
    service, runner, mid = _world(prepo, layout, settings, clock, meeting, sink)
    done = service.assign_speaker(mid, LABEL, "11")
    assert (done.redeliver, done.deliver) == (True, False)
    drain(runner)
    assert service.require(mid).state is MeetingState.DONE and sink.republished == 1 and len(sink.calls) == 1


def test_a_meeting_that_failed_only_at_archive_is_edited_in_place(prepo, layout, settings, clock, meeting,
                                                                  monkeypatch):
    from meeting_scribe.pipeline.stages import Stages

    def disk_full(self, meeting):
        raise OSError("disk full")

    monkeypatch.setattr(Stages, "archive", disk_full)
    sink = Publication(name="discord")
    service, runner, mid = _world(prepo, layout, settings, clock, meeting, sink)
    assert service.require(mid).state is MeetingState.FAILED
    assert service.repo.get_job(mid).failed_stage is Stage.ARCHIVE and sink.published
    done = service.assign_speaker(mid, LABEL, "11")
    assert (done.redeliver, done.deliver) == (True, False)


def test_the_discord_identity_refresh_of_an_unpublished_meeting_claims_nothing(monkeypatch):
    sink = DiscordNotesSink.__new__(DiscordNotesSink)

    class Loop:
        def is_closed(self):
            return False

    class Done:
        def result(self, timeout):
            return ""

    sink._adapter, sink._loop, sink._timeout = (lambda: object()), (lambda: Loop()), 1
    monkeypatch.setattr(DiscordNotesSink, "republish_identity", lambda self, m, n: None)
    monkeypatch.setattr("asyncio.run_coroutine_threadsafe", lambda op, loop: Done())
    res = sink.republish(object(), object(), None)
    assert res.ok and res.delivered == () and res.skipped == ("not published yet",)
