"""Application service: the single entry point for commands, tools, CLI and Phase B capture/UI.

Phase B contract (capture):
  1. ``meeting = service.begin_recording(Meeting(id=short_id(), ..., state=RECORDING))`` — creates
     the folder, persists meta + index row; returns the meeting with ``folder`` set.
  2. write per-speaker Ogg/Opus to ``service.track_path(meeting, user_id)``;
  3. ``service.finish_recording(meeting.id, speakers=...)`` — marks ``captured`` and enqueues the
     pipeline. A crash in between is handled by ``PipelineRunner.recover`` (partial=true).
Phase B buttons call ``approve_item`` / ``dismiss_item`` / ``approve_all`` / ``set_project``.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from ..analyze.projects import find_candidate, gather_candidates
from ..config import Settings
from ..domain.models import (
    ActionStatus, Candidate, Meeting, MeetingState, SinkResult, Speaker, Stage,
)
from ..domain.ports import Clock, ProjectCatalog
from ..storage.artifacts import read_notes, write_notes
from ..storage.layout import Layout
from ..storage.repo import Repository
from .runner import PipelineRunner


class MeetingService:
    def __init__(self, repo: Repository, layout: Layout, runner: PipelineRunner, settings: Callable[[], Settings], *,
                 clock: Clock, item_sinks: Callable[[], Mapping[str, Any]],
                 catalogs: Callable[[], Iterable[ProjectCatalog]]) -> None:
        self.repo = repo
        self.layout = layout
        self.runner = runner
        self.settings = settings
        self.clock = clock
        self._item_sinks = item_sinks
        self._catalogs = catalogs

    # -- lookup -------------------------------------------------------------------------------
    def find(self, id_or_prefix: str) -> Optional[Meeting]:
        return self.repo.find_meeting(id_or_prefix)

    def require(self, id_or_prefix: str) -> Meeting:
        meeting = self.find(id_or_prefix)
        if meeting is None:
            raise KeyError(id_or_prefix)
        return meeting

    def folder(self, meeting: Meeting) -> Path:
        return self.layout.meeting_folder(meeting)

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return self.repo.search(query, limit)

    def status(self, recent: int = 5) -> dict[str, Any]:
        jobs = {j.meeting_id: j for j in self.repo.list_jobs(("queued", "running", "failed"))}
        rows = []
        for m in self.repo.list_meetings(limit=recent):
            job = jobs.get(m.id)
            rows.append({"id": m.id, "title": m.title or m.channel_name, "state": m.state.value,
                         "started_at": m.started_at.isoformat(), "job": None if job is None else {
                             "state": job.state, "stage": job.stage.value, "attempts": job.attempts,
                             "failed_stage": job.failed_stage.value if job.failed_stage else None,
                             "error": job.error}})
        return {"queued": self.repo.pending_job_count(), "worker_running": self.runner.running, "recent": rows}

    # -- capture lifecycle (Phase B) -----------------------------------------------------------
    def begin_recording(self, meeting: Meeting) -> Meeting:
        if meeting.state is not MeetingState.RECORDING:
            raise ValueError("begin_recording expects a meeting in state 'recording'")
        meeting = self.runner.stages.persist(meeting)
        self.repo.set_capture_owner(meeting.id, self.runner.owner)  # only we may close it as an orphan
        return meeting

    def track_path(self, meeting: Meeting, user_id: str) -> Path:
        path = Layout.track_path(self.folder(meeting), user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def finish_recording(self, meeting_id: str, *, speakers: Sequence[Speaker] = (), partial: bool = False) -> Meeting:
        meeting = self.repo.get_meeting(meeting_id)
        if meeting is None:
            raise KeyError(meeting_id)
        merged = {s.user_id: s for s in meeting.speakers}
        merged.update({s.user_id: s for s in speakers})
        meeting = replace(meeting, speakers=tuple(merged.values()), ended_at=meeting.ended_at or self.clock.now(),
                          partial=meeting.partial or partial).with_state(MeetingState.CAPTURED)
        meeting = self.runner.stages.persist(meeting)
        self.runner.enqueue(meeting.id, Stage.TRANSCRIBE)
        return meeting

    # -- processing control -------------------------------------------------------------------
    def reprocess(self, id_or_prefix: str, stage: Stage) -> Meeting:
        meeting = self.require(id_or_prefix)
        self.runner.reprocess(meeting.id, stage)
        return self.repo.get_meeting(meeting.id) or meeting

    # -- action items -------------------------------------------------------------------------
    def _sink(self, name: str) -> Any:
        sink = self._item_sinks().get(name)
        if sink is None or not sink.enabled():
            raise KeyError(f"sink {name!r} is not available")
        return sink

    def approve_item(self, meeting_id: str, item_id: str, sink_name: str) -> str:
        meeting = self.require(meeting_id)
        item = self.repo.get_action_item(meeting.id, item_id)
        if item is None:
            raise KeyError(item_id)
        if item.status is ActionStatus.DISMISSED:
            raise ValueError(f"action item {item_id} was dismissed")
        sink = self._sink(sink_name)
        notes = read_notes(self.folder(meeting))
        if notes is None:
            raise ValueError("meeting has no notes yet")
        # Per-sink decision (review finding 1): approving for Kanban does not approve for Linear.
        self.repo.set_item_sink_status(meeting.id, item_id, sink_name, "approved")
        if item.status is ActionStatus.PENDING:
            self.repo.set_action_status(meeting.id, item_id, ActionStatus.APPROVED)
        return str(sink.deliver_item(meeting, notes, item, self.folder(meeting)))

    def approve_all(self, meeting_id: str, sink_name: str) -> SinkResult:
        meeting = self.require(meeting_id)
        delivered: list[str] = []
        errors: list[str] = []
        for item in self.repo.list_action_items(meeting.id):
            if item.status is ActionStatus.DISMISSED:
                continue
            eligible = getattr(self._sink(sink_name), "eligible", lambda _i: True)
            if not eligible(item):
                continue
            try:
                delivered.append(self.approve_item(meeting.id, item.id, sink_name))
            except Exception as exc:  # keep going; report per item
                errors.append(f"{item.id}: {type(exc).__name__}: {exc}")
        return SinkResult(sink_name, not errors, tuple(delivered), errors=tuple(errors))

    def dismiss_item(self, meeting_id: str, item_id: str) -> None:
        meeting = self.require(meeting_id)
        if self.repo.get_action_item(meeting.id, item_id) is None:
            raise KeyError(item_id)
        self.repo.set_action_status(meeting.id, item_id, ActionStatus.DISMISSED)

    # -- projects & people --------------------------------------------------------------------
    def candidates(self, meeting: Meeting) -> list[Candidate]:
        return gather_candidates(self._catalogs(), meeting)[0]

    def set_project(self, meeting_id: str, project: str) -> Candidate:
        meeting = self.require(meeting_id)
        cands = self.candidates(meeting)
        chosen = find_candidate(project, cands)
        if chosen is None:
            raise LookupError(project)
        self.repo.learn_channel_project(meeting.channel_id, chosen.key, chosen.name)
        meeting = replace(meeting, project=chosen.name, project_key=chosen.key)
        self.repo.save_meeting(meeting)
        folder = self.folder(meeting)
        notes = read_notes(folder)
        if notes is not None:
            notes = replace(notes, project=chosen.name, project_confidence=1.0)
            write_notes(folder, meeting, notes, notes.language or self.settings().ui_language)
        return chosen

    def link(self, discord_user_id: str, target: str) -> None:
        target = target.strip()
        if "@" in target:
            self.repo.set_link(discord_user_id, email=target)
        else:
            self.repo.set_link(discord_user_id, name=target)
