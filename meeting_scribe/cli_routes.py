"""``hermes meeting-scribe route list|add|remove|move`` — the rules of ``meeting_routes``, one at a time
(DESIGN §19.3), with channel names instead of ids when the gateway has reported the channels.

``route add --voice "<name or id>" | --category "<name or id>" | --meet "<pattern>" --to "#channel"
[--private | --dm]``: names are resolved against the channel catalog (the rule is stored with ids,
so renaming a channel never breaks it); ``--dm`` takes no ``--to``. ``--space`` picks the space.
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from . import doctor, route_editor
from .cli_spaces import CliExit, _print, add_space_arg, selected
from .i18n import t
from .route_editor import RuleError


def add_parser(sub: Any) -> None:
    rp = sub.add_parser("route", help="Per-meeting rules: which channel (or direct messages) each meeting goes to")
    rs = rp.add_subparsers(dest="route_command")
    ls = rs.add_parser("list", help="Every rule, readable, with its channels checked")
    add_space_arg(ls)
    ls.add_argument("--json", action="store_true")
    ad = rs.add_parser("add", help="Add a rule")
    add_space_arg(ad)
    origin = ad.add_mutually_exclusive_group(required=True)
    origin.add_argument("--voice", metavar="CHANNEL", help="Meetings of this voice channel (name or id)")
    origin.add_argument("--category", metavar="CATEGORY", help="Meetings of any voice channel in this category")
    origin.add_argument("--meet", metavar="PATTERN", help="Google Meet meetings whose code/title matches (* wildcard)")
    ad.add_argument("--to", metavar="CHANNEL", default="", help="Text or forum channel (name or id); not with --dm")
    mode = ad.add_mutually_exclusive_group()
    mode.add_argument("--private", action="store_true", help="Everything stays in that channel; tasks leave by button")
    mode.add_argument("--dm", action="store_true", help="No channel: each participant gets it by direct message")
    ad.add_argument("--position", type=int, help="Where in the list (1 = first; default: last)")
    rm = rs.add_parser("remove", help="Remove a rule (its number from `route list`, or its origin)")
    add_space_arg(rm)
    rm.add_argument("rule")
    mv = rs.add_parser("move", help="Change a rule's position (the first matching rule of the same kind wins)")
    add_space_arg(mv)
    mv.add_argument("rule")
    mv.add_argument("position", type=int)
    rp.set_defaults(_route_parser=rp)


def _state(args: argparse.Namespace, rt: Any) -> tuple[str, str, list[str]]:
    """``(space, scope, entries)``: where the list lives and what it holds now."""
    space = selected(args, rt, action=True) or ""
    scope = route_editor.scope_for(rt.spaces(), space)
    return space, scope, list(rt.settings(space).meeting_routes)


def _write(rt: Any, space: str, scope: str, entries: list[str]) -> None:
    if scope == "space":
        rt.spaces().set_override(space, "meeting_routes", entries)
    else:
        rt.set_config("meeting_routes", entries)
    from .cli import _nudge_waiting  # cli imports this module: bound at call time

    _nudge_waiting(rt)  # a delivery waiting for its rule goes now


def _catalog(rt: Any, space: str) -> Any:
    return doctor.space_catalog(rt.service().repo, space)


def _list(args: argparse.Namespace, rt: Any) -> int:
    space = selected(args, rt, action=False) or ""
    settings = rt.settings(space or None)
    lang = settings.ui_language
    rows = doctor.route_rows(settings, rt.service().repo)
    if args.json:
        _print(json.dumps({"space": space, "rules": rows}, ensure_ascii=False, default=str))
        return 0
    if not rows:
        _print(t("routes.cli_none", lang))
        return 0
    for n, row in enumerate(rows, start=1):
        status = row.get("status")
        mark = {"ok": "✓", "not_checked": "·"}.get(str(status), "!")
        _print(f"{n:>2}. {mark} {route_editor.sentence(row, lang)}")
        _print(f"      {doctor.route_text(row)}")
        if status == "not_checked":
            _print(f"      {t('routes.cli_not_checked', lang)}")
        elif status not in ("ok",) and row.get("detail"):
            _print(f"      ! {row['detail']}")
        if row.get("warning"):
            _print(f"      ! {row['warning']}")
    return 0


def _add(args: argparse.Namespace, rt: Any) -> int:
    space, scope, entries = _state(args, rt)
    kind, origin = next((k, v) for k, v in (("voice", args.voice), ("category", args.category), ("meet", args.meet))
                        if v)
    mode = "dm" if args.dm else "private" if args.private else "normal"
    built = route_editor.build_entry(kind, origin, args.to, mode, _catalog(rt, space))
    new = route_editor.add(entries, built.entry, args.position)
    _write(rt, space, scope, new)
    lang = rt.settings(space).ui_language
    _print(t("routes.cli_added", lang, rule=built.entry))
    if built.warning:
        _print(f"! {built.warning}")
    return 0


def _remove(args: argparse.Namespace, rt: Any) -> int:
    space, scope, entries = _state(args, rt)
    gone = entries[route_editor.index_of(entries, args.rule)]
    new = route_editor.remove(entries, args.rule)
    _write(rt, space, scope, new)
    _print(t("routes.cli_removed", rt.settings(space).ui_language, rule=gone))
    return 0


def _move(args: argparse.Namespace, rt: Any) -> int:
    space, scope, entries = _state(args, rt)
    new = route_editor.move(entries, args.rule, args.position)
    _write(rt, space, scope, new)
    _print(t("routes.cli_moved", rt.settings(space).ui_language, position=max(1, min(len(new), args.position))))
    return 0


_YES = ("y", "yes", "s", "si", "sí")


def setup_step(rt: Any, lang: str, ask: Any = None) -> int:
    """The optional rules step of ``setup``: add rules one question at a time; returns how many."""
    ask = ask or input
    if ask(t("routes.setup_ask", lang)).strip().lower() not in _YES:
        return 0
    spaces = rt.spaces().all()
    if len(spaces) > 1:  # which space a rule belongs to is a per-space decision: say how
        _print(t("routes.setup_spaces", lang))
        return 0
    space = spaces[0].slug if spaces else ""
    scope = route_editor.scope_for(rt.spaces(), space) if spaces else "global"
    catalog = _catalog(rt, space)
    if not catalog.known:
        _print(t("routes.cli_not_checked", lang))
    added = 0
    while True:
        _print(t("routes.setup_modes", lang))
        kind = {"1": "voice", "2": "category", "3": "meet"}.get(ask(t("routes.setup_kind", lang)).strip())
        if kind is None:
            return added
        origin = ask(t(f"routes.setup_origin_{kind}", lang)).strip()
        mode = {"1": "normal", "2": "private", "3": "dm"}.get(ask(t("routes.setup_mode", lang)).strip() or "1", "")
        target = "" if mode == "dm" else ask(t("routes.setup_target", lang)).strip()
        try:
            built = route_editor.build_entry(kind, origin, target, mode, catalog)
            entries = route_editor.add(list(rt.settings(space or None).meeting_routes), built.entry)
        except RuleError as exc:
            _print(f"! {exc}")
        else:
            _write(rt, space, scope, entries)
            added += 1
            _print(t("routes.cli_added", lang, rule=built.entry))
            if built.warning:
                _print(f"! {built.warning}")
        if ask(t("routes.setup_more", lang)).strip().lower() not in _YES:
            return added


def dispatch(args: argparse.Namespace, rt: Any) -> int:
    cmd = getattr(args, "route_command", None)
    if cmd is None:
        _print(args._route_parser.format_help())
        return 0
    handlers = {"list": _list, "add": _add, "remove": _remove, "move": _move}
    try:
        return handlers[cmd](args, rt)
    except CliExit as exc:
        _print(str(exc))
        return exc.code
    except (RuleError, ValueError) as exc:  # a rule that cannot be written, a space override refused
        _print(str(exc))
        return 2
