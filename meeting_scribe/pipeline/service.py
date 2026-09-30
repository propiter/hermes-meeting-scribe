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

import logging
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from ..analyze.projects import find_candidate, gather_candidates
from ..config import Settings
from ..domain.errors import ItemDismissed, NotesNotReady, SinkUnavailable
from ..domain.models import (
    SOURCE_DISCORD, ActionItem, ActionStatus, Candidate, Meeting, MeetingState, SinkResult, Speaker, Stage, Utterance,
)
from ..domain.ports import Clock, ProjectCatalog
from ..storage.artifacts import read_meta, read_notes, write_meta, write_notes, write_transcript, write_transcript_md
from ..storage.layout import Layout
from ..storage.owner import capturing
from ..storage.repo import Repository
from .runner import PipelineRunner, effective_stage
from .task_moves import apply_move

log = logging.getLogger(__name__)


class MeetingService:
    def __init__(self, repo: Repository, layout: Layout, runner: PipelineRunner, settings: Callable[..., Settings], *,
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
    # ``settings(space)`` returns that space's settings (DESIGN §23); ``space=None`` on a lookup means
    # "any space" and is only used where the caller already holds a meeting id it was given by us.
    def find(self, id_or_prefix: str, space: Optional[str] = None) -> Optional[Meeting]:
        return self.repo.find_meeting(id_or_prefix, space)

    def require(self, id_or_prefix: str, space: Optional[str] = None) -> Meeting:
        meeting = self.find(id_or_prefix, space)
        if meeting is None:
            raise KeyError(id_or_prefix)
        return meeting

    def folder(self, meeting: Meeting) -> Path:
        return self.layout.meeting_folder(meeting)

    def space_for(self, guild_id: Optional[str] = None) -> str:
        """The space a request acts in (DESIGN §23). From a Discord server: the space that owns it
        (with one space an unowned server joins it). Without a server (DM, CLI, agent outside a
        server): the only space; with several a :class:`~meeting_scribe.spaces.SpaceError` asks for
        an explicit choice, so nothing of another team is ever shown by default."""
        from ..spaces import SpaceError, Spaces

        if guild_id not in (None, ""):
            owner = self.repo.claim_guild(str(guild_id), "") if str(guild_id).isdigit() else None
            if owner is None:
                raise SpaceError(f"Discord server {guild_id} belongs to no space")
            return owner
        return Spaces(lambda: self.repo, lambda key, default=None: default).resolve(None).slug

    def search(self, query: str, space: str, limit: int = 10, reader: Optional[Any] = None) -> list[dict[str, Any]]:
        """Transcript hits of ``space``; with a ``reader`` (a chat) the lines of private meetings it may
        not read are left out (DESIGN §19.2)."""
        if reader is None:
            return self.repo.search(query, space, limit)
        hits: list[dict[str, Any]] = []
        for hit in self.repo.search(query, space, limit * 4):
            if len(hits) < limit and self.readable(str(hit["meeting_id"]), reader):
                hits.append(hit)
        return hits

    def readable(self, meeting: Any, reader: Any) -> bool:
        """``reader`` (:class:`~meeting_scribe.privacy.Reader`) may see ``meeting`` (an id or a Meeting)."""
        found = self.repo.get_meeting(meeting) if isinstance(meeting, str) else meeting
        return found is not None and reader.may_read(self.repo, self.settings(found.space), found)

    def is_private(self, meeting: Meeting) -> bool:
        from ..privacy import is_private

        return is_private(self.repo, self.settings(meeting.space), meeting)

    def prepare_audio(self, id_or_prefix: str) -> Path:
        """Write the listening copy (``playback.ogg``) of a meeting archived before copies existed."""
        from ..audio.ffmpeg import resolve_ffmpeg
        from ..audio.playback import build_playback

        meeting = self.require(id_or_prefix)
        return build_playback(resolve_ffmpeg(self.settings(meeting.space).audio_ffmpeg_path), self.folder(meeting))

    def status(self, space: Optional[str] = None, recent: int = 5) -> dict[str, Any]:
        """Queue size, recent meetings and pending deliveries of ``space`` (``None``: every space — only
        for the machine-wide views of an operator; chat surfaces always pass their space)."""
        jobs = {j.meeting_id: j for j in self.repo.list_jobs(("queued", "running", "failed"), space=space)}
        rows = []
        for m in self.repo.list_meetings(limit=recent, space=space):
            job = jobs.get(m.id)
            rows.append({"id": m.id, "space": m.space, "title": m.title or m.channel_name, "state": m.state.value,
                         "started_at": m.started_at.isoformat(), "missing_audio": list(m.missing_audio_names),
                         "job": None if job is None else {
                             "state": job.state, "stage": job.stage.value, "attempts": job.attempts,
                             "failed_stage": job.failed_stage.value if job.failed_stage else None,
                             "error": job.error}})
        waiting = self._in_space(self.waiting_destination(), space)
        for row in rows:
            if row["id"] in waiting:
                row["delivery"] = {"state": "waiting_destination", "reason": waiting[row["id"]]}
        return {"queued": self.repo.pending_job_count(space), "worker_running": self.runner.running, "recent": rows,
                "recording": self.recording(space),
                "waiting_destination": waiting, "dm_notes": self._in_space(self.dm_notes(), space),
                "dm_unreachable": self._in_space(self.dm_unreachable(), space)}

    def recording(self, space: Optional[str] = None) -> list[dict[str, Any]]:
        """Meetings in state ``recording`` read from the DATABASE, so every process sees the gateway's
        captures (the CLI has no capture of its own). ``live``: the process capturing it still runs —
        restarting the gateway would cut it; ``False`` is an orphan the gateway closes on its next start."""
        return [{"id": m.id, "space": m.space, "title": m.title or m.channel_name, "channel": m.channel_name,
                 "started_at": m.started_at.isoformat(), "live": capturing(owner, self.runner.owner)}
                for m, owner in self.repo.recordings(space)]

    def _in_space(self, by_meeting: dict[str, str], space: Optional[str]) -> dict[str, str]:
        if space is None:
            return by_meeting
        out = {}
        for mid, value in by_meeting.items():
            m = self.repo.get_meeting(mid)
            if m is not None and m.space == space:
                out[mid] = value
        return out

    def retry_waiting(self) -> int:
        """Re-queue now every delivery waiting for a destination (a channel setting changed)."""
        n = 0
        for mid in self.waiting_destination():
            job = self.repo.get_job(mid)
            if job is not None and job.state == "queued":
                self.repo.enqueue_job(mid, job.stage, now=self.clock.now())
                n += 1
        if n:
            self.runner._wake.set()
        return n

    def dm_notes(self) -> dict[str, str]:
        """Meetings whose notes an older version posted in a DM (DESIGN §19): id -> how to move them."""
        from ..domain.models import KV_DM_NOTES

        return {k[len(KV_DM_NOTES):]: v for k, v in self.repo.kv_prefix(KV_DM_NOTES).items()}

    def dm_unreachable(self) -> dict[str, str]:
        """Direct-messages meetings (DESIGN §19.3) some participant did not get: id -> who, and why."""
        from ..privacy import DM_UNREACHABLE_KV

        return {k[len(DM_UNREACHABLE_KV):]: v for k, v in self.repo.kv_prefix(DM_UNREACHABLE_KV).items()}

    def waiting_destination(self) -> dict[str, str]:
        """Meetings whose delivery waits for a Discord channel (DESIGN §19): id -> instruction."""
        from .runner import WAITING_KV

        return {k[len(WAITING_KV):]: v for k, v in self.repo.kv_prefix(WAITING_KV).items()}

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

    def finish_recording(self, meeting_id: str, *, speakers: Sequence[Speaker] = (), partial: bool = False,
                         heard: bool = True, missing_audio: Sequence[str] = ()) -> Meeting:
        """``heard=False``: the capture never received audio from anyone, so the meeting ends
        ``empty`` right away (no job, no transcription, nothing published). ``missing_audio``: people
        present whose voice was not captured (DESIGN §4.1)."""
        meeting = self.repo.get_meeting(meeting_id)
        if meeting is None:
            raise KeyError(meeting_id)
        merged = {s.user_id: s for s in meeting.speakers}
        merged.update({s.user_id: s for s in speakers})
        meeting = replace(meeting, speakers=tuple(merged.values()), ended_at=meeting.ended_at or self.clock.now(),
                          partial=meeting.partial or partial,
                          missing_audio=tuple(dict.fromkeys((*meeting.missing_audio, *missing_audio))))
        if not heard:
            log.info("meeting-scribe %s: nobody was heard; discarded without processing", meeting.id)
            return self.runner.stages.discard(meeting)
        meeting = meeting.with_state(MeetingState.CAPTURED)
        meeting = self.runner.stages.persist(meeting)
        self.runner.enqueue(meeting.id, Stage.TRANSCRIBE)
        return meeting

    # -- imported transcripts (DESIGN §17) -------------------------------------------------------
    def import_transcript(self, meeting: Meeting, utterances: Sequence[Utterance]) -> Optional[Meeting]:
        """Enter an already-transcribed meeting (Google Meet) at ``transcribed`` and queue ANALYZE.

        Transcript files are written first (the folder name carries a fresh id, so nothing can
        collide); then the row + utterances are inserted in ONE transaction guarded by the
        ``(space, source, external_id)`` unique index. ``None`` = somebody already imported it (the
        files just written are removed). A crash before the enqueue is healed by ``recover``.
        """
        if meeting.state is not MeetingState.TRANSCRIBED or not meeting.external_id:
            raise ValueError("import_transcript expects a 'transcribed' meeting with an external_id")
        if self.repo.find_by_external(meeting.space, meeting.source, meeting.external_id) is not None:
            return None
        folder = self.layout.meeting_folder(meeting)
        folder.mkdir(parents=True, exist_ok=True)
        meeting = replace(meeting, folder=self.layout.relative(folder))
        write_meta(folder, meeting)  # FIRST: a crash leftover is recognisable (clean_import_leftovers)
        write_transcript(folder, utterances)
        write_transcript_md(folder, meeting, utterances, meeting.language or self.settings(meeting.space).ui_language)
        # all files exist before the row: a committed row always has its transcript
        if not self.repo.create_imported_meeting(meeting, utterances):
            shutil.rmtree(folder, ignore_errors=True)
            return None
        self.runner.stages.mark_private(meeting)  # before its job exists: never delivered as a normal meeting
        self.runner.enqueue(meeting.id, Stage.ANALYZE)
        return meeting

    def clean_import_leftovers(self, *, older_than: float = 3600.0) -> int:
        """Remove folders of imports that crashed after writing files but before committing the row.

        Only folders whose ``meta.json`` says ``source != discord`` and whose meeting id has no row
        are touched, and only when older than ``older_than`` seconds (another process may be
        importing right now). Returns how many were removed.
        """
        root = self.layout.meetings_dir()
        if not root.is_dir():
            return 0
        cutoff = time.time() - older_than
        removed = 0
        for meta_path in root.glob("*/*/*/*/meta.json"):  # meetings/<space>/YYYY/MM/<folder>/
            folder = meta_path.parent
            try:
                if folder.stat().st_mtime > cutoff:
                    continue
                meta = read_meta(folder)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if meta is None or meta.source == SOURCE_DISCORD or self.repo.get_meeting(meta.id) is not None:
                continue
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
            log.info("meeting-scribe: removed leftover of an interrupted import: %s", folder)
        return removed

    # -- processing control -------------------------------------------------------------------
    def effective_stage(self, meeting: Meeting, stage: Stage) -> Stage:
        """The stage a reprocess really starts from: imported meetings have no audio to re-transcribe."""
        return effective_stage(meeting, stage)

    def reprocess(self, id_or_prefix: str, stage: Stage) -> Meeting:
        meeting = self.require(id_or_prefix)
        self.runner.reprocess(meeting.id, stage)
        return self.repo.get_meeting(meeting.id) or meeting

    # -- unidentified tracks (DESIGN §4.1) ---------------------------------------------------------
    def speaker_tracks(self, meeting: Meeting) -> list[Any]:
        """The meeting's "unidentified participant" tracks: interval, lines, owner once assigned."""
        from .speakers import tracks

        return tracks(self.repo, self.folder(meeting), meeting)

    def assign_speaker(self, meeting_id: str, label: str, who: str, *, actor: str = "local", admin: bool = False) -> Any:
        """Give an unidentified track to a participant (see :mod:`.speakers`)."""
        from .speakers import assign

        return assign(self, meeting_id, label, who, actor=actor, admin=admin)

    # -- action items -------------------------------------------------------------------------
    def item_sinks(self) -> Mapping[str, Any]:
        return self._item_sinks()

    def assign_task(self, meeting_id: str, item_id: str, who: str, actor: Any, *, name: str = "") -> Any:
        """Give a task to someone, take it or release it (see :mod:`.task_assign` for who may)."""
        from .task_assign import assign

        done = assign(self, meeting_id, item_id, who, actor, name=name)
        if done.changed:
            self.runner._wake.set()  # the gateway's worker shows it in Discord on its next tick
        return done

    def undo_task_assignment(self, meeting_id: str, item_id: str, actor: Any) -> Any:
        from .task_assign import undo

        done = undo(self, meeting_id, item_id, actor)
        self.runner._wake.set()
        return done

    def resolve_task(self, meeting: Meeting, ref: str) -> ActionItem:
        """A task of ``meeting`` by id, unique id prefix, or the id of the Discord message that shows it."""
        from .task_assign import TaskAssignError, item_for_message

        ref = str(ref or "").strip()
        items = self.repo.list_action_items(meeting.id)
        exact = next((a for a in items if a.id == ref), None)
        if exact is not None:
            return exact
        by_message = item_for_message(self.repo, meeting.id, ref)
        if by_message is not None:
            found = next((a for a in items if a.id == by_message), None)
            if found is not None:
                return found
        prefixed = [a for a in items if ref and a.id.startswith(ref)]
        if len(prefixed) == 1:
            return prefixed[0]
        raise TaskAssignError("unknown_task", ref)

    def _sink(self, name: str, meeting: Meeting) -> Any:
        sink = self._item_sinks().get(name)
        if sink is None or not sink.enabled(meeting):
            raise SinkUnavailable(name)
        return sink

    def approve_item(self, meeting_id: str, item_id: str, sink_name: str) -> str:
        meeting = self.require(meeting_id)
        item = self.repo.get_action_item(meeting.id, item_id)
        if item is None:
            raise KeyError(item_id)
        if item.status is ActionStatus.DISMISSED:
            raise ItemDismissed(f"action item {item_id} was dismissed")
        sink = self._sink(sink_name, meeting)
        notes = read_notes(self.folder(meeting))
        if notes is None:
            raise NotesNotReady("meeting has no notes yet")
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
            eligible = getattr(self._sink(sink_name, meeting), "eligible", lambda _m, _i: True)
            if not eligible(meeting, item):
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

    def move_item(self, meeting_id: str, item_id: str, channel_id: str, name: str, *,
                  learn: bool = True) -> ActionItem:
        """📁: pin one task to a Discord channel; ``learn`` (owners) also teaches routing (DESIGN §16), never
        from a private meeting (DESIGN §19.2)."""
        meeting = self.require(meeting_id)
        # a private meeting never teaches routing: it would reveal the room's topics and route other meetings
        learn = learn and not self.is_private(meeting)
        return apply_move(self.repo, self.folder(meeting), meeting, item_id, channel_id, name, learn=learn)

    # -- projects & people --------------------------------------------------------------------
    def candidates(self, meeting: Meeting) -> list[Candidate]:
        return gather_candidates(self._catalogs(), meeting)[0]

    def set_project(self, meeting_id: str, project: str) -> Candidate:
        meeting = self.require(meeting_id)
        cands = self.candidates(meeting)
        chosen = find_candidate(project, cands)
        if chosen is None:
            raise LookupError(project)
        self.repo.learn_channel_project(meeting.space, meeting.channel_id, chosen.key, chosen.name)
        meeting = replace(meeting, project=chosen.name, project_key=chosen.key)
        self.repo.save_meeting(meeting)
        folder = self.folder(meeting)
        notes = read_notes(folder)
        if notes is not None:
            notes = replace(notes, project=chosen.name, project_confidence=1.0)
            write_notes(folder, meeting, notes, notes.language or self.settings(meeting.space).ui_language)
        return chosen

    def link_google(self, space: str, discord_user_id: str, google_user: str) -> None:
        """Link a Discord member to a Google account (``users/<id>``) WITHIN ``space``: an admin's
        decision, the only way a Google Meet attendee becomes a DM recipient or a mention (DESIGN §19.3)."""
        self.repo.set_google_user(space, discord_user_id, google_user)

    def link(self, space: str, discord_user_id: str, target: str) -> None:
        """Link a Discord member to a Linear user WITHIN ``space`` (the same person may differ per team).
        It never identifies anyone in Google Meet (DESIGN §19.3)."""
        target = target.strip()
        if "@" in target:
            self.repo.set_link(space, discord_user_id, email=target)
        else:
            self.repo.set_link(space, discord_user_id, name=target)
