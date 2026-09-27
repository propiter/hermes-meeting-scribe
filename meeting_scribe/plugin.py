"""Hermes wiring: ``register(ctx, plugin_root)`` builds the runtime and registers every surface.

Phase B hook points: after the core is wired, ``meeting_scribe.capture`` and
``meeting_scribe.discord_ui`` are imported and, if they expose ``install(ctx, runtime)``, it is
called. Missing or failing installs are logged and never break the core (processing, search,
tools, CLI keep working).
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from . import cli, hermes_adapters
from .analyze.projects import CallableCatalog
from .commands import Caller, MeetingCommands, caller_from_session
from .config import PRIMARY_COMMAND
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


def build_host(ctx: Any) -> Host:
    o = _host_overrides()

    def call_mcp() -> Optional[Callable[[str, str, dict], Any]]:
        return (lambda server, tool, args: ctx.call_mcp(server, tool, args)) if o["mcp_allowed"]() else None

    def project_sources() -> list[CallableCatalog]:
        return [CallableCatalog("hermes", hermes_adapters.hermes_projects),
                CallableCatalog("kanban", hermes_adapters.kanban_boards)]

    return Host(get_config=ctx.get_config, set_config=ctx.set_config, data_dir=o["data_dir"], llm=lambda: ctx.llm,
                secret=o["secret"], spawner=hermes_adapters.context_spawner, call_mcp=call_mcp,
                kanban=HermesKanban(), project_sources=project_sources, llm_ready=_llm_ready(ctx))


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


def _command_handler(commands: MeetingCommands, runtime: Runtime, name: str) -> Callable[[str], str]:
    def handler(raw_args: str) -> str:
        try:
            runtime.ensure_pipeline()
        except Exception:  # storage problems surface through the command reply below
            log.exception("meeting-scribe: pipeline start failed")
        return commands.handle(raw_args, caller_from_session(), name)
    return handler


def register(ctx: Any, plugin_root: Path) -> Runtime:
    ctx.register_auxiliary_task(hermes_adapters.AUX_TASK, display_name="Meeting Scribe",
                                description="Meeting notes, decisions and action items from transcripts")
    runtime = Runtime(build_host(ctx))
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

    commands = MeetingCommands(runtime.service(), runtime.settings, capture=lambda: runtime.capture)
    for name in (PRIMARY_COMMAND, *runtime.settings().commands_aliases):
        ctx.register_command(name, _command_handler(commands, runtime, name),
                             description="Meeting scribe: record, transcribe and summarise voice meetings",
                             args_hint="[start|stop|status|list|show|search|reprocess|link|project|config|help]")

    ctx.register_cli_command(name=PLUGIN_ID, help="Meeting scribe: setup, doctor, meetings",
                             setup_fn=cli.setup_parser, handler_fn=lambda args: cli.dispatch(args, runtime),
                             description="Configure and inspect the meeting-scribe plugin")
    ctx.register_skill(PLUGIN_ID, plugin_root / "skills" / PLUGIN_ID / "SKILL.md",
                       description="Search and use recorded meeting notes")
    _install_phase_b(ctx, runtime)
    return runtime
