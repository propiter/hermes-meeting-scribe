"""``hermes meeting-scribe space …`` and the ``--space`` selector of the other commands (DESIGN §23).

Selection rule, shared by every command: ``--space <slug>`` must name an existing space. Without it,
an install with ONE space uses it; with several, read-only views (``status``, ``list``, ``config
get|list``, ``google status``) show every space, and anything that acts on one space's data or
settings stops and asks for ``--space``.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Optional

from .config import RETIRED_KEYS, SPEC, canonical_key, retired_hint
from .i18n import t
from .spaces import Space


class CliExit(Exception):
    """Stop a command with ``message`` and exit ``code`` (2 = invalid input, 1 = failure)."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def _print(text: str) -> None:
    sys.stdout.write(text.rstrip("\n") + "\n")


def add_space_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--space", metavar="SLUG", help="Space to act on (required when there are several)")


def add_parser(sub: Any) -> None:
    sp = sub.add_parser("space", help="Spaces: teams or clients with their own servers, settings and meetings")
    ss = sp.add_subparsers(dest="space_command")
    ls = ss.add_parser("list", help="Every space with its servers and meeting count")
    ls.add_argument("--json", action="store_true")
    sh = ss.add_parser("show", help="One space: servers, overrides, Google connection")
    sh.add_argument("slug")
    sh.add_argument("--json", action="store_true")
    cr = ss.add_parser("create", help="Create a space")
    cr.add_argument("name")
    cr.add_argument("--slug", help="Id (default: derived from the name)")
    rn = ss.add_parser("rename", help="Change a space's display name")
    rn.add_argument("slug")
    rn.add_argument("name")
    dl = ss.add_parser("delete", help="Delete an EMPTY space (its servers become unassigned)")
    dl.add_argument("slug")
    ag = ss.add_parser("add-guild", help="Assign a Discord server to a space")
    ag.add_argument("slug")
    ag.add_argument("guild_id")
    ag.add_argument("--name", default="", help="Server name to show until the bot sees it")
    rg = ss.add_parser("remove-guild", help="Unassign a Discord server from a space")
    rg.add_argument("slug")
    rg.add_argument("guild_id")
    st = ss.add_parser("set", help="Override one setting for this space")
    st.add_argument("slug")
    st.add_argument("key", metavar="KEY")
    st.add_argument("value")
    us = ss.add_parser("unset", help="Remove a space override (the global value applies again)")
    us.add_argument("slug")
    us.add_argument("key", metavar="KEY")
    sp.set_defaults(_space_parser=sp)


# -- selection -----------------------------------------------------------------------------------
def _names(spaces: list[Space]) -> str:
    return ", ".join(s.slug for s in spaces) or "-"


def selected(args: argparse.Namespace, rt: Any, *, action: bool) -> Optional[str]:
    """The space a command acts on. ``None``: a view over every space (several spaces, no
    ``--space``, ``action=False``). ``CliExit`` for an unknown slug or an action without a choice."""
    lang = rt.settings().ui_language
    spaces = rt.spaces().all()
    wanted = (getattr(args, "space", None) or "").strip().lower()
    if wanted:
        if not any(s.slug == wanted for s in spaces):
            raise CliExit(t("space.cli_unknown", lang, slug=wanted, spaces=_names(spaces)))
        return wanted
    if len(spaces) == 1:
        return spaces[0].slug
    if action:
        raise CliExit(t("space.cli_required", lang, spaces=_names(spaces)))
    return None


def several(rt: Any) -> bool:
    return len(rt.spaces().all()) > 1


# -- commands ------------------------------------------------------------------------------------
def dispatch(args: argparse.Namespace, rt: Any) -> int:
    cmd = getattr(args, "space_command", None)
    if cmd is None:
        _print(args._space_parser.format_help())
        return 0
    handlers: dict[str, Callable[[argparse.Namespace, Any], int]] = {
        "list": _list, "show": _show, "create": _create, "rename": _rename, "delete": _delete,
        "add-guild": _add_guild, "remove-guild": _remove_guild, "set": _set, "unset": _unset}
    try:
        return handlers[cmd](args, rt)
    except CliExit as exc:
        _print(str(exc))
        return exc.code
    except ValueError as exc:  # SpaceError, or a value the setting's rules refuse
        _print(str(exc))
        return 2


def _guild_label(gid: str, name: str) -> str:
    return f"{name} ({gid})" if name else gid


def _row(rt: Any, space: Space, counts: dict[str, dict[str, int]]) -> dict[str, Any]:
    out = space.to_dict()
    out.pop("adopt_guilds", None)
    out["counts"] = counts.get(space.slug, {"meetings": 0, "queued": 0, "running": 0, "failed": 0})
    out["google"] = _google(rt, space.slug)
    return out


def _google(rt: Any, slug: str) -> dict[str, Any]:
    files = rt.google_files(slug)
    token = files.read_token() or {}
    status = rt.meet_importer(slug).status()
    return {"client_stored": files.client_path.exists(),
            "connected": bool(token.get("refresh_token")) and not token.get("disconnected"),
            "last_poll_at": status.get("last_poll_at"), "last_import_at": status.get("last_import_at"),
            "last_error": status.get("last_error") if status.get("last_poll_ok") == "0" else None}


def _list(args: argparse.Namespace, rt: Any) -> int:
    counts = rt.repo().space_counts()
    rows = [_row(rt, s, counts) for s in rt.spaces().all()]
    if args.json:
        _print(json.dumps({"spaces": rows, "unassigned_guilds": unassigned(rt)}, ensure_ascii=False))
        return 0
    lang = rt.settings().ui_language
    for r in rows:
        guilds = ", ".join(_guild_label(g["id"], g["name"]) for g in r["guilds"]) or t("space.cli_no_guilds", lang)
        google = "google: " + ("connected" if r["google"]["connected"] else "not connected")
        _print(f"{r['slug']:<16} {r['name']}  — {r['counts']['meetings']} meeting(s); {google}")
        _print(f"{'':<16} {guilds}")
    for g in unassigned(rt):
        _print("! " + t("space.cli_unassigned_guild", lang, guild=_guild_label(g["id"], g["name"])))
    return 0


def unassigned(rt: Any) -> list[dict[str, str]]:
    """Servers the bot was last seen in that no space owns (only meaningful with several spaces)."""
    repo = rt.repo()
    guilds, _seen = repo.bot_guilds()
    return [{"id": g, "name": n} for g, n in guilds if repo.space_of_guild(g) is None]


def _show(args: argparse.Namespace, rt: Any) -> int:
    space = rt.spaces().require(args.slug.strip().lower())
    row = _row(rt, space, rt.repo().space_counts())
    if args.json:
        _print(json.dumps(row, ensure_ascii=False))
        return 0
    lang = rt.settings().ui_language
    _print(f"{space.slug}: {space.name}")
    _print(t("space.cli_guilds", lang) + " " + (", ".join(_guild_label(g, n) for g, n in space.guilds)
                                              or t("space.cli_no_guilds", lang)))
    c = row["counts"]
    _print(f"meetings: {c['meetings']}  jobs: {c['queued']} queued, {c['running']} running, {c['failed']} failed")
    g = row["google"]
    _print(f"google: {'connected' if g['connected'] else 'not connected'}"
           + "".join(f"; {k}={g[k]}" for k in ("last_poll_at", "last_import_at", "last_error") if g.get(k)))
    if space.overrides:
        _print(t("space.cli_overrides", lang))
        for key, value in sorted(space.overrides.items()):
            _print(f"  {key} = {', '.join(value) if isinstance(value, list) else value}")
    else:
        _print(t("space.cli_no_overrides", lang))
    return 0


def _create(args: argparse.Namespace, rt: Any) -> int:
    space = rt.spaces().create(args.name, args.slug)
    _print(t("space.cli_created", rt.settings().ui_language, slug=space.slug, name=space.name))
    return 0


def _rename(args: argparse.Namespace, rt: Any) -> int:
    space = rt.spaces().rename(args.slug, args.name)
    _print(t("space.cli_renamed", rt.settings().ui_language, slug=space.slug, name=space.name))
    return 0


def _delete(args: argparse.Namespace, rt: Any) -> int:
    rt.spaces().delete(args.slug)
    _print(t("space.cli_deleted", rt.settings().ui_language, slug=args.slug))
    return 0


def _add_guild(args: argparse.Namespace, rt: Any) -> int:
    name = args.name or dict(rt.repo().bot_guilds()[0]).get(str(args.guild_id).strip(), "")
    space = rt.spaces().add_guild(args.slug, args.guild_id, name)
    _print(t("space.cli_guild_added", rt.settings().ui_language, guild=_guild_label(args.guild_id, name),
             slug=space.slug))
    return 0


def _remove_guild(args: argparse.Namespace, rt: Any) -> int:
    space = rt.spaces().remove_guild(args.slug, args.guild_id)
    _print(t("space.cli_guild_removed", rt.settings().ui_language, guild=args.guild_id, slug=space.slug))
    return 0


def check_space_key(key: str, lang: str) -> str:
    """The canonical name of a setting a space may override; ``CliExit`` for a machine-wide one."""
    try:
        name = canonical_key(key)
    except KeyError as exc:
        raise CliExit(retired_hint(key) or f"unknown key {key}") from exc
    if SPEC[name].scope != "space":
        raise CliExit(t("space.cli_global_key", lang, key=name))
    return name


def _set(args: argparse.Namespace, rt: Any) -> int:
    lang = rt.settings().ui_language
    key, value = rt.spaces().set_override(args.slug, check_space_key(args.key, lang), args.value)
    _print(t("space.cli_override_set", lang, slug=args.slug, key=key,
             value=", ".join(value) if isinstance(value, (list, tuple)) else value))
    return 0


def _unset(args: argparse.Namespace, rt: Any) -> int:
    """Also removes an override of a retired key (e.g. ``delivery_project_threads`` kept by an older
    version), which ``set`` refuses."""
    lang = rt.settings().ui_language
    key = args.key if args.key in RETIRED_KEYS else check_space_key(args.key, lang)
    key = rt.spaces().unset_override(args.slug, key)
    _print(t("space.cli_override_unset", lang, slug=args.slug, key=key))
    return 0
