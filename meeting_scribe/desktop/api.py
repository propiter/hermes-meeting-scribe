"""REST API of the Desktop «Meetings» page, mounted by Hermes under ``/api/plugins/meeting-scribe``.

``dashboard/plugin_api.py`` only puts the plugin root on ``sys.path`` and re-exports ``router``.
Authentication is the host's: the web server's auth middleware guards every ``/api/`` route
(session token / OAuth gate) and its runtime gate 404s this namespace while the plugin is disabled.

Owner profile (DESIGN §1.5): the data dir and config always resolve to the profile that owns the
installation (``meeting_scribe.home``), whatever profile Desktop was launched with and whatever
``?profile=`` it adds for its active profile, so the page shows the same library everywhere. Each
request enters Hermes' own request scope for the owner (the helper the core routes use), which also
scopes secrets to it. An owner that cannot be resolved answers 503 with the reason.

This process never starts a pipeline, worker or capture: reads come from SQLite/files, settings go
through Hermes' config writer and ``reprocess`` is a queued command the owner's gateway worker runs.
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


# -- seams (tests replace them; production resolves Hermes lazily per request) -------------------
def _default_data_dir() -> Path:
    from .. import home

    return home.data_dir()


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
def _default_owner_scope() -> Iterator[None]:
    from .. import home

    with home.owner_scope():
        yield


def _default_owner() -> str:
    from .. import home

    return home.owner().describe()


_DEFAULT_SEAMS: dict[str, Callable[..., Any]] = {
    "data_dir": _default_data_dir, "settings_store": _default_settings_store, "aux_store": _default_aux_store,
    "secret": _default_secret, "owner_scope": _default_owner_scope, "kanban": lambda: None,
    "owner": _default_owner,
}
SEAMS: dict[str, Callable[..., Any]] = dict(_DEFAULT_SEAMS)


# -- helpers --------------------------------------------------------------------------------------
SpaceParam = Query("", max_length=40, description="the space (team) to read; optional with one space")


@contextlib.contextmanager
def _ctx(request: Request) -> Iterator[dict[str, Any]]:
    """The OWNER profile's scope + an open repository for one request; domain errors become HTTP errors.

    The ``?profile=`` Desktop adds for its active profile is deliberately not used: the plugin's data
    and settings belong to the owner profile (``meeting_scribe.home``), so every profile shows the
    same library."""
    from ..home import OwnerError
    from ..spaces import SpaceError, bootstrap
    from ..storage.repo import Repository

    try:
        with SEAMS["owner_scope"]():
            root = Path(SEAMS["data_dir"]())
            repo = Repository(root / "index.sqlite")
            try:
                if not repo.list_spaces():  # the gateway normally did it; idempotent under its lock
                    bootstrap(repo, SEAMS["settings_store"]().settings(), root)
                yield {"root": root, "repo": repo}
            finally:
                repo.close()
    except HTTPException:
        raise
    except OwnerError as exc:  # a broken owner_profile: say so, never serve another profile's data
        raise HTTPException(503, redact(str(exc))) from exc
    except KeyError as exc:
        raise HTTPException(404, "not found") from exc
    except PermissionError as exc:
        raise HTTPException(403, redact(str(exc)) or "not allowed") from exc
    except (SpaceError, ValueError) as exc:
        raise HTTPException(400, redact(str(exc))) from exc


def _space(c: dict[str, Any], wanted: str, *, required: bool = True) -> Optional[str]:
    """The space a data request reads (DESIGN §23): ``?space=`` when given (unknown: 404), else the
    install's only space; with several and none given, 409 (``required=False``: ``None``). Never a
    library mixing two teams."""
    rows = c["repo"].list_spaces()
    if wanted:
        if not any(r.slug == wanted for r in rows):
            raise HTTPException(404, f"there is no space called {wanted!r}")
        return wanted
    if len(rows) == 1:
        return rows[0].slug
    if required:
        raise HTTPException(409, "this installation has several spaces: say which one with ?space=<id> "
                                 "(the list is at /v1/spaces)")
    return None


def _library(c: dict[str, Any], wanted: str) -> Any:
    from .queries import Library

    return Library(c["repo"], c["root"], _space(c, wanted))


def _command_in_space(c: dict[str, Any], request_id: str, wanted: str) -> None:
    """A command is visible only through its meeting's space (unknown ids stay a plain 404)."""
    row = c["repo"]._x("SELECT meeting_id FROM desktop_commands WHERE id=?", (request_id,)).fetchone()
    lib = _library(c, wanted)
    if row is not None:
        lib.require(row["meeting_id"])


def _spaces(c: dict[str, Any]) -> Any:
    from ..spaces import Spaces

    return Spaces(lambda: c["repo"], SEAMS["settings_store"]()._lookup, lambda: c["root"])


def _space_or_404(c: dict[str, Any], slug: str) -> Any:
    space = _spaces(c).get(slug)
    if space is None:
        raise HTTPException(404, f"there is no space called {slug!r}")
    return space


def _space_view(c: dict[str, Any], space: Any) -> dict[str, Any]:
    from .queries import google_status, google_summary

    repo = c["repo"]
    settings = _spaces(c).settings(space.slug)
    counts = {r["state"]: int(r["n"]) for r in repo._x(
        "SELECT state, COUNT(*) AS n FROM meetings WHERE space=? GROUP BY state", (space.slug,)).fetchall()}
    return {"slug": space.slug, "name": space.name,
            "guilds": [{"id": g, "name": n} for g, n in space.guilds],
            "google": google_summary(google_status(c["root"], repo, settings.google_meet_enabled, space.slug)),
            "counts": {"meetings": sum(counts.values()), "by_state": counts}}


def _mid(meeting_id: str) -> str:
    if not _MEETING_ID.fullmatch(meeting_id):
        raise HTTPException(404, "not found")
    return meeting_id


def _lang(value: str) -> str:
    return "es" if (value or "").lower().startswith("es") else "en"


# -- library --------------------------------------------------------------------------------------
@router.get("/v1/meetings")
def list_meetings(request: Request, q: str = Query("", max_length=200), source: str = "", state: str = "",
                  since: str = "", until: str = "", channel: str = Query("", max_length=200),
                  project: str = Query("", max_length=200), cursor: str = Query("", max_length=500),
                  limit: int = Query(30, ge=1, le=100), space: str = SpaceParam) -> dict[str, Any]:
    with _ctx(request) as c:
        lib = _library(c, space)
        out = lib.meetings(limit=limit, cursor=cursor, q=q, source=source, state=state, since=since, until=until,
                           channel=channel, project=project)
        out["facets"] = lib.facets()
        return out


@router.get("/v1/meetings/{meeting_id}")
def get_meeting(request: Request, meeting_id: str, space: str = SpaceParam) -> dict[str, Any]:
    with _ctx(request) as c:
        lib = _library(c, space)
        detail = lib.detail(_mid(meeting_id))
        settings = _spaces(c).settings(lib.space)
        # Which task destinations are on: "enviada a Kanban" and "enviada a Linear" are separate facts,
        # and a destination that is off must not read as "pending" forever.
        detail["destinations"] = {"discord": True, "kanban": settings.kanban_mode, "linear": settings.linear_mode,
                                  "kanban_board": settings.kanban_board}
        audio = detail["audio"]
        if audio.get("available"):
            # The absolute path lets Desktop's own media player stream it (hermes-media:// locally,
            # /api/files/stream on a remote); ``stream_path`` is this API's Range-capable twin.
            audio["stream_path"] = f"/v1/meetings/{meeting_id}/audio"
        return detail


@router.get("/v1/meetings/{meeting_id}/transcript")
def get_transcript(request: Request, meeting_id: str, cursor: str = Query("", max_length=500),
                   limit: int = Query(200, ge=1, le=500), space: str = SpaceParam) -> dict[str, Any]:
    with _ctx(request) as c:
        return _library(c, space).transcript(_mid(meeting_id), limit=limit, cursor=cursor)


@router.api_route("/v1/meetings/{meeting_id}/audio", methods=["GET", "HEAD"])
def get_audio(request: Request, meeting_id: str, space: str = SpaceParam) -> FileResponse:
    """The meeting's mixed recording, inline, with HTTP Range (Starlette ``FileResponse``).

    The file is chosen by the server from the meeting row (never from a client path) and must be a
    regular, non-symlinked file inside the meeting's folder."""
    with _ctx(request) as c:
        lib = _library(c, space)
        mid = _mid(meeting_id)
        path = lib.artifact(mid, "playback.ogg")  # the listening copy of a multitrack archive
        if not path.is_file() or path.is_symlink():
            path = lib.artifact(mid, "recording.ogg")
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, "no audio for this meeting")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "audio/ogg",
                            content_disposition_type="inline", filename=path.name)


# -- reprocess (queued for the gateway) -----------------------------------------------------------
@router.post("/v1/meetings/{meeting_id}/commands")
def submit_command(request: Request, meeting_id: str, body: dict[str, Any] = Body(...),
                   space: str = SpaceParam) -> dict[str, Any]:
    """``{"request_id", "action": "reprocess", "stage": "transcribe|analyze|deliver", "confirm": true}``
    or ``{"request_id", "action": "prepare_audio", "confirm": true}`` (write the listening copy).
    ``confirm`` must be literally ``true``: the page asks the operator before sending it."""
    if body.get("confirm") is not True:
        raise HTTPException(400, "confirmation required")
    rid = str(body.get("request_id") or "")
    if not _REQUEST_ID.fullmatch(rid):
        raise HTTPException(400, "invalid request id")
    command = {k: v for k, v in body.items() if k not in ("request_id", "confirm")}
    from .control import Commands

    with _ctx(request) as c:
        _library(c, space).require(_mid(meeting_id))  # a meeting of this space only (404 otherwise)
        return Commands(c["repo"]).submit(rid, _mid(meeting_id), command)


@router.get("/v1/commands/{request_id}")
def get_command(request: Request, request_id: str, space: str = SpaceParam) -> dict[str, Any]:
    if not _REQUEST_ID.fullmatch(request_id):
        raise HTTPException(404, "not found")
    from .control import Commands

    with _ctx(request) as c:
        _command_in_space(c, request_id, space)
        return Commands(c["repo"]).get(request_id)


@router.post("/v1/commands/{request_id}/acknowledge")
def acknowledge_command(request: Request, request_id: str, body: dict[str, Any] = Body(...),
                        space: str = SpaceParam) -> dict[str, Any]:
    """Acknowledge an uncertain outcome after inspecting results; does not re-execute it."""
    if body.get("confirm") is not True:
        raise HTTPException(400, "confirmation required")
    if not _REQUEST_ID.fullmatch(request_id):
        raise HTTPException(404, "not found")
    from .control import Commands

    with _ctx(request) as c:
        _command_in_space(c, request_id, space)
        return Commands(c["repo"]).acknowledge(request_id)


# -- status / google / doctor ---------------------------------------------------------------------
@router.get("/v1/status")
def get_status(request: Request, space: str = SpaceParam) -> dict[str, Any]:
    """The bot (worker heartbeat) and the processing queue are the machine's; jobs, commands and
    Google are the space's. With several spaces and no ``?space=``: only the machine's part."""
    from .queries import Library, google_status

    with _ctx(request) as c:
        slug = _space(c, space, required=False)
        out = Library(c["repo"], c["root"], slug).status()
        machine = Library(c["repo"], c["root"]).status()
        out["worker"] = machine["worker"]
        out["queue"] = machine["counts"]
        if slug is None:
            out.update({"counts": machine["counts"], "jobs": [], "waiting_destination": [], "dm_notes": [],
                        "commands": []})
        settings = _spaces(c).settings(slug)
        out["space"] = slug
        out["google"] = None if slug is None else google_status(c["root"], c["repo"], settings.google_meet_enabled, slug)
        out["settings_warnings"] = [redact(w) for w in settings.warnings]
        return out


@router.get("/v1/doctor")
def get_doctor(request: Request) -> dict[str, Any]:
    from .settings import DashboardDoctorEnv, run_doctor

    with _ctx(request) as c:
        env = DashboardDoctorEnv(store=SEAMS["settings_store"](), root=c["root"], repo=c["repo"],
                                 secret=SEAMS["secret"], kanban=SEAMS["kanban"](), aux_store=SEAMS["aux_store"](),
                                 owner=SEAMS["owner"]())
        return run_doctor(env)


# -- spaces and servers ---------------------------------------------------------------------------
@router.get("/v1/spaces")
def list_spaces(request: Request) -> dict[str, Any]:
    with _ctx(request) as c:
        return {"items": [_space_view(c, sp) for sp in _spaces(c).all()]}


@router.post("/v1/spaces")
def create_space(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """``{"name": "Team", "slug"?: "team"}``; 409 when the slug is taken."""
    from ..spaces import SpaceError

    with _ctx(request) as c:
        slug = str(body.get("slug") or "") or None
        if slug and _spaces(c).get(slug) is not None:
            raise HTTPException(409, f"a space {slug!r} already exists")
        try:
            space = _spaces(c).create(str(body.get("name") or ""), slug)
        except SpaceError as exc:
            status = 409 if "already exists" in str(exc) else 400
            raise HTTPException(status, str(exc)) from exc
        return _space_view(c, space)


@router.patch("/v1/spaces/{slug}")
def rename_space(request: Request, slug: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """``{"name": "New name"}`` (the slug never changes: it names the space's folders)."""
    with _ctx(request) as c:
        _space_or_404(c, slug)
        return _space_view(c, _spaces(c).rename(slug, str(body.get("name") or "")))


@router.delete("/v1/spaces/{slug}")
def delete_space(request: Request, slug: str) -> dict[str, Any]:
    """Only an empty space, never the last one (409 otherwise)."""
    from ..spaces import SpaceError

    with _ctx(request) as c:
        _space_or_404(c, slug)
        try:
            _spaces(c).delete(slug)
        except SpaceError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"deleted": slug}


@router.put("/v1/spaces/{slug}/guilds/{guild_id}")
def add_guild(request: Request, slug: str, guild_id: str) -> dict[str, Any]:
    """Give Discord server ``guild_id`` to the space; 409 when another space owns it."""
    from ..spaces import SpaceError

    with _ctx(request) as c:
        _space_or_404(c, slug)
        if not guild_id.isdigit():
            raise HTTPException(400, f"expected a Discord server id, got {guild_id!r}")
        name = dict(c["repo"].bot_guilds()[0]).get(guild_id, "")
        try:
            return _space_view(c, _spaces(c).add_guild(slug, guild_id, name))
        except SpaceError as exc:
            raise HTTPException(409, str(exc)) from exc


@router.delete("/v1/spaces/{slug}/guilds/{guild_id}")
def remove_guild(request: Request, slug: str, guild_id: str) -> dict[str, Any]:
    from ..spaces import SpaceError

    with _ctx(request) as c:
        _space_or_404(c, slug)
        try:
            return _space_view(c, _spaces(c).remove_guild(slug, guild_id))
        except SpaceError as exc:
            raise HTTPException(404, str(exc)) from exc


@router.get("/v1/guilds")
def list_guilds(request: Request) -> dict[str, Any]:
    """The servers the bot is in (as of its last Discord connect) with the space that owns each (or
    ``null``), plus servers assigned by id the bot has not reported."""
    with _ctx(request) as c:
        repo = c["repo"]
        guilds, seen = repo.bot_guilds()
        items = [{"id": g, "name": n, "space": repo.space_of_guild(g), "bot_present": True} for g, n in guilds]
        known = {g for g, _ in guilds}
        for sp in _spaces(c).all():
            items += [{"id": g, "name": n, "space": sp.slug, "bot_present": False}
                      for g, n in sp.guilds if g not in known]
        return {"items": items, "seen_at": seen}


# -- settings -------------------------------------------------------------------------------------
@router.get("/v1/settings")
def get_settings(request: Request, lang: str = "en", space: str = SpaceParam) -> dict[str, Any]:
    """Without ``?space=``: the global values. With it: what that space sees (its overrides marked
    ``origin: "space"``). ``global``/``space`` list which keys each scope holds."""
    from .settings import llm_view, settings_view

    with _ctx(request) as c:
        row = c["repo"].get_space(_space(c, space)) if space else None
        out = settings_view(SEAMS["settings_store"](), _lang(lang), row)
        out["llm"] = llm_view(SEAMS["aux_store"]())
        return out


@router.put("/v1/settings/{key}")
def put_setting(request: Request, key: str, body: dict[str, Any] = Body(...), space: str = SpaceParam) -> dict[str, Any]:
    """``{"value": ...}``; validated exactly like ``hermes meeting-scribe config set``. With ``?space=``
    it writes that space's override (``"value": null`` removes it); machine-wide keys refuse (400)."""
    if "value" not in body:
        raise HTTPException(400, "value is required")
    from .settings import set_setting, set_space_setting

    with _ctx(request) as c:
        try:
            if space:
                return set_space_setting(SEAMS["settings_store"](), c["repo"], _space(c, space), key, body["value"])
            return set_setting(SEAMS["settings_store"](), c["repo"], key, body["value"])
        except KeyError as exc:
            raise HTTPException(400, f"unknown setting {key}") from exc


@router.put("/v1/llm")
def put_llm(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    from .settings import llm_update

    with _ctx(request):
        return llm_update(SEAMS["aux_store"](), body)


def reset_seams() -> None:  # tests
    SEAMS.update(_DEFAULT_SEAMS)


__all__ = ["router", "SEAMS", "reset_seams"]
