"""``hermes meeting-scribe setup|doctor|status|list|show|reprocess|export|config`` (DESIGN §10).

``setup_parser`` / ``dispatch`` are pure (the runtime is injected) so they are unit-tested
without Hermes; ``register()`` binds them to the plugin runtime. Exit codes: 0 ok, 1 failure /
not found, 2 invalid input.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from . import doctor
from .config import LEGACY_KEYS, SPEC, Settings, canonical_key, validate_value
from .domain.models import Stage
from .i18n import t
from .pipeline.service import MeetingService
from .storage.artifacts import read_notes, read_transcript, render_notes_md
from .transcribe.client import realtime_factor

MODELS = ("tiny", "base", "small", "medium", "large-v3", "turbo")


class CliRuntime(Protocol):
    def service(self) -> MeetingService: ...

    def settings(self) -> Settings: ...

    def set_config(self, key: str, value: Any) -> None: ...

    def doctor_env(self) -> Any: ...


def doctor_registry() -> doctor.CheckRegistry:
    return doctor.registry


def setup_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="ms_command")
    s = sub.add_parser("setup", help="Interactive (or flag-driven) configuration wizard")
    s.add_argument("--non-interactive", action="store_true", help="Only apply the given flags")
    s.add_argument("--language", help="Spoken language code (es, en, ... or auto)")
    s.add_argument("--model", help=f"Whisper model ({', '.join(MODELS)})")
    s.add_argument("--notes-channel", help="Discord channel id for notes")
    s.add_argument("--owners", help="Comma-separated Discord user ids allowed to send tasks to Kanban")
    s.add_argument("--autojoin", dest="autojoin", action="store_true", default=None)
    s.add_argument("--no-autojoin", dest="autojoin", action="store_false")
    s.add_argument("--retention", choices=SPEC["audio_retention"].choices)
    s.add_argument("--kanban-mode")
    s.add_argument("--linear-mode")
    s.add_argument("--linear-team")
    s.add_argument("--obsidian-vault")
    d = sub.add_parser("doctor", help="Check dependencies and integrations")
    d.add_argument("--json", action="store_true")
    st = sub.add_parser("status", help="Queue and recent meetings")
    st.add_argument("--json", action="store_true")
    ls = sub.add_parser("list", help="Recent meetings")
    ls.add_argument("-n", type=int, default=20)
    sh = sub.add_parser("show", help="Print a meeting's notes")
    sh.add_argument("meeting_id")
    rp = sub.add_parser("reprocess", help="Re-run a meeting from a stage")
    rp.add_argument("meeting_id")
    rp.add_argument("--from", dest="stage", default="transcribe", choices=["transcribe", "analyze", "deliver"])
    rp.add_argument("--now", action="store_true", help="Process in this process instead of queueing for the gateway")
    ex = sub.add_parser("export", help="Export notes + transcript")
    ex.add_argument("meeting_id")
    ex.add_argument("--format", choices=["md", "json"], default="md")
    ex.add_argument("--out", help="File to write (default: stdout)")
    cf = sub.add_parser("config", help="Get or set plugin settings")
    cf_sub = cf.add_subparsers(dest="config_command")
    g = cf_sub.add_parser("get")
    g.add_argument("key", nargs="?")
    se = cf_sub.add_parser("set")
    se.add_argument("key", choices=[*SPEC, *LEGACY_KEYS.values()], metavar="KEY")
    se.add_argument("value")
    parser.set_defaults(_ms_parser=parser)


def _print(text: str) -> None:
    sys.stdout.write(text.rstrip("\n") + "\n")


def _fmt(value: Any) -> str:
    return ", ".join(value) if isinstance(value, (tuple, list)) else str(value)


# -- commands -------------------------------------------------------------------------------------
def _setup(args: argparse.Namespace, rt: CliRuntime) -> int:
    current = rt.settings()
    lang = current.ui_language
    flags: dict[str, Any] = {
        "transcribe_language": args.language, "transcribe_model": args.model,
        "delivery_discord_channel": args.notes_channel, "owners": args.owners, "autojoin_enabled": args.autojoin,
        "audio_retention": args.retention, "kanban_mode": args.kanban_mode, "linear_mode": args.linear_mode,
        "linear_default_team": args.linear_team, "obsidian_vault_path": args.obsidian_vault}
    answers = {k: v for k, v in flags.items() if v is not None}
    if not args.non_interactive:
        _print(t("cli.setup_intro", lang))
        _print(t("cli.setup_language_hint", lang))
        for key in flags:
            if key in answers:
                continue
            default = _fmt(getattr(current, key))
            if key == "transcribe_model":
                for m in MODELS:
                    _print("  " + t("cli.model_estimate", lang, model=m, factor=realtime_factor(m),
                                    threads=current.effective_cpu_threads))
            raw = input(f"{key} [{default}]: ").strip()
            if raw:
                answers[key] = raw
    if "transcribe_language" in answers and str(answers["transcribe_language"]) != "auto":
        code = str(answers["transcribe_language"])
        answers.setdefault("analysis_language", code)
        if code in ("es", "en"):
            answers.setdefault("ui_language", code)
    try:
        validated = {k: validate_value(k, v) for k, v in answers.items()}
    except ValueError as exc:
        _print(str(exc))
        return 2
    for key, value in validated.items():
        rt.set_config(key, value)
    _print(t("cli.setup_saved", rt.settings().ui_language, count=len(validated)))
    return 0


def _doctor(args: argparse.Namespace, rt: CliRuntime) -> int:
    results, code = doctor.run_checks(doctor_registry(), rt.doctor_env())
    if args.json:
        _print(json.dumps({"ok": code == 0, "checks": [r.__dict__ for r in results]}, ensure_ascii=False))
    else:
        _print(doctor.format_report(results, rt.settings().ui_language))
    return code


def _status(args: argparse.Namespace, rt: CliRuntime) -> int:
    st = rt.service().status(recent=10)
    if args.json:
        _print(json.dumps(st, ensure_ascii=False, default=str))
        return 0
    lang = rt.settings().ui_language
    _print(t("cmd.status_idle", lang, queued=st["queued"]))
    for row in st["recent"]:
        job = row["job"] or {}
        extra = f" [{job.get('state')}: {job.get('error')}]" if job.get("error") else ""
        _print(f"  {row['id']}  {row['state']:<12} {row['title']}{extra}")
    return 0


def _list(args: argparse.Namespace, rt: CliRuntime) -> int:
    meetings = rt.service().repo.list_meetings(limit=args.n)
    if not meetings:
        _print(t("cli.no_meetings", rt.settings().ui_language))
    for m in meetings:
        _print(f"{m.id}  {m.started_at:%Y-%m-%d %H:%M}  {m.state.value:<12} {m.title or m.channel_name}")
    return 0


def _find(rt: CliRuntime, meeting_id: str) -> Optional[Any]:
    meeting = rt.service().find(meeting_id)
    if meeting is None:
        _print(t("cmd.not_found", rt.settings().ui_language, id=meeting_id))
    return meeting


def _show(args: argparse.Namespace, rt: CliRuntime) -> int:
    meeting = _find(rt, args.meeting_id)
    if meeting is None:
        return 1
    notes = read_notes(rt.service().folder(meeting))
    if notes is None:
        _print(f"{meeting.id}: {meeting.state.value}")
        return 0
    _print(render_notes_md(meeting, notes, notes.language or rt.settings().ui_language))
    return 0


def _reprocess(args: argparse.Namespace, rt: CliRuntime) -> int:
    meeting = _find(rt, args.meeting_id)
    if meeting is None:
        return 1
    service = rt.service()
    stage = Stage.parse(args.stage)
    service.reprocess(meeting.id, stage)
    if args.now:
        while service.runner.run_once():
            pass
    state = service.repo.get_meeting(meeting.id).state.value  # type: ignore[union-attr]
    _print(t("cli.reprocess_done", rt.settings().ui_language, id=meeting.id, stage=stage.value, state=state))
    return 0


def _export(args: argparse.Namespace, rt: CliRuntime) -> int:
    meeting = _find(rt, args.meeting_id)
    if meeting is None:
        return 1
    folder = rt.service().folder(meeting)
    notes = read_notes(folder)
    utts = read_transcript(folder)
    if args.format == "json":
        text = json.dumps({"meeting": meeting.to_dict(), "notes": notes.to_dict() if notes else None,
                           "transcript": [u.to_dict() for u in utts]}, ensure_ascii=False, indent=2)
    else:
        text = render_notes_md(meeting, notes, notes.language or "en") if notes else ""
        transcript_md = folder / "transcript.md"
        if transcript_md.exists():
            text += "\n\n" + transcript_md.read_text(encoding="utf-8")
    if args.out:
        Path(args.out).expanduser().write_text(text, encoding="utf-8")
        _print(t("cli.exported", rt.settings().ui_language, path=args.out))
    else:
        _print(text)
    return 0


def _config(args: argparse.Namespace, rt: CliRuntime) -> int:
    settings = rt.settings()
    if getattr(args, "config_command", None) == "set":
        try:
            rt.set_config(canonical_key(args.key), validate_value(args.key, args.value))
        except KeyError:
            _print(f"unknown key {args.key}")
            return 2
        except ValueError as exc:
            _print(str(exc))
            return 2
        return 0
    key = getattr(args, "key", None)
    if key:
        try:
            key = canonical_key(key)
        except KeyError:
            _print(f"unknown key {key}")
            return 2
        _print(_fmt(settings.as_dict()[key]))
        return 0
    for k, v in settings.as_dict().items():
        _print(f"{k} = {_fmt(v)}")
    for w in settings.warnings:
        _print(f"! {w}")
    return 0


_COMMANDS: dict[str, Callable[[argparse.Namespace, CliRuntime], int]] = {
    "setup": _setup, "doctor": _doctor, "status": _status, "list": _list, "show": _show,
    "reprocess": _reprocess, "export": _export, "config": _config}


def dispatch(args: argparse.Namespace, rt: CliRuntime) -> int:
    command = getattr(args, "ms_command", None)
    if command is None:
        parser = getattr(args, "_ms_parser", None)
        if parser is not None:
            _print(parser.format_help())
        return 0
    return _COMMANDS[command](args, rt)
