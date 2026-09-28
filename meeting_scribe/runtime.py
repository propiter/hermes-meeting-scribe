"""Composition root. Builds adapters from a :class:`Host` (Hermes capabilities as plain callables)
so unit tests wire the whole plugin with fakes and ``register()`` stays tiny.

Multi-profile safety (DESIGN §1.4, §15): settings are read on every call; repositories (and the
service built on them) are kept PER database path. When ``plugin_data_dir`` resolves elsewhere the
runtime switches to (or opens) that path's repository — it never closes one another caller may
still hold (review finding 7). All of them are closed by :meth:`close`.

Spaces (DESIGN §23): ``settings(space)`` layers a space's overrides over the global settings; every
meeting-scoped adapter asks for its meeting's space. Opening a database with no space yet creates
the first one from the existing setup (see :func:`meeting_scribe.spaces.bootstrap`).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional, Sequence

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
from .spaces import Spaces, bootstrap
from .sinks.files import FilesSink
from .sinks.kanban import KanbanGateway, KanbanSink
from .sinks.linear import LinearBackend, LinearSink, select_backend
from .sinks.obsidian import ObsidianSink
from .storage.layout import Layout
from .storage.repo import Repository
from .transcribe.client import SubprocessTranscriber

if TYPE_CHECKING:
    from .google.importer import MeetImporter, MeetPoller
    from .google.oauth import GoogleCredentials, GoogleFiles


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
    is_gateway: Callable[[], bool] = lambda: True
    llm_store: Callable[[], Any] = lambda: None  # ``llm_config.AuxStore`` over Hermes' config (None in tests)


class Runtime:
    def __init__(self, host: Host) -> None:
        self.host = host
        self.clock = SystemClock()
        self.capture: Optional[CaptureController] = None
        self._extra_sinks: list[Sink] = []
        self._extra_catalogs: list[ProjectCatalog] = []
        self._lock = threading.RLock()
        self._repos: dict[Path, Repository] = {}
        self._services: dict[Path, MeetingService] = {}
        self._repo_path: Optional[Path] = None
        self._meet_pollers: dict[str, "MeetPoller"] = {}
        self._pollers_checked = 0.0  # monotonic time of the last reconciliation of the Meet pollers
        self.google_transport: Any = None  # tests inject a fake HTTP transport here (never the network)

    # -- settings & simple adapters -------------------------------------------------------------
    def spaces(self) -> Spaces:
        return Spaces(self.repo, self.host.get_config, self.host.data_dir)

    def settings(self, space: Optional[str] = None) -> Settings:
        """The global settings; with ``space``, that space's overrides on top."""
        if not space:
            return Settings.load(self.host.get_config)
        return self.spaces().settings(space)

    def owners(self, space: Optional[str] = None) -> tuple[str, ...]:
        return effective_owners(self.settings(space), self.host.secret)

    def space_of_guild(self, guild: Any) -> Optional[str]:
        """The space that owns a Discord server (``guild`` object or id); ``None``: not recorded.

        One space: an unowned server joins it (the single-team behaviour before spaces). Several
        spaces: only servers assigned to a space are recorded or published to."""
        gid = getattr(guild, "id", guild)
        return self.spaces().claim_guild(gid, str(getattr(guild, "name", "") or ""))

    def adopt_guilds(self, guilds: Iterable[Any]) -> list[str]:
        """At every Discord connect: remember the bot's servers (doctor and the Desktop list the
        unassigned ones) and, the first time after bootstrap, give them to the ``main`` space."""
        pairs = [(str(getattr(g, "id", g)), str(getattr(g, "name", "") or "")) for g in guilds]
        pairs = [p for p in pairs if p[0].isdigit()]
        repo = self.repo()
        if pairs:
            repo.set_bot_guilds(pairs)
        return repo.adopt_guilds("main", pairs)

    def space_guilds(self, space: str) -> Optional[frozenset[str]]:
        """The Discord servers a meeting of ``space`` may be published to. ``None`` with a single
        space (any server of the bot, as before spaces); with several, only the space's own."""
        spaces = self.spaces().all()
        if len(spaces) <= 1:
            return None
        return next((frozenset(s.guild_ids) for s in spaces if s.slug == space), frozenset())

    def default_space(self) -> str:
        """The only space of the install; ``SpaceError`` when there are several (choose one)."""
        return self.spaces().resolve(None).slug

    def ffmpeg(self) -> Ffmpeg:
        return resolve_ffmpeg(self.settings().audio_ffmpeg_path)

    def layout(self) -> Layout:
        return Layout(self.host.data_dir)

    def linear_backend(self) -> Optional[LinearBackend]:
        return select_backend(lambda: self.host.secret("LINEAR_API_KEY"), self.host.call_mcp())

    def repo(self) -> Repository:
        path = self.layout().db_path()
        with self._lock:
            if path != self._repo_path and self._repo_path in self._services:
                self._services[self._repo_path].runner.stop()  # one worker, for the active profile
            repo = self._repos.get(path)
            if repo is None:
                repo = self._repos[path] = Repository(path)
                bootstrap(repo, Settings.load(self.host.get_config), path.parent)
            self._repo_path = path
            return repo

    @property
    def _service(self) -> Optional[MeetingService]:
        return self._services.get(self._repo_path) if self._repo_path is not None else None

    # -- projects & sinks -----------------------------------------------------------------------
    def catalogs(self) -> list[ProjectCatalog]:
        cats: list[ProjectCatalog] = list(self.host.project_sources())
        cats.append(LearnedCatalog(self.repo()))
        cats.append(CallableCatalog("linear", linear_projects(self.linear_backend)))
        cats.extend(self._extra_catalogs)
        return cats

    def add_catalog(self, catalog: ProjectCatalog) -> None:
        """Phase B adds the guild's Discord channels as project candidates (DESIGN §16)."""
        self._extra_catalogs.append(catalog)

    def project_for(self, meeting: Meeting, notes: Notes, item: ActionItem, *,
                    sources: Optional[Sequence[str]] = None) -> Optional[Candidate]:
        """Item project, else meeting project, matched back to a candidate of ``sources``.

        The meeting's resolved ``project_key`` is authoritative when the item did not name a
        different project (review finding 5)."""
        from .analyze.projects import gather_candidates, route_candidate
        from .domain.text import fold

        wanted = item.project or notes.project or meeting.project
        if not wanted:
            return None
        same_as_meeting = not item.project or fold(item.project) == fold(meeting.project or "")
        key = meeting.project_key if same_as_meeting else None
        return route_candidate(wanted, key, gather_candidates(self.catalogs(), meeting)[0], sources)

    def project_router(self, sources: Sequence[str]) -> Callable[[Meeting, Notes, ActionItem], Optional[Candidate]]:
        def route(meeting: Meeting, notes: Notes, item: ActionItem) -> Optional[Candidate]:
            return self.project_for(meeting, notes, item, sources=sources)
        return route

    def add_sink(self, sink: Sink) -> None:
        """Phase B registers the Discord notes sink here."""
        self._extra_sinks.append(sink)

    def item_sinks(self) -> dict[str, Any]:
        repo = self.repo()
        return {"kanban": KanbanSink(self.settings, repo, self.host.kanban, owners=self.owners,
                                     project_for=self.project_router(("hermes", "kanban"))),
                "linear": LinearSink(self.settings, repo, self.linear_backend,
                                     project_for=self.project_router(("linear",)))}

    def sinks(self) -> list[Sink]:
        items = self.item_sinks()
        return [FilesSink(self.settings), ObsidianSink(self.settings), items["kanban"], items["linear"],
                *self._extra_sinks]

    # -- pipeline -------------------------------------------------------------------------------
    def service(self) -> MeetingService:
        with self._lock:
            repo = self.repo()
            path = self._repo_path
            assert path is not None
            if path not in self._services:
                stages = Stages(repo=repo, layout=self.layout(), settings=self.settings,
                                transcriber=SubprocessTranscriber(self.settings, self.ffmpeg),
                                analyzer=LlmAnalyzer(HermesStructuredLLM(
                                    self.host.llm, timeout=lambda: self.settings().analysis_timeout_seconds,
                                    max_tokens=lambda: self.settings().analysis_max_tokens), self.settings),
                                catalogs=self.catalogs, sinks=self.sinks,
                                archiver=make_archiver(self.settings, self.ffmpeg))
                runner = PipelineRunner(repo, stages, clock=self.clock, spawner=self.host.spawner,
                                        max_attempts=lambda: self.settings().pipeline_max_attempts,
                                        workers=lambda: self.settings().pipeline_workers,
                                        max_transcriptions=lambda: self.settings().pipeline_max_transcriptions)
                self._services[path] = MeetingService(repo, self.layout(), runner, self.settings, clock=self.clock,
                                                      item_sinks=self.item_sinks, catalogs=self.catalogs)
                self._wire_desktop(self._services[path])
            return self._services[path]

    def _wire_desktop(self, service: MeetingService) -> None:
        """The gateway's worker is the only executor of Desktop commands (see ``desktop.control``);
        its pulse also keeps one Meet poller per space."""
        from .desktop import control

        service.runner.control = lambda: control.execute_one(service)
        service.runner.pulse = lambda: self._pulse(service)

    POLLER_RECONCILE_SECONDS = 60.0  # the worker pulses every few seconds; spaces change rarely

    def _pulse(self, service: MeetingService) -> None:
        from .desktop import control

        control.pulse(service.repo)
        self.reconcile_meet_pollers()

    def reconcile_meet_pollers(self, *, force: bool = False) -> bool:
        """Start/stop pollers for spaces created/deleted elsewhere, at most once per
        ``POLLER_RECONCILE_SECONDS`` (it lists the spaces: a DB read the worker must not do on every
        tick). True when it ran."""
        now = time.monotonic()
        with self._lock:
            if not force and now - self._pollers_checked < self.POLLER_RECONCILE_SECONDS:
                return False
            self._pollers_checked = now
        self.start_meet_pollers()
        return True

    def start_pipeline(self, live_meeting_ids: Iterable[str] = (), *, owns_capture: bool = False) -> None:
        self.service().runner.start(live_meeting_ids, owns_capture=owns_capture)
        self.reconcile_meet_pollers(force=True)

    def stop_pipeline(self) -> None:
        # Workers first: their pulse reconciles the pollers and could start one again after the
        # pollers were stopped. Joined outside the lock (see close()).
        with self._lock:
            runners = [svc.runner for svc in self._services.values()]
        for runner in runners:
            runner.stop()
        with self._lock:
            pollers, self._meet_pollers = list(self._meet_pollers.values()), {}
        for poller in pollers:
            poller.stop()

    # -- Google Meet import (DESIGN §17, §23: one connection per space) ---------------------------
    # ``space=None``: the install's only space; with several, ``SpaceError`` — never another team's
    # connection by accident (the CLI passes ``--space``, doctor walks every space).
    def google_files(self, space: Optional[str] = None) -> "GoogleFiles":
        from .google.oauth import GoogleFiles

        return GoogleFiles(self.host.data_dir, space or self.default_space())

    def google_credentials(self, space: Optional[str] = None) -> "GoogleCredentials":
        from .google.oauth import GoogleCredentials

        return GoogleCredentials(self.google_files(space), transport=self.google_transport)

    def google_connected_at(self, space: Optional[str] = None) -> Optional[float]:
        token = self.google_files(space).read_token() or {}
        value = token.get("connected_at")
        return float(value) if isinstance(value, (int, float)) else None

    def meet_importer(self, space: Optional[str] = None) -> "MeetImporter":
        from .google.importer import MeetImporter
        from .google.meet_api import MeetClient

        space = space or self.default_space()
        return MeetImporter(space=space, service=self.service,
                            client=lambda: MeetClient(self.google_credentials(space)), clock=self.clock.now)

    def start_meet_pollers(self) -> None:
        """Gateway only (callers already are): one polling thread PER SPACE, each syncing only with
        its space's lease. Idempotent — called again on every worker pulse, so a space created or
        deleted from the CLI or the Desktop gets (or loses) its poller without a restart."""
        from .google.importer import MeetPoller

        slugs = {s.slug for s in self.spaces().all()}
        with self._lock:
            gone = [p for slug, p in self._meet_pollers.items() if slug not in slugs]
            self._meet_pollers = {slug: p for slug, p in self._meet_pollers.items() if slug in slugs}
            owner = self.service().runner.owner
            for slug in sorted(slugs):
                current = self._meet_pollers.get(slug)
                if current is not None and current.running:
                    continue
                poller = MeetPoller(space=slug, importer=lambda s=slug: self.meet_importer(s), repo=self.repo,
                                    settings=lambda s=slug: self.settings(s),
                                    connected_at=lambda s=slug: self.google_connected_at(s),
                                    owner=owner, spawner=self.host.spawner)
                self._meet_pollers[slug] = poller
                poller.start()
        for poller in gone:
            poller.stop()

    def meet_poller_running(self, space: Optional[str] = None) -> bool:
        """``space=None``: whether any space's poller runs."""
        if space is None:
            return any(p.running for p in list(self._meet_pollers.values()))
        poller = self._meet_pollers.get(space)
        return poller is not None and poller.running

    def pipeline_running(self) -> bool:
        svc = self._service
        return svc is not None and svc.runner.running

    # -- doctor / CLI surface -------------------------------------------------------------------
    def data_dir(self) -> Path:
        return self.host.data_dir()

    def kanban_boards(self) -> list[dict[str, Any]]:
        return self.host.kanban.list_boards()

    def llm_status(self) -> tuple[bool, str]:
        return self.host.llm_ready()

    def set_config(self, key: str, value: Any) -> None:
        self.host.set_config(key, value)

    def config_origin(self, key: str) -> str:
        """``configured`` when the user set ``key`` (flat or legacy spelling), else ``default``."""
        from .config import LEGACY_KEYS

        missing = object()
        for name in (key, LEGACY_KEYS.get(key)):
            if name and self.host.get_config(name, missing) not in (missing, None):
                return "configured"
        return "default"

    def llm_store(self) -> Any:
        return self.host.llm_store()

    def doctor_env(self) -> "Runtime":
        return self

    def ensure_pipeline(self) -> None:
        """Start the worker lazily IN THE GATEWAY (commands call this); Phase B starts it on connect.

        A CLI/TUI ``hermes chat`` process must not start a second worker nor run ``recover``
        (review finding 2): its commands only read/enqueue, and the gateway's worker picks work up.
        Recording rows are never closed from here (``owns_capture`` stays False).
        """
        if not self.host.is_gateway() or self.pipeline_running():
            return
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
        # Threads first and WITHOUT the runtime lock: the poller/worker may be waiting for it inside
        # ``repo()``; joining them while holding it would time out and leave them on a closed DB.
        self.stop_pipeline()
        with self._lock:
            self.stop_pipeline()  # anything started meanwhile
            for repo in self._repos.values():
                repo.close()
            self._repos.clear()
            self._services.clear()
            self._repo_path = None
