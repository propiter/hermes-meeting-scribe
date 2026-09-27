"""``hermes meeting-scribe setup|doctor|status|list|show|reprocess|export|config|google`` (DESIGN §10, §17).

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

from . import cli_google, doctor, llm_config
from .config import CHANNEL_KEYS, LEGACY_KEYS, SPEC, Settings, canonical_key, config_schema, validate_value
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
    s.add_argument("--notes-channel", help="Discord notes channel: id, <#id> or name")
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
    cl = cf_sub.add_parser("list", help="Every setting with its effective value and where it comes from")
    cl.add_argument("--json", action="store_true")
    cl.add_argument("--group", help="Only one group (see `config schema`)")
    sc = cf_sub.add_parser("schema", help="Machine-readable description of every setting (for UIs)")
    sc.add_argument("--json", action="store_true", help="JSON output (the only format; kept for clarity)")
    sc.add_argument("--lang", help="Language of labels/help (default: ui_language)")
    _llm_parser(sub)
    s.add_argument("--google-meet", dest="google_meet", action="store_true", default=None,
                   help="Enable Google Meet import (then run `google connect`)")
    s.add_argument("--no-google-meet", dest="google_meet", action="store_false")
    s.add_argument("--google-meet-channel", help="Discord channel for Google Meet notes: id, <#id> or name")
    cli_google.add_parser(sub)
    parser.set_defaults(_ms_parser=parser)


def _llm_parser(sub: Any) -> None:
    lp = sub.add_parser("llm", help="Models and fallbacks for meeting analysis (auxiliary.meeting_scribe)")
    ls = lp.add_subparsers(dest="llm_command")
    sh = ls.add_parser("show", help="Effective provider, model, fallback chain and timeout, and their origin")
    sh.add_argument("--json", action="store_true")
    st = ls.add_parser("set", help="Primary provider/model (auto = Hermes' main model)")
    st.add_argument("--provider")
    st.add_argument("--model")
    st.add_argument("--base-url", dest="base_url")
    st.add_argument("--timeout", type=float, help="Hermes-side request timeout (seconds)")
    fb = ls.add_parser("fallback", help="Ordered fallback chain Hermes walks on 402 / rate limit / connection errors")
    fs = fb.add_subparsers(dest="fallback_command")
    fa = fs.add_parser("add", help="Append (or insert with --position) provider[:model]")
    fa.add_argument("link", metavar="PROVIDER[:MODEL]")
    fa.add_argument("--position", type=int, help="1-based position")
    fa.add_argument("--base-url", dest="base_url")
    fr = fs.add_parser("remove", help="By position, provider or provider:model")
    fr.add_argument("which")
    fs.add_parser("clear", help="Remove every fallback")
    fset = fs.add_parser("set", help="Replace the whole chain")
    fset.add_argument("links", nargs="+", metavar="PROVIDER[:MODEL]")
    tp = ls.add_parser("test", help="Probe every link of the chain with a tiny call (no credentials printed)")
    tp.add_argument("--json", action="store_true")
    tp.add_argument("--timeout", type=float, default=llm_config.PROBE_TIMEOUT)


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
    google = {"google_meet_enabled": getattr(args, "google_meet", None),
              "google_meet_discord_channel": getattr(args, "google_meet_channel", None)}
    answers = {k: v for k, v in {**flags, **google}.items() if v is not None}
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
        if "google_meet_enabled" not in answers:  # optional, off by default (DESIGN §17)
            raw = input(t("cli.setup_google", lang)).strip().lower()
            if raw in ("y", "yes", "s", "si", "sí"):
                answers["google_meet_enabled"] = True
                if "google_meet_discord_channel" not in answers:
                    raw = input(f"google_meet_discord_channel [{current.google_meet_discord_channel}]: ").strip()
                    if not raw and not _meet_channel_set(current, answers):
                        _print(t("cli.meet_channel_missing", lang))
                        raw = input("google_meet_discord_channel []: ").strip()
                    if raw:
                        answers["google_meet_discord_channel"] = raw
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
    _warn_meet_channel(rt)
    return 0


def _meet_channel_set(settings: Settings, pending: dict[str, Any]) -> bool:
    return any(str(pending.get(k) or getattr(settings, k) or "").strip()
               for k in ("google_meet_discord_channel", "delivery_discord_channel"))


def _warn_meet_channel(rt: CliRuntime) -> None:
    """Meet import on without any notes channel: notes go to the server's automatic channel, or wait."""
    s = rt.settings()
    if s.google_meet_enabled and not _meet_channel_set(s, {}):
        _print(t("cli.meet_channel_missing", s.ui_language))


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
        if row.get("delivery"):
            extra = f" [{t('cli.waiting_destination', lang)}]"
        else:
            extra = f" [{job.get('state')}: {job.get('error')}]" if job.get("error") else ""
        _print(f"  {row['id']}  {row['state']:<12} {row['title']}{extra}")
    for mid, reason in (st.get("waiting_destination") or {}).items():
        _print(f"! {mid}: {reason}")
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
    asked = Stage.parse(args.stage)
    stage = service.effective_stage(meeting, asked)
    if stage is not asked:
        _print(t("cmd.reprocess_no_audio", rt.settings().ui_language))
    service.reprocess(meeting.id, asked)
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
    command = getattr(args, "config_command", None)
    if command == "set":
        try:
            key = canonical_key(args.key)
            rt.set_config(key, validate_value(args.key, args.value))
        except KeyError:
            _print(f"unknown key {args.key}")
            return 2
        except ValueError as exc:
            _print(str(exc))
            return 2
        if key in ("google_meet_enabled", "google_meet_discord_channel", "delivery_discord_channel"):
            _warn_meet_channel(rt)
        if key in CHANNEL_KEYS or key in ("delivery_discord_guild", "delivery_auto_channel_names"):
            _nudge_waiting(rt)
        return 0
    if command == "list":
        return _config_list(args, rt)
    if command == "schema":
        _print(json.dumps(config_schema(args.lang or settings.ui_language), ensure_ascii=False, indent=2))
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


def _nudge_waiting(rt: CliRuntime) -> None:
    """A destination setting changed: deliveries waiting for a channel are retried right away."""
    try:
        n = rt.service().retry_waiting()
    except Exception:  # storage issues are reported by doctor
        return
    if n:
        _print(t("cli.waiting_retried", rt.settings().ui_language, count=n))


def config_rows(rt: CliRuntime) -> list[dict[str, Any]]:
    """Effective value + origin of every setting (``default`` | ``configured`` | ``invalid``), with the
    channel/server a name resolved to (``resolved``, from the gateway's last delivery)."""
    settings = rt.settings()
    origin_of = getattr(rt, "config_origin", None)
    invalid = {w.split("=", 1)[0] for w in settings.warnings}
    resolved = _resolved_channels(rt)
    rows = []
    for key, opt in SPEC.items():
        value = getattr(settings, key)
        if key in invalid:
            origin = "invalid"
        elif callable(origin_of):
            origin = origin_of(key)
        else:
            origin = "default" if value == opt.default else "configured"
        row: dict[str, Any] = {"key": key, "group": opt.group, "value": list(value) if isinstance(value, tuple)
                               else value, "origin": origin}
        if key in resolved:
            row["resolved"] = resolved[key]
        rows.append(row)
    return rows


def _resolved_channels(rt: CliRuntime) -> dict[str, dict[str, Any]]:
    from .discord_ui.destination import REPORT_KV

    try:
        reports = rt.service().repo.kv_prefix(REPORT_KV)
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for raw in reports.values():
        try:
            rep = json.loads(raw)
        except ValueError:
            continue
        for step in rep.get("steps") or ():
            key = "delivery_auto_channel_names" if step.get("key") == "auto" else step.get("key")
            if key in SPEC and key not in out:
                out[key] = {k: v for k, v in step.items() if k != "key"}
        guild = rep.get("guild") or {}
        if guild.get("id") and "delivery_discord_guild" not in out:
            out["delivery_discord_guild"] = {"status": "ok", "guild_id": guild["id"], "guild_name": guild.get("name"),
                                             "source": guild.get("source")}
    return out


def _config_list(args: argparse.Namespace, rt: CliRuntime) -> int:
    rows = [r for r in config_rows(rt) if not args.group or r["group"] == args.group]
    llm = _llm_view(rt)
    if args.json:
        _print(json.dumps({"settings": rows, "llm": llm.to_dict() if llm else None}, ensure_ascii=False,
                          default=str))
        return 0
    group = None
    for r in rows:
        if r["group"] != group:
            group = r["group"]
            _print(f"[{t(f'cfg.group.{group}', rt.settings().ui_language)}]")
        res = r.get("resolved") or {}
        note = ""
        if res.get("channel_id"):
            note = f"  → #{res.get('channel_name') or res['channel_id']} ({res['channel_id']})"
        elif res.get("guild_id"):
            note = f"  → {res.get('guild_name') or res['guild_id']} ({res['guild_id']}, {res.get('source')})"
        elif res.get("detail"):
            note = f"  ! {res['detail']}"
        _print(f"  {r['key']} = {_fmt(r['value'])}  ({r['origin']}){note}")
    if llm is not None and (not args.group or args.group == "llm"):
        _print(f"[{t('cfg.group.llm', rt.settings().ui_language)}]")
        _print_llm(llm, rt.settings().ui_language, indent="  ")
    return 0


# -- llm --------------------------------------------------------------------------------------------
def _llm_store(rt: CliRuntime) -> Optional[Any]:
    getter = getattr(rt, "llm_store", None)
    return getter() if callable(getter) else None


def _llm_view(rt: CliRuntime) -> Optional[llm_config.LlmView]:
    store = _llm_store(rt)
    if store is None:
        return None
    try:
        return llm_config.view(store)
    except Exception:  # Hermes config unreadable: doctor reports it
        return None


def _print_llm(v: llm_config.LlmView, lang: str, indent: str = "") -> None:
    src = v.sources
    eff = v.effective_primary
    primary = v.primary.label()
    if v.primary.provider in ("auto", "main", ""):
        primary += f" → {eff.label()} ({t('cli.llm_main_model', lang)})"
    _print(f"{indent}provider/model: {primary}  [{src.get('provider')}]")
    _print(f"{indent}timeout: {v.timeout if v.timeout is not None else '-'}s  [{src.get('timeout')}]")
    if v.fallback_chain:
        for i, lk in enumerate(v.fallback_chain, start=1):
            _print(f"{indent}fallback {i}: {lk.label()}")
    else:
        _print(f"{indent}{t('cli.llm_no_fallback', lang)}")
    _print(f"{indent}({t('cli.llm_config_path', lang, path=llm_config.CONFIG_PATH)})")
    for p in v.problems:
        _print(f"{indent}! {p}")


def _llm(args: argparse.Namespace, rt: CliRuntime) -> int:
    lang = rt.settings().ui_language
    store = _llm_store(rt)
    if store is None:
        _print(t("cli.llm_unavailable", lang))
        return 1
    command = getattr(args, "llm_command", None) or "show"
    try:
        if command == "show":
            v = llm_config.view(store)
            if getattr(args, "json", False):
                _print(json.dumps(v.to_dict(), ensure_ascii=False))
            else:
                _print_llm(v, lang)
            return 0
        if command == "set":
            v = llm_config.set_primary(store, provider=args.provider, model=args.model, base_url=args.base_url,
                                       timeout=args.timeout)
        elif command == "fallback":
            sub = getattr(args, "fallback_command", None)
            if sub == "add":
                link = llm_config.parse_link(args.link)
                if args.base_url:
                    link = llm_config.validate_link(llm_config.Link(link.provider, link.model, args.base_url))
                v = llm_config.fallback_add(store, link, args.position)
            elif sub == "remove":
                v = llm_config.fallback_remove(store, args.which)
            elif sub == "clear":
                v = llm_config.fallback_clear(store)
            elif sub == "set":
                v = llm_config.fallback_set(store, [llm_config.parse_link(x) for x in args.links])
            else:
                _print("usage: hermes meeting-scribe llm fallback add|remove|clear|set")
                return 2
        elif command == "test":
            rows = llm_config.test_chain(store, timeout=args.timeout)
            if args.json:
                _print(json.dumps(rows, ensure_ascii=False))
            else:
                for r in rows:
                    _print(f"  [{'OK' if r['ok'] else 'FAIL':>4}] {r['role']}: {r['link']} — {r['detail']}")
            return 0 if rows and rows[0]["ok"] or any(r["ok"] for r in rows) else 1
        else:
            return 2
    except ValueError as exc:
        _print(str(exc))
        return 2
    except PermissionError as exc:
        _print(str(exc))
        return 1
    _print(t("cli.llm_saved", lang, path=llm_config.CONFIG_PATH))
    _print_llm(v, lang)
    return 0


_COMMANDS: dict[str, Callable[[argparse.Namespace, CliRuntime], int]] = {
    "setup": _setup, "doctor": _doctor, "status": _status, "list": _list, "show": _show,
    "reprocess": _reprocess, "export": _export, "config": _config, "google": cli_google.dispatch, "llm": _llm}


def dispatch(args: argparse.Namespace, rt: CliRuntime) -> int:
    command = getattr(args, "ms_command", None)
    if command is None:
        parser = getattr(args, "_ms_parser", None)
        if parser is not None:
            _print(parser.format_help())
        return 0
    return _COMMANDS[command](args, rt)
