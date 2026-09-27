"""Stage functions (DESIGN §9): captured → transcribe → analyze → deliver → archive.

Each stage reads its inputs from the meeting folder / index and writes its outputs atomically, so
re-running a stage (retry, resume, ``reprocess``) is safe. External side effects are the sinks',
which are idempotent through the ``deliveries`` table.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from ..analyze.projects import ProjectResolver, gather_candidates
from ..analyze.reconcile import reconcile_ids
from ..audio.archive import build_archive
from ..audio.ffmpeg import Ffmpeg
from ..config import Settings
from ..domain.errors import EmptyRecording
from ..domain.models import Meeting, MeetingState, SinkResult, Stage
from ..domain.ports import Analyzer, ProjectCatalog, Sink, Transcriber
from ..storage.artifacts import (
    NOTES_JSON, read_notes, read_transcript, write_meta, write_notes, write_transcript, write_transcript_md,
)
from ..storage.layout import Layout
from ..storage.repo import Repository

Archiver = Callable[[Meeting, Path], Optional[Path]]
ProgressCb = Callable[[str, str, float], None]  # meeting_id, track, fraction


SINKS_DONE_KV = "pipeline.sinks_done."  # meeting id -> {"notes": sha256, "sinks": [...]} (review M4)


class StageError(RuntimeError):
    """A stage finished with recoverable errors (e.g. one sink failed); the job is retried."""


class StageDeferred(StageError):
    """Only waiting for a target to become ready (Discord still connecting): retried later without
    using up an attempt. ``waiting``: no destination is configured/resolvable yet — that wait has no
    deadline (it ends when the user configures a channel, DESIGN §19)."""

    def __init__(self, message: str, *, waiting: bool = False) -> None:
        super().__init__(message)
        self.waiting = waiting


def make_archiver(settings: Callable[[], Settings], ffmpeg: Callable[[], Ffmpeg]) -> Archiver:
    """Production archiver: package ``tracks/`` per ``audio.retention`` (DESIGN §5)."""

    def archive(meeting: Meeting, folder: Path) -> Optional[Path]:
        s = settings()
        tracks = {p.stem: p for p in sorted(Layout.tracks_dir(folder).glob("*.ogg"))}
        shutil.rmtree(Layout.work_dir(folder), ignore_errors=True)
        existing = Layout.archive_path(folder, s.audio_retention)
        if not tracks:
            # Already archived (resume) or nothing captured; never fail the meeting for it.
            return existing if existing is not None and existing.exists() else None
        return build_archive(ffmpeg(), tracks, meeting.speakers, folder, s.audio_retention, s.audio_bitrate_kbps)

    return archive


@dataclass
class Stages:
    repo: Repository
    layout: Layout
    settings: Callable[[], Settings]
    transcriber: Transcriber
    analyzer: Analyzer
    catalogs: Callable[[], Iterable[ProjectCatalog]]
    sinks: Callable[[], Sequence[Sink]]
    archiver: Archiver
    progress: Optional[ProgressCb] = None
    last_results: dict[str, list[SinkResult]] = field(default_factory=dict)

    def folder(self, meeting: Meeting) -> Path:
        return self.layout.meeting_folder(meeting)

    def persist(self, meeting: Meeting) -> Meeting:
        """Index + ``meta.json`` together; the folder name is fixed on first persist."""
        folder = self.folder(meeting)
        if not meeting.folder:
            folder.mkdir(parents=True, exist_ok=True)
            meeting = replace(meeting, folder=self.layout.relative(folder))
        self.repo.save_meeting(meeting)
        write_meta(folder, meeting)
        return meeting

    def discard(self, meeting: Meeting) -> Meeting:
        """End a meeting in which no voice was captured (DESIGN §9, ``empty``).

        The folder and its ``meta.json`` stay (the row points at them and they cost nothing); the
        per-speaker tracks, the decoding scratch and any transcript are removed whatever
        ``audio.retention`` says: there is no speech in them worth keeping."""
        folder = self.folder(meeting)
        shutil.rmtree(Layout.tracks_dir(folder), ignore_errors=True)
        shutil.rmtree(Layout.work_dir(folder), ignore_errors=True)
        self.repo.replace_utterances(meeting.id, [])
        return self.persist(meeting.with_state(MeetingState.EMPTY))

    def run(self, stage: Stage, meeting: Meeting) -> Meeting:
        return {Stage.TRANSCRIBE: self.transcribe, Stage.ANALYZE: self.analyze,
                Stage.DELIVER: self.deliver, Stage.ARCHIVE: self.archive}[stage](meeting)

    # -- stages -------------------------------------------------------------------------------
    def transcribe(self, meeting: Meeting) -> Meeting:
        folder = self.folder(meeting)
        cb = (lambda track, frac: self.progress(meeting.id, track, frac)) if self.progress else None
        utterances = self.transcriber.transcribe(meeting, folder, cb)
        if not utterances:
            raise EmptyRecording("the transcription found no speech")
        write_transcript(folder, utterances)
        write_transcript_md(folder, meeting, utterances, self.settings().ui_language)
        self.repo.replace_utterances(meeting.id, utterances)
        return meeting

    def analyze(self, meeting: Meeting) -> Meeting:
        folder = self.folder(meeting)
        utterances = read_transcript(folder)
        if not utterances:  # never ask the LLM to summarise nothing (imports, older rows)
            raise EmptyRecording("the transcript has no lines")
        candidates, _errors = gather_candidates(self.catalogs(), meeting)
        notes = self.analyzer.analyze(meeting, utterances, candidates)
        # Rephrased titles must keep their ids, or a reprocess duplicates Kanban/Linear items.
        notes = replace(notes, action_items=tuple(reconcile_ids(self.repo.list_action_items(meeting.id),
                                                                notes.action_items)))
        resolver = ProjectResolver(self.settings().projects_min_confidence, self.repo)
        res = resolver.resolve(meeting, candidates, notes.project, notes.project_confidence)
        project = res.candidate.name if res.candidate else None
        notes = replace(notes, project=project, project_confidence=res.confidence if project else 0.0)
        meeting = replace(meeting, title=notes.meeting_title or meeting.title, project=project,
                          project_key=res.candidate.key if res.candidate else None,
                          language=notes.language or meeting.language)
        write_notes(folder, meeting, notes, notes.language or self.settings().ui_language)
        self.repo.sync_action_items(meeting.id, notes.action_items)
        return meeting

    def deliver(self, meeting: Meeting) -> Meeting:
        folder = self.folder(meeting)
        notes = read_notes(folder)
        if notes is None:
            raise StageError(f"notes.json missing in {folder}; reprocess from=analyze")
        results: list[SinkResult] = []
        digest = self._notes_digest(folder)
        done = self._done_sinks(meeting.id, digest)
        for sink in self.sinks():
            name = getattr(sink, "name", type(sink).__name__)
            try:
                if not sink.enabled():
                    continue
                if name in done:  # delivered this same content in an earlier try of this job
                    continue
                result = sink.deliver(meeting, notes, folder)
                results.append(result)
                if result.ok and not result.errors and not result.deferred:
                    done.add(name)
            except Exception as exc:  # one sink must not stop the others; the job retries
                results.append(SinkResult(getattr(sink, "name", type(sink).__name__), False,
                                          errors=(f"{type(exc).__name__}: {exc}",)))
        self.last_results[meeting.id] = results
        errors = [f"{r.sink}: {e}" for r in results for e in r.errors]
        self.repo.kv_set(SINKS_DONE_KV + meeting.id,
                         json.dumps({"notes": digest, "sinks": sorted(done)}) if errors else None)
        if errors:
            failed = [r for r in results if r.errors]
            if all(r.deferred or r.waiting for r in failed):
                raise StageDeferred("; ".join(errors), waiting=all(r.waiting for r in failed))
            raise StageError("; ".join(errors))
        return meeting

    @staticmethod
    def _notes_digest(folder: Path) -> str:
        try:
            return hashlib.sha256((folder / NOTES_JSON).read_bytes()).hexdigest()
        except OSError:
            return ""

    def _done_sinks(self, meeting_id: str, digest: str) -> set[str]:
        """Sinks that already delivered THIS notes content during the current job's retries, so a
        delivery waiting for Discord does not rewrite files or re-check claims every cycle. Forgotten
        when the job finishes, on an explicit reprocess and whenever the notes change."""
        raw = self.repo.kv_get(SINKS_DONE_KV + meeting_id)
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        if not digest or data.get("notes") != digest:
            return set()
        return {str(s) for s in data.get("sinks") or ()}

    def archive(self, meeting: Meeting) -> Meeting:
        self.archiver(meeting, self.folder(meeting))
        return meeting
