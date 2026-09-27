"""REST API of the Desktop «Meetings» page, mounted by Hermes under ``/api/plugins/meeting-scribe``.

``dashboard/plugin_api.py`` only puts the plugin root on ``sys.path`` and re-exports ``router``.
Authentication is the host's: the web server's auth middleware guards every ``/api/`` route
(session token / OAuth gate) and its runtime gate 404s this namespace while the plugin is disabled.

Profile isolation: every request resolves the data dir and config through ``get_hermes_home()``
at call time. Desktop sends ``?profile=<name>`` when a profile shares the host backend; the handler
enters Hermes' own request scope for it (the same helper the core routes use). Mutating requests are
routed by Desktop to a backend already launched under the profile's HERMES_HOME.

This process never starts a pipeline, worker or capture: reads come from SQLite/files, settings go
through Hermes' config writer and ``reprocess`` is a queued command the gateway's worker executes.
"""
from __future__ import annotations

import contextlib
import mimetypes
import re
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ..llm_config import redact

router = APIRouter()
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
_MEETING_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
_PROFILE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


# -- seams (tests replace them; production resolves Hermes lazily per request) -------------------
def _default_data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir("meeting-scribe")


def _default_settings_store() -> Any:
    from .settings import hermes_settings_store

    return hermes_settings_store()


def _default_aux_store() -> Any:
    from ..llm_config import HermesAuxStore

    return HermesAuxStore()


def _default_secret(name: str) -> Optional[str]:
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        return None
    try:
        return get_secret(name)
    except Exception:  # an unscoped read in multi-profile hosting fails closed: treat as absent
        return None


@contextlib.contextmanager
def _default_profile_scope(profile: Optional[str]) -> Iterator[None]:
    if not profile:
        yield
        return
    from hermes_cli.web_server_profiles import _config_profile_scope  # the core routes' own scope

    with _config_profile_scope(profile):
        yield


SEAMS: dict[str, Callable[..., Any]] = {
    "data_dir": _default_data_dir, "settings_store": _default_settings_store, "aux_store": _default_aux_store,
    "secret": _default_secret, "profile_scope": _default_profile_scope, "kanban": lambda: None,
}


# -- helpers --------------------------------------------------------------------------------------
@contextlib.contextmanager
def _ctx(request: Request) -> Iterator[dict[str, Any]]:
    """Profile scope + an open repository for one request; domain errors become HTTP errors."""
    from ..storage.repo import Repository

    profile = (request.query_params.get("profile") or "").strip()
    if profile and (profile.lower() == "current"):
        profile = ""
    if profile and not _PROFILE.fullmatch(profile):
        raise HTTPException(400, "invalid profile")
    try:
        with SEAMS["profile_scope"](profile or None):
            root = Path(SEAMS["data_dir"]())
            repo = Repository(root / "index.sqlite")
            try:
                yield {"root": root, "repo": repo}
            finally:
                repo.close()
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(404, "not found") from exc
    except PermissionError as exc:
        raise HTTPException(403, redact(str(exc)) or "not allowed") from exc
    except ValueError as exc:
        raise HTTPException(400, redact(str(exc))) from exc


def _library(c: dict[str, Any]) -> Any:
    from .queries import Library

    return Library(c["repo"], c["root"])


def _mid(meeting_id: str) -> str:
    if not _MEETING_ID.fullmatch(meeting_id):
        raise HTTPException(404, "not found")
    return meeting_id


def _lang(value: str) -> str:
    return "es" if (value or "").lower().startswith("es") else "en"


# -- library --------------------------------------------------------------------------------------
@router.get("/v1/meetings")
def list_meetings(request: Request, q: str = Query("", max_length=200), source: str = "", state: str = "",
                  since: str = "", until: str = "", cursor: str = Query("", max_length=500),
                  limit: int = Query(30, ge=1, le=100)) -> dict[str, Any]:
    with _ctx(request) as c:
        lib = _library(c)
        out = lib.meetings(limit=limit, cursor=cursor, q=q, source=source, state=state, since=since, until=until)
        out["facets"] = lib.facets()
        return out


@router.get("/v1/meetings/{meeting_id}")
def get_meeting(request: Request, meeting_id: str) -> dict[str, Any]:
    with _ctx(request) as c:
        detail = _library(c).detail(_mid(meeting_id))
        audio = detail["audio"]
        if audio.get("available"):
            # The absolute path lets Desktop's own media player stream it (hermes-media:// locally,
            # /api/files/stream on a remote); ``stream_path`` is this API's Range-capable twin.
            audio["stream_path"] = f"/v1/meetings/{meeting_id}/audio"
        return detail


@router.get("/v1/meetings/{meeting_id}/transcript")
def get_transcript(request: Request, meeting_id: str, cursor: str = Query("", max_length=500),
                   limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    with _ctx(request) as c:
        return _library(c).transcript(_mid(meeting_id), limit=limit, cursor=cursor)


@router.api_route("/v1/meetings/{meeting_id}/audio", methods=["GET", "HEAD"])
def get_audio(request: Request, meeting_id: str) -> FileResponse:
    """The meeting's mixed recording, inline, with HTTP Range (Starlette ``FileResponse``).

    The file is chosen by the server from the meeting row (never from a client path) and must be a
    regular, non-symlinked file inside the meeting's folder."""
    with _ctx(request) as c:
        lib = _library(c)
        path = lib.artifact(_mid(meeting_id), "recording.ogg")
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, "no audio for this meeting")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "audio/ogg",
                            content_disposition_type="inline", filename=path.name)


# -- reprocess (queued for the gateway) -----------------------------------------------------------
@router.post("/v1/meetings/{meeting_id}/commands")
def submit_command(request: Request, meeting_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """``{"request_id", "action": "reprocess", "stage": "transcribe|analyze|deliver", "confirm": true}``.
    ``confirm`` must be literally ``true``: the page asks the operator before sending it."""
    if body.get("confirm") is not True:
        raise HTTPException(400, "confirmation required")
    rid = str(body.get("request_id") or "")
    if not _REQUEST_ID.fullmatch(rid):
        raise HTTPException(400, "invalid request id")
    command = {k: v for k, v in body.items() if k not in ("request_id", "confirm")}
    from .control import Commands

    with _ctx(request) as c:
        return Commands(c["repo"]).submit(rid, _mid(meeting_id), command)


@router.get("/v1/commands/{request_id}")
def get_command(request: Request, request_id: str) -> dict[str, Any]:
    if not _REQUEST_ID.fullmatch(request_id):
        raise HTTPException(404, "not found")
    from .control import Commands

    with _ctx(request) as c:
        return Commands(c["repo"]).get(request_id)


@router.post("/v1/commands/{request_id}/acknowledge")
def acknowledge_command(request: Request, request_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Acknowledge an uncertain outcome after inspecting results; does not re-execute it."""
    if body.get("confirm") is not True:
        raise HTTPException(400, "confirmation required")
    if not _REQUEST_ID.fullmatch(request_id):
        raise HTTPException(404, "not found")
    from .control import Commands

    with _ctx(request) as c:
        return Commands(c["repo"]).acknowledge(request_id)


# -- status / google / doctor ---------------------------------------------------------------------
@router.get("/v1/status")
def get_status(request: Request) -> dict[str, Any]:
    from .queries import google_status

    with _ctx(request) as c:
        out = _library(c).status()
        settings = SEAMS["settings_store"]().settings()
        out["google"] = google_status(c["root"], c["repo"], settings.google_meet_enabled)
        out["settings_warnings"] = [redact(w) for w in settings.warnings]
        return out


@router.get("/v1/doctor")
def get_doctor(request: Request) -> dict[str, Any]:
    from .settings import DashboardDoctorEnv, run_doctor

    with _ctx(request) as c:
        env = DashboardDoctorEnv(store=SEAMS["settings_store"](), root=c["root"], repo=c["repo"],
                                 secret=SEAMS["secret"], kanban=SEAMS["kanban"](), aux_store=SEAMS["aux_store"]())
        return run_doctor(env)


# -- settings -------------------------------------------------------------------------------------
@router.get("/v1/settings")
def get_settings(request: Request, lang: str = "en") -> dict[str, Any]:
    from .settings import llm_view, settings_view

    with _ctx(request):
        out = settings_view(SEAMS["settings_store"](), _lang(lang))
        out["llm"] = llm_view(SEAMS["aux_store"]())
        return out


@router.put("/v1/settings/{key}")
def put_setting(request: Request, key: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """``{"value": ...}``; validated exactly like ``hermes meeting-scribe config set``."""
    if "value" not in body:
        raise HTTPException(400, "value is required")
    from .settings import set_setting

    with _ctx(request) as c:
        try:
            return set_setting(SEAMS["settings_store"](), c["repo"], key, body["value"])
        except KeyError as exc:
            raise HTTPException(400, f"unknown setting {key}") from exc


@router.put("/v1/llm")
def put_llm(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    from .settings import llm_update

    with _ctx(request):
        return llm_update(SEAMS["aux_store"](), body)


def reset_seams() -> None:  # tests
    SEAMS.update({"data_dir": _default_data_dir, "settings_store": _default_settings_store,
                  "aux_store": _default_aux_store, "secret": _default_secret,
                  "profile_scope": _default_profile_scope, "kanban": lambda: None})


__all__ = ["router", "SEAMS", "reset_seams"]
