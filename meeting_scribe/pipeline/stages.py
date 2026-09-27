"""Stage functions (DESIGN §9): captured → transcribe → analyze → deliver → archive.

Each stage reads its inputs from the meeting folder / index and writes its outputs atomically, so
re-running a stage (retry, resume, ``reprocess``) is safe. External side effects are the sinks',
which are idempotent through the ``deliveries`` table.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from ..analyze.projects import ProjectResolver, gather_candidates
from ..analyze.reconcile import reconcile_ids
from ..audio.archive import build_archive
from ..audio.ffmpeg import Ffmpeg
from ..config import Settings
from ..domain.models import Meeting, SinkResult, Stage
from ..domain.ports import Analyzer, ProjectCatalog, Sink, Transcriber
from ..storage.artifacts import (
    read_notes, read_transcript, write_meta, write_notes, write_transcript, write_transcript_md,
)
from ..storage.layout import Layout
from ..storage.repo import Repository

Archiver = Callable[[Meeting, Path], Optional[Path]]
ProgressCb = Callable[[str, str, float], None]  # meeting_id, track, fraction


class StageError(RuntimeError):
    """A stage finished with recoverable errors (e.g. one sink failed); the job is retried."""


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

    def run(self, stage: Stage, meeting: Meeting) -> Meeting:
        return {Stage.TRANSCRIBE: self.transcribe, Stage.ANALYZE: self.analyze,
                Stage.DELIVER: self.deliver, Stage.ARCHIVE: self.archive}[stage](meeting)

    # -- stages -------------------------------------------------------------------------------
    def transcribe(self, meeting: Meeting) -> Meeting:
        folder = self.folder(meeting)
        cb = (lambda track, frac: self.progress(meeting.id, track, frac)) if self.progress else None
        utterances = self.transcriber.transcribe(meeting, folder, cb)
        write_transcript(folder, utterances)
        write_transcript_md(folder, meeting, utterances, self.settings().ui_language)
        self.repo.replace_utterances(meeting.id, utterances)
        return meeting

    def analyze(self, meeting: Meeting) -> Meeting:
        folder = self.folder(meeting)
        utterances = read_transcript(folder)
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
        for sink in self.sinks():
            try:
                if not sink.enabled():
                    continue
                results.append(sink.deliver(meeting, notes, folder))
            except Exception as exc:  # one sink must not stop the others; the job retries
                results.append(SinkResult(getattr(sink, "name", type(sink).__name__), False,
                                          errors=(f"{type(exc).__name__}: {exc}",)))
        self.last_results[meeting.id] = results
        errors = [f"{r.sink}: {e}" for r in results for e in r.errors]
        if errors:
            raise StageError("; ".join(errors))
        return meeting

    def archive(self, meeting: Meeting) -> Meeting:
        self.archiver(meeting, self.folder(meeting))
        return meeting
