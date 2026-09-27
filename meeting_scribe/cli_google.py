"""``hermes meeting-scribe google connect|status|sync|disconnect`` (DESIGN §17). No secret is ever printed."""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .i18n import t

_READ_LINE: Callable[[str], str] = input  # seam for tests


def add_parser(sub: Any) -> None:
    g = sub.add_parser("google", help="Google Meet import: connect, status, sync, disconnect")
    gs = g.add_subparsers(dest="google_command")
    c = gs.add_parser("connect", help="Authorize read access to your Meet transcripts (OAuth, your own client)")
    c.add_argument("--client-secret", help="OAuth client JSON downloaded from Cloud Console (type Desktop app)")
    c.add_argument("--no-browser", action="store_true", help="Print the URL and paste the redirect back (SSH)")
    c.add_argument("--timeout", type=int, default=300, help="Seconds to wait for the browser redirect")
    st = gs.add_parser("status", help="Connection and last poll")
    st.add_argument("--json", action="store_true")
    sy = gs.add_parser("sync", help="List/import finished conferences now")
    grp = sy.add_mutually_exclusive_group()
    grp.add_argument("--since", help="RFC 3339 start (e.g. 2026-09-01T00:00:00Z)")
    grp.add_argument("--days", type=int, help="Backfill the last N days (Meet keeps 30)")
    sy.add_argument("--dry-run", action="store_true", help="Only list what would be imported")
    sy.add_argument("--json", action="store_true")
    gs.add_parser("disconnect", help="Revoke access (best effort) and delete the stored token")
    g.set_defaults(_google_parser=g)


def _print(text: str) -> None:
    sys.stdout.write(text.rstrip("\n") + "\n")


def dispatch(args: argparse.Namespace, rt: Any) -> int:
    cmd = getattr(args, "google_command", None)
    if cmd is None:
        parser = getattr(args, "_google_parser", None)
        if parser is not None:
            _print(parser.format_help())
        return 0
    return {"connect": _connect, "status": _status, "sync": _sync, "disconnect": _disconnect}[cmd](args, rt)


def _connect(args: argparse.Namespace, rt: Any) -> int:
    from .google import oauth
    from .google.http import UrllibTransport

    lang = rt.settings().ui_language
    files = rt.google_files()
    try:
        client = oauth.import_client_file(files, Path(args.client_secret)) if args.client_secret \
            else oauth.load_client(files)
    except oauth.GoogleDisconnected:
        _print(t("google.no_client", lang))
        return 2
    except oauth.GoogleAuthError as exc:
        _print(t("google.error", lang, error=exc))
        return 2
    transport = rt.google_transport or UrllibTransport()

    def emit(url: str) -> None:
        _print(t("google.connect_intro", lang))
        _print(url)
        if not args.no_browser:
            _print(t("google.connect_waiting", lang, minutes=max(1, args.timeout // 60)))

    def read_line(_prompt: str) -> str:
        _print(t("google.connect_paste", lang))
        return _READ_LINE("> ")

    try:
        token = oauth.connect_flow(files, client, transport=transport, no_browser=args.no_browser, emit=emit,
                                   read_line=read_line, open_browser=None if args.no_browser else webbrowser.open,
                                   timeout=float(args.timeout))
    except (oauth.GoogleAuthError, OSError) as exc:
        _print(t("google.error", lang, error=exc))
        return 1
    try:
        rt.meet_importer().set_status(last_error=None)
    except Exception:  # storage trouble is reported by `doctor`; the token is stored
        pass
    extra = "" if rt.settings().google_meet_enabled else t("google.enable_hint", lang)
    if token.get("reconnected_at"):
        since = datetime.fromtimestamp(float(token["connected_at"]), timezone.utc).isoformat(timespec="minutes")
        _print(t("google.reconnected", lang, since=since, extra=extra))
    else:
        _print(t("google.connected", lang, extra=extra))
    return 0


def status_dict(rt: Any) -> dict[str, Any]:
    files = rt.google_files()
    token = files.read_token() or {}
    connected = rt.google_credentials().connected()
    out: dict[str, Any] = {"connected": connected, "revoked": bool(token.get("disconnected")),
                           "client_stored": files.client_path.exists(),
                           "enabled": rt.settings().google_meet_enabled}
    if token.get("connected_at"):
        out["connected_at"] = datetime.fromtimestamp(float(token["connected_at"]), timezone.utc).isoformat()
    try:
        out.update(rt.meet_importer().status())
    except Exception as exc:  # storage unavailable: status still answers
        out["status_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _status(args: argparse.Namespace, rt: Any) -> int:
    lang = rt.settings().ui_language
    st = status_dict(rt)
    if args.json:
        _print(json.dumps(st, ensure_ascii=False))
        return 0
    if st["connected"]:
        _print(t("google.status_connected", lang))
    elif st["revoked"]:
        _print(t("google.status_disconnected", lang))
    else:
        _print(t("google.not_connected", lang))
    for key in ("enabled", "connected_at", "last_poll_at", "last_poll_ok", "last_error", "last_import_at",
                "last_import_meeting", "records_given_up", "records_given_up_last", "retry_after_until"):
        if key in st:
            _print(t("google.status_line", lang, key=key, value=st[key]))
    return 0


def _sync(args: argparse.Namespace, rt: Any) -> int:
    from .google.convert import parse_time

    lang = rt.settings().ui_language
    if not rt.google_credentials().connected():
        _print(t("google.not_connected", lang))
        return 1
    since = None
    if args.since:
        since = parse_time(args.since)
        if since is None:
            _print(t("google.error", lang, error=f"invalid --since {args.since!r} (RFC 3339)"))
            return 2
    try:
        importer = rt.meet_importer()
        start = importer.window_start(since=since, days=args.days, connected_at=rt.google_connected_at())
        report = importer.sync(ended_after=start, dry_run=args.dry_run)
    except Exception as exc:  # storage unavailable etc.: one line, never a traceback
        _print(t("google.error", lang, error=f"{type(exc).__name__}: {exc}"))
        return 1
    if args.json:
        _print(json.dumps({"since": start.isoformat(), **report.as_dict()}, ensure_ascii=False))
    else:
        _print(t("google.sync_report", lang, listed=report.listed, imported=len(report.imported),
                 already=report.already, pending=len(report.pending), none=report.no_transcript))
        if args.dry_run:
            _print(t("google.sync_dry", lang, records=", ".join(report.would_import) or "-"))
        for mid in report.imported:
            _print(f"  + {mid}")
        for err in report.errors:
            _print(t("google.sync_error", lang, error=err))
    return 1 if report.errors else 0


def _disconnect(args: argparse.Namespace, rt: Any) -> int:
    from .google import oauth
    from .google.http import UrllibTransport

    lang = rt.settings().ui_language
    files = rt.google_files()
    token = files.read_token() or {}
    revoked = oauth.revoke(rt.google_transport or UrllibTransport(), token) if token else False
    files.delete_token()
    _print(t("google.disconnected", lang, revoked=t("google.revoked", lang) if revoked else ""))
    return 0
