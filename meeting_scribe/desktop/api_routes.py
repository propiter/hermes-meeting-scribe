"""Channel catalog and per-meeting rules for the Desktop (DESIGN §19.3, Appendix A).

``GET /v1/discord/channels?space=`` — the space's channels as the gateway last saw them, grouped for
pickers. ``/v1/routes?space=`` — the rules of ``meeting_routes`` one by one: list (readable and
checked), add, replace, delete and move. Every write goes through :mod:`route_editor` (the same rules
as ``hermes meeting-scribe route``) and answers 400 with a plain reason when a rule cannot be written.
"""
from __future__ import annotations

from typing import Any

from fastapi import Body, HTTPException, Request

from .. import doctor, route_editor
from ..route_editor import RuleError
from .api import SEAMS, SpaceParam, _ctx, _lang, _space, _spaces, router


def _catalog(c: dict[str, Any], space: str) -> Any:
    return doctor.space_catalog(c["repo"], space)


def _rules(c: dict[str, Any], space: str, lang: str) -> dict[str, Any]:
    settings = _spaces(c).settings(space)
    rows = doctor.route_rows(settings, c["repo"])
    for n, row in enumerate(rows, start=1):
        row["position"] = n
        row["sentence"] = route_editor.sentence(row, lang)
    return {"space": space, "scope": route_editor.scope_for(_spaces(c), space), "items": rows,
            "catalog_seen_at": _catalog(c, space).seen_at}


def _write(c: dict[str, Any], space: str, entries: list[str]) -> None:
    from .settings import set_setting, set_space_setting

    store = SEAMS["settings_store"]()
    if route_editor.scope_for(_spaces(c), space) == "space":
        set_space_setting(store, c["repo"], space, "meeting_routes", entries)
    else:
        set_setting(store, c["repo"], "meeting_routes", entries)


def _built(c: dict[str, Any], space: str, body: dict[str, Any]) -> route_editor.Built:
    try:
        return route_editor.build_entry(str(body.get("origin_kind") or ""), str(body.get("origin") or ""),
                                        str(body.get("target") or ""), str(body.get("mode") or "normal"),
                                        _catalog(c, space))
    except RuleError as exc:
        raise HTTPException(400, str(exc)) from exc


def _entries(c: dict[str, Any], space: str) -> list[str]:
    return list(_spaces(c).settings(space).meeting_routes)


def _apply(c: dict[str, Any], space: str, change: Any) -> list[str]:
    try:
        return change(_entries(c, space))
    except RuleError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/v1/discord/channels")
def list_channels(request: Request, space: str = SpaceParam) -> dict[str, Any]:
    """``{"items": [{id, name, type, parent_id, parent_name, public, guild_id, guild_name}], "seen_at",
    "no_servers"}``: only the channels of the space's own servers. An empty list and ``seen_at: null``
    until the gateway has reported them; a space without servers gets ``no_servers: true`` and nothing
    else (never another space's channels)."""
    with _ctx(request) as c:
        cat = _catalog(c, _space(c, space) or "")
        return {"items": cat.channels(), "seen_at": cat.seen_at, "no_servers": cat.no_servers}


@router.get("/v1/routes")
def list_routes(request: Request, space: str = SpaceParam, lang: str = "en") -> dict[str, Any]:
    with _ctx(request) as c:
        return _rules(c, _space(c, space) or "", _lang(lang))


@router.post("/v1/routes")
def add_route(request: Request, body: dict[str, Any] = Body(...), space: str = SpaceParam,
              lang: str = "en") -> dict[str, Any]:
    """``{"origin_kind": voice|category|meet, "origin", "target"?, "mode": normal|private|dm, "position"?}``."""
    with _ctx(request) as c:
        slug = _space(c, space) or ""
        built = _built(c, slug, body)
        position = body.get("position")
        entries = _apply(c, slug, lambda e: route_editor.add(e, built.entry, int(position) if position else None))
        _write(c, slug, entries)
        return {**_rules(c, slug, _lang(lang)), "added": built.entry, "warning": built.warning}


@router.put("/v1/routes/{position}")
def replace_route(request: Request, position: int, body: dict[str, Any] = Body(...), space: str = SpaceParam,
                  lang: str = "en") -> dict[str, Any]:
    with _ctx(request) as c:
        slug = _space(c, space) or ""
        built = _built(c, slug, body)
        entries = _apply(c, slug, lambda e: route_editor.replace_at(e, position, built.entry))
        _write(c, slug, entries)
        return {**_rules(c, slug, _lang(lang)), "warning": built.warning}


@router.delete("/v1/routes/{position}")
def delete_route(request: Request, position: int, space: str = SpaceParam, lang: str = "en") -> dict[str, Any]:
    with _ctx(request) as c:
        slug = _space(c, space) or ""
        entries = _apply(c, slug, lambda e: route_editor.remove(e, position))
        _write(c, slug, entries)
        return _rules(c, slug, _lang(lang))


@router.post("/v1/routes/{position}/move")
def move_route(request: Request, position: int, body: dict[str, Any] = Body(...), space: str = SpaceParam,
               lang: str = "en") -> dict[str, Any]:
    """``{"to": 1}`` — the new position (1 = first)."""
    if not isinstance(body.get("to"), int):
        raise HTTPException(400, "to: the new position, a number (1 = first)")
    with _ctx(request) as c:
        slug = _space(c, space) or ""
        entries = _apply(c, slug, lambda e: route_editor.move(e, position, body["to"]))
        _write(c, slug, entries)
        return _rules(c, slug, _lang(lang))
