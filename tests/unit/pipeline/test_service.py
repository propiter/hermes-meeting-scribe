from dataclasses import replace

import pytest

from meeting_scribe.domain.models import ActionStatus, MeetingState, Speaker
from meeting_scribe.pipeline.service import MeetingService
from meeting_scribe.storage.artifacts import read_meta, read_notes

from .test_runner import build, drain


class ItemSinkFake:
    name = "kanban"

    def __init__(self):
        self.items = []

    def enabled(self):
        return True

    def deliver_item(self, meeting, notes, item, folder):
        self.items.append(item.id)
        return f"t_{item.id}"


def svc(prepo, layout, settings, clock, **kw):
    runner, tr, an, sinks = build(prepo, layout, settings, clock)
    kanban = ItemSinkFake()
    service = MeetingService(prepo, layout, runner, settings, clock=clock,
                             item_sinks=lambda: {"kanban": kanban}, catalogs=lambda: runner.stages.catalogs())
    return service, runner, kanban


def test_recording_lifecycle_enqueues_processing(prepo, layout, settings, clock, meeting):
    service, runner, _ = svc(prepo, layout, settings, clock)
    live = service.begin_recording(replace(meeting, state=MeetingState.RECORDING, ended_at=None, folder=""))
    folder = layout.meeting_folder(live)
    assert live.state is MeetingState.RECORDING and folder.is_dir() and live.folder
    assert read_meta(folder).state is MeetingState.RECORDING
    assert service.track_path(live, "10") == folder / "tracks" / "10.ogg"
    clock.advance(600)
    done = service.finish_recording(live.id, speakers=(Speaker("10", "Ana"), Speaker("99", "Bot", True)))
    assert done.state is MeetingState.CAPTURED and done.ended_at == clock.now()
    assert prepo.get_job(live.id).state == "queued"
    assert {s.user_id for s in done.speakers} == {"10", "11", "99"}


def test_begin_recording_rejects_non_recording_state(prepo, layout, settings, clock, meeting):
    service, *_ = svc(prepo, layout, settings, clock)
    with pytest.raises(ValueError):
        service.begin_recording(meeting)  # fixture is CAPTURED


def _processed(service, runner, prepo, layout, meeting):
    live = service.begin_recording(replace(meeting, state=MeetingState.RECORDING, ended_at=None))
    service.finish_recording(live.id)
    drain(runner)
    return prepo.get_meeting(live.id)


def test_approve_and_dismiss_items(prepo, layout, settings, clock, meeting):
    service, runner, kanban = svc(prepo, layout, settings, clock)
    m = _processed(service, runner, prepo, layout, meeting)
    assert service.approve_item(m.id, "a1", "kanban") == "t_a1"
    assert kanban.items == ["a1"]
    with pytest.raises(KeyError):
        service.approve_item(m.id, "nope", "kanban")
    with pytest.raises(KeyError):
        service.approve_item(m.id, "a1", "linear")
    service.dismiss_item(m.id, "a1")
    assert prepo.get_action_item(m.id, "a1").status is ActionStatus.DISMISSED
    with pytest.raises(ValueError):
        service.approve_item(m.id, "a1", "kanban")


def test_approve_all(prepo, layout, settings, clock, meeting):
    service, runner, kanban = svc(prepo, layout, settings, clock)
    m = _processed(service, runner, prepo, layout, meeting)
    result = service.approve_all(m.id, "kanban")
    assert result.ok and result.delivered == ("t_a1",) and kanban.items == ["a1"]


def test_set_project_learns_channel_map(prepo, layout, settings, clock, meeting):
    service, runner, _ = svc(prepo, layout, settings, clock)
    m = _processed(service, runner, prepo, layout, meeting)
    cand = service.set_project(m.id, "website")
    assert cand.key == "hermes:p1"
    assert prepo.channel_project(m.channel_id) == {"project_key": "hermes:p1", "project_name": "Website"}
    assert prepo.get_meeting(m.id).project == "Website"
    assert read_notes(layout.meeting_folder(m)).project == "Website"
    with pytest.raises(LookupError):
        service.set_project(m.id, "Nope")


def test_link_person(prepo, layout, settings, clock):
    service, *_ = svc(prepo, layout, settings, clock)
    service.link("10", "ana@x.io")
    service.link("11", "Luis Pérez")
    assert prepo.get_link("10")["email"] == "ana@x.io"
    assert prepo.get_link("11")["name"] == "Luis Pérez"


def test_status_and_search(prepo, layout, settings, clock, meeting):
    service, runner, _ = svc(prepo, layout, settings, clock)
    m = _processed(service, runner, prepo, layout, meeting)
    st = service.status()
    assert st["queued"] == 0 and st["recent"][0]["id"] == m.id
    assert service.search("informe")[0]["meeting_id"] == m.id
    assert service.find(m.id[:4]).id == m.id
