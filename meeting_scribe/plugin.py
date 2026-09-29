"""Hermes wiring: ``register(ctx, plugin_root)`` builds the runtime and registers every surface.

Phase B hook points: after the core is wired, ``meeting_scribe.capture`` and
``meeting_scribe.discord_ui`` are imported and, if they expose ``install(ctx, runtime)``, it is
called. Missing or failing installs are logged and never break the core (processing, search,
tools, CLI keep working).
"""
from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from . import cli, hermes_adapters, home
from .analyze.projects import CallableCatalog
from .commands import Caller, MeetingCommands, caller_from_session
from .config import PRIMARY_COMMAND
from .job_scope import owner_job_scope
from .runtime import Host, Runtime
from .sinks.kanban import HermesKanban
from .tools import SCHEMAS, TOOLSET, MeetingTools

log = logging.getLogger(__name__)
PLUGIN_ID = "meeting-scribe"
PHASE_B_MODULES = ("capture", "discord_ui")
RUNTIMES: dict[int, Runtime] = {}  # keyed by id(ctx): one runtime per plugin context (profile)

__all__ = ["register", "Caller", "caller_from_session", "RUNTIMES"]


def _mcp_allowed() -> bool:
    """True when the operator allowlisted the ``linear`` MCP server for this plugin."""
    from hermes_cli.config import load_config_readonly

    entry = ((load_config_readonly() or {}).get("plugins") or {}).get("entries", {}).get(PLUGIN_ID) or {}
    allow = entry.get("mcp_allowlist") if isinstance(entry, dict) else None
    return isinstance(allow, list) and "linear" in allow


def _host_overrides() -> dict[str, Any]:
    """Seam for tests: the Hermes-backed defaults for storage/secrets/MCP gate."""
    return {"data_dir": hermes_adapters.data_dir, "secret": hermes_adapters.secret, "mcp_allowed": _mcp_allowed}


def _llm_ready(ctx: Any) -> Callable[[], tuple[bool, str]]:
    def probe() -> tuple[bool, str]:
        try:
            res = ctx.llm.complete([{"role": "user", "content": "Reply with OK."}], max_tokens=5, timeout=60,
                                   task=hermes_adapters.AUX_TASK, purpose="meeting-scribe doctor")
        except Exception as exc:  # doctor reports; never raises
            return False, f"LLM call failed: {type(exc).__name__}: {exc}"
        return True, f"{getattr(res, 'provider', '?')}/{getattr(res, 'model', '?')} reachable"
    return probe


def build_host(ctx: Any, role: home.Role) -> Host:
    o = _host_overrides()

    def call_mcp() -> Optional[Callable[[str, str, dict], Any]]:
        return (lambda server, tool, args: ctx.call_mcp(server, tool, args)) if o["mcp_allowed"]() else None

    def project_sources() -> list[CallableCatalog]:
        return [CallableCatalog("hermes", hermes_adapters.hermes_projects),
                CallableCatalog("kanban", hermes_adapters.kanban_boards)]

    return Host(get_config=ctx.get_config, set_config=ctx.set_config, data_dir=o["data_dir"], llm=lambda: ctx.llm,
                secret=o["secret"], spawner=hermes_adapters.context_spawner, call_mcp=call_mcp,
                kanban=HermesKanban(), project_sources=project_sources, llm_ready=_llm_ready(ctx),
                is_gateway=hermes_adapters.is_gateway_process, llm_store=_llm_store, role=role.detail,
                job_scope=lambda: owner_job_scope(home.owner_home))


def _llm_store() -> Any:
    from .llm_config import HermesAuxStore

    return HermesAuxStore()


def _install_phase_b(ctx: Any, runtime: Runtime) -> None:
    for name in PHASE_B_MODULES:
        try:
            module = importlib.import_module(f"{__package__}.{name}")
        except ImportError as exc:
            log.warning("meeting-scribe: %s unavailable: %s", name, exc)
            continue
        install = getattr(module, "install", None)
        if not callable(install):
            log.debug("meeting-scribe: %s has no install(); skipping", name)
            continue
        try:
            install(ctx, runtime)
        except Exception:  # Phase B problems (e.g. discord.py missing) must not break the core
            log.exception("meeting-scribe: %s.install failed; live capture disabled", name)


def _membership(runtime: Runtime) -> Any:
    """The Discord membership check of the UI installed for ``runtime`` (``None`` without Discord)."""
    ui = sys.modules.get(f"{__package__}.discord_ui")
    fn = getattr(ui, "membership_for", None)
    return fn(runtime) if callable(fn) else None


def _command_handler(commands: MeetingCommands, runtime: Runtime, name: str) -> Callable[[str], str]:
    def handler(raw_args: str) -> str:
        try:
            runtime.ensure_pipeline()
        except Exception:  # storage problems surface through the command reply below
            log.exception("meeting-scribe: pipeline start failed")
        return commands.handle(raw_args, caller_from_session(), name)
    return handler


def _register_aux_task(ctx: Any) -> None:
    """Declare our auxiliary slot with neutral defaults (``auto`` = Hermes' main model, 600 s timeout).

    The user's ``auxiliary.meeting_scribe`` block always wins (Hermes layers these defaults under it);
    older Hermes without ``defaults=`` gets the bare registration."""
    from .llm_config import TASK_DEFAULTS

    kw = dict(display_name="Meeting Scribe", description="Meeting notes, decisions and action items from transcripts")
    try:
        ctx.register_auxiliary_task(hermes_adapters.AUX_TASK, defaults=dict(TASK_DEFAULTS), **kw)
    except TypeError:
        ctx.register_auxiliary_task(hermes_adapters.AUX_TASK, **kw)


def _profile_role() -> home.Role:
    """The role of the profile this plugin context was loaded for (Hermes binds discovery to it)."""
    from hermes_constants import get_hermes_home

    return home.role(get_hermes_home())


def _register_non_owner(ctx: Any, role: home.Role) -> None:
    """Another profile owns the plugin: no runtime, no worker, no capture, no tools or chat commands
    (they would write to this profile's own data). Only the CLI stays, to say where to go; the
    Desktop page is served by the dashboard backend, which always reads the owner's data."""
    log.info("meeting-scribe: %s", role.detail)

    def handler(args: Any) -> int:
        print(f"meeting-scribe: {role.detail}")
        return 0 if getattr(args, "ms_command", None) in (None, "doctor") else 2

    ctx.register_cli_command(name=PLUGIN_ID, help="Meeting scribe: setup, doctor, meetings",
                             setup_fn=cli.setup_parser, handler_fn=handler,
                             description="Configure and inspect the meeting-scribe plugin")


def register(ctx: Any, plugin_root: Path) -> Optional[Runtime]:
    role = _profile_role()
    if not role.owner:
        _register_non_owner(ctx, role)
        return None
    _register_aux_task(ctx)
    runtime = Runtime(build_host(ctx, role))
    RUNTIMES[id(ctx)] = runtime
    on_unload = getattr(ctx, "on_unload", None)
    if callable(on_unload):
        # Registered FIRST so it runs LAST (Hermes unwinds in reverse): after capture/UI teardown.
        def meeting_scribe_runtime_close() -> None:
            if RUNTIMES.get(id(ctx)) is runtime:
                del RUNTIMES[id(ctx)]
            runtime.close()  # stops the pipeline thread and closes SQLite (review W3)
        on_unload(meeting_scribe_runtime_close)

    tools = MeetingTools(runtime.service)
    ctx.register_tool("meeting_search", TOOLSET, SCHEMAS["meeting_search"], tools.search,
                      description=SCHEMAS["meeting_search"]["description"], emoji="🎙️")
    ctx.register_tool("meeting_get", TOOLSET, SCHEMAS["meeting_get"], tools.get,
                      description=SCHEMAS["meeting_get"]["description"], emoji="🎙️")

    commands = MeetingCommands(runtime.service, runtime.settings, capture=lambda: runtime.capture,
                               membership=lambda: _membership(runtime), owners=runtime.owners)
    for name in (PRIMARY_COMMAND, *runtime.settings().commands_aliases):
        ctx.register_command(name, _command_handler(commands, runtime, name),
                             description="Meeting notes: record a voice call and get its summary and tasks",
                             args_hint="[start|stop|status|list|show|search|reprocess|link|project|config|help]")

    ctx.register_cli_command(name=PLUGIN_ID, help="Meeting scribe: setup, doctor, meetings",
                             setup_fn=cli.setup_parser, handler_fn=lambda args: cli.dispatch(args, runtime),
                             description="Configure and inspect the meeting-scribe plugin")
    ctx.register_skill(PLUGIN_ID, plugin_root / "skills" / PLUGIN_ID / "SKILL.md",
                       description="Search and use recorded meeting notes")
    _install_phase_b(ctx, runtime)
    _start_in_gateway(runtime)
    return runtime


def _start_in_gateway(runtime: Runtime) -> None:
    """In the gateway, start the worker and the Google Meet poller right away.

    Without this they only started when Discord connected or a /meeting command ran, so a gateway
    without Discord (or before its first connect) never imported Meet transcripts. CLI/TUI
    processes start nothing (``ensure_pipeline`` checks ``is_gateway``); Discord connecting later
    adds capture ownership to the running worker instead of starting a second one.
    """
    try:
        runtime.ensure_pipeline()
    except Exception:  # storage problems: commands and doctor report them
        log.exception("meeting-scribe: pipeline start at register failed")
