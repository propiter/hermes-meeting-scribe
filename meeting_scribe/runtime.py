"""Composition root. Builds adapters from a :class:`Host` (Hermes capabilities as plain callables)
so unit tests wire the whole plugin with fakes and ``register()`` stays tiny.

Multi-profile safety (DESIGN §1.4): settings are read on every call; the SQLite repository is
re-opened whenever ``plugin_data_dir`` resolves to a different path.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .analyze.extract import LlmAnalyzer
from .analyze.projects import CallableCatalog, LearnedCatalog
from .audio.ffmpeg import Ffmpeg, resolve_ffmpeg
from .commands import CaptureController
from .config import Settings, effective_owners
from .domain.models import ActionItem, Candidate, Meeting, Notes
from .domain.ports import ProjectCatalog, Sink
from .hermes_adapters import HermesStructuredLLM, linear_projects
from .pipeline.runner import PipelineRunner
from .pipeline.service import MeetingService
from .pipeline.stages import Stages, make_archiver
from .sinks.files import FilesSink
from .sinks.kanban import KanbanGateway, KanbanSink
from .sinks.linear import LinearBackend, LinearSink, select_backend
from .sinks.obsidian import ObsidianSink
from .storage.layout import Layout
from .storage.repo import Repository
from .transcribe.client import SubprocessTranscriber


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class Host:
    """Everything the plugin needs from Hermes. ``call_mcp()`` returns None unless the operator
    allowlisted the ``linear`` MCP server (re-evaluated per call; config can change at runtime)."""

    get_config: Callable[[str, Any], Any]
    set_config: Callable[[str, Any], None]
    data_dir: Callable[[], Path]
    llm: Callable[[], Any]
    secret: Callable[[str], Optional[str]]
    spawner: Callable[..., threading.Thread]
    call_mcp: Callable[[], Optional[Callable[[str, str, dict], Any]]]
    kanban: KanbanGateway
    project_sources: Callable[[], list[CallableCatalog]]
    llm_ready: Callable[[], tuple[bool, str]]


class Runtime:
    def __init__(self, host: Host) -> None:
        self.host = host
        self.clock = SystemClock()
        self.capture: Optional[CaptureController] = None
        self._extra_sinks: list[Sink] = []
        self._lock = threading.RLock()
        self._repo: Optional[Repository] = None
        self._repo_path: Optional[Path] = None
        self._service: Optional[MeetingService] = None

    # -- settings & simple adapters -------------------------------------------------------------
    def settings(self) -> Settings:
        return Settings.load(self.host.get_config)

    def owners(self) -> tuple[str, ...]:
        return effective_owners(self.settings(), self.host.secret)

    def ffmpeg(self) -> Ffmpeg:
        return resolve_ffmpeg(self.settings().audio_ffmpeg_path)

    def layout(self) -> Layout:
        return Layout(self.host.data_dir)

    def linear_backend(self) -> Optional[LinearBackend]:
        return select_backend(lambda: self.host.secret("LINEAR_API_KEY"), self.host.call_mcp())

    def repo(self) -> Repository:
        path = self.layout().db_path()
        with self._lock:
            if self._repo is None or self._repo_path != path:
                if self._repo is not None:
                    self.stop_pipeline()
                    self._repo.close()
                    self._service = None
                self._repo, self._repo_path = Repository(path), path
            return self._repo

    # -- projects & sinks -----------------------------------------------------------------------
    def catalogs(self) -> list[ProjectCatalog]:
        cats: list[ProjectCatalog] = list(self.host.project_sources())
        cats.append(LearnedCatalog(self.repo()))
        cats.append(CallableCatalog("linear", linear_projects(self.linear_backend)))
        return cats

    def project_for(self, meeting: Meeting, notes: Notes, item: ActionItem) -> Optional[Candidate]:
        """Item project, else meeting project, matched back to a candidate for sink routing."""
        from .analyze.projects import find_candidate, gather_candidates

        wanted = item.project or notes.project or meeting.project
        if not wanted:
            return None
        return find_candidate(wanted, gather_candidates(self.catalogs(), meeting)[0])

    def add_sink(self, sink: Sink) -> None:
        """Phase B registers the Discord notes sink here."""
        self._extra_sinks.append(sink)

    def item_sinks(self) -> dict[str, Any]:
        repo = self.repo()
        return {"kanban": KanbanSink(self.settings, repo, self.host.kanban, owners=self.owners,
                                     project_for=self.project_for),
                "linear": LinearSink(self.settings, repo, self.linear_backend, project_for=self.project_for)}

    def sinks(self) -> list[Sink]:
        items = self.item_sinks()
        return [FilesSink(self.settings), ObsidianSink(self.settings), items["kanban"], items["linear"],
                *self._extra_sinks]

    # -- pipeline -------------------------------------------------------------------------------
    def service(self) -> MeetingService:
        with self._lock:
            repo = self.repo()
            if self._service is None:
                stages = Stages(repo=repo, layout=self.layout(), settings=self.settings,
                                transcriber=SubprocessTranscriber(self.settings, self.ffmpeg),
                                analyzer=LlmAnalyzer(HermesStructuredLLM(self.host.llm), self.settings),
                                catalogs=self.catalogs, sinks=self.sinks,
                                archiver=make_archiver(self.settings, self.ffmpeg))
                runner = PipelineRunner(repo, stages, clock=self.clock, spawner=self.host.spawner)
                self._service = MeetingService(repo, self.layout(), runner, self.settings, clock=self.clock,
                                               item_sinks=self.item_sinks, catalogs=self.catalogs)
            return self._service

    def start_pipeline(self, live_meeting_ids: Iterable[str] = ()) -> None:
        self.service().runner.start(live_meeting_ids)

    def stop_pipeline(self) -> None:
        with self._lock:
            if self._service is not None:
                self._service.runner.stop()

    def pipeline_running(self) -> bool:
        return self._service is not None and self._service.runner.running

    # -- doctor / CLI surface -------------------------------------------------------------------
    def data_dir(self) -> Path:
        return self.host.data_dir()

    def kanban_boards(self) -> list[dict[str, Any]]:
        return self.host.kanban.list_boards()

    def llm_status(self) -> tuple[bool, str]:
        return self.host.llm_ready()

    def set_config(self, key: str, value: Any) -> None:
        self.host.set_config(key, value)

    def doctor_env(self) -> "Runtime":
        return self

    def ensure_pipeline(self) -> None:
        """Start the worker lazily in the gateway (commands call this); Phase B starts it on connect."""
        if not self.pipeline_running():
            live = self.capture.live_meeting_ids() if self.capture is not None else set()
            self.start_pipeline(live)

    def capture_status(self) -> tuple[bool, str]:
        if self.capture is None:
            return False, "live capture not installed (meeting_scribe.capture is Phase B); processing works"
        status = getattr(self.capture, "status", None)
        if not callable(status):
            return True, "capture controller installed"
        ok, detail = status()
        return bool(ok), str(detail)

    def close(self) -> None:
        with self._lock:
            self.stop_pipeline()
            if self._repo is not None:
                self._repo.close()
            self._repo = self._service = None
