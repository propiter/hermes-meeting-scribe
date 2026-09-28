"""``hermes meeting-scribe doctor`` (DESIGN §10).

Checks live in a registry so Phase B can add its own (Discord compat probe, intents/permissions)
with ``doctor.registry.register_check(name, fn)`` without touching this module. A check receives a
``DoctorEnv`` and returns a :class:`Check`; a check that raises counts as a failure (a doctor that
crashes is useless exactly when things are broken).
"""
from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from .audio.ffmpeg import FfmpegNotFound, capabilities, resolve_ffmpeg
from .config import Settings
from .i18n import t

MIN_FREE_GB = 2.0


class DoctorEnv(Protocol):
    def settings(self) -> Settings: ...

    def data_dir(self) -> Path: ...

    def kanban_boards(self) -> list[dict[str, Any]]: ...

    def linear_backend(self) -> Optional[Any]: ...

    def capture_status(self) -> tuple[bool, str]: ...

    def llm_status(self) -> tuple[bool, str]: ...


@dataclass(frozen=True)
class Check:
    status: str  # ok | warn | fail
    detail: str

    @classmethod
    def ok(cls, detail: str = "") -> "Check":
        return cls("ok", detail)

    @classmethod
    def warn(cls, detail: str) -> "Check":
        return cls("warn", detail)

    @classmethod
    def fail(cls, detail: str) -> "Check":
        return cls("fail", detail)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str


CheckFn = Callable[[Any], Check]


class CheckRegistry:
    def __init__(self) -> None:
        self._checks: dict[str, CheckFn] = {}

    def register_check(self, name: str, fn: CheckFn) -> None:
        """Add or replace a check; replacing keeps the original position in the report."""
        self._checks[name] = fn

    def names(self) -> list[str]:
        return list(self._checks)

    def items(self) -> list[tuple[str, CheckFn]]:
        return list(self._checks.items())


def run_checks(reg: CheckRegistry, env: Any) -> tuple[list[CheckResult], int]:
    results: list[CheckResult] = []
    for name, fn in reg.items():
        try:
            res = fn(env)
        except Exception as exc:  # a crashing check is a failed check, never a crashed doctor
            res = Check.fail(f"{type(exc).__name__}: {exc}")
        results.append(CheckResult(name, res.status, res.detail))
    return results, int(any(r.status == "fail" for r in results))


def format_report(results: list[CheckResult], lang: str) -> str:
    label = {"ok": t("doctor.ok", lang), "warn": t("doctor.warn", lang), "fail": t("doctor.fail", lang)}
    lines = [t("doctor.header", lang)]
    lines += [f"  [{label[r.status]:>4}] {r.name}: {r.detail}" for r in results]
    lines.append(t("doctor.summary", lang, failed=sum(r.status == "fail" for r in results),
                   warned=sum(r.status == "warn" for r in results)))
    return "\n".join(lines)


# -- core checks ----------------------------------------------------------------------------------
def check_settings(env: DoctorEnv) -> Check:
    s = env.settings()
    return Check.warn("; ".join(s.warnings)) if s.warnings else Check.ok("valid")


def check_ffmpeg(env: DoctorEnv) -> Check:
    try:
        ff = resolve_ffmpeg(env.settings().audio_ffmpeg_path)
    except FfmpegNotFound as exc:
        return Check.fail(str(exc))
    caps = capabilities(ff)
    if not caps["libopus"]:
        return Check.fail(f"{ff.ffmpeg} has no libopus encoder")
    return Check.ok(f"{ff.ffmpeg} ({caps['version']}), ffprobe ok, libopus")


def check_faster_whisper(env: DoctorEnv) -> Check:
    if importlib.util.find_spec("faster_whisper") is None:
        return Check.fail("faster-whisper is not installed in this Python (pip install 'faster-whisper>=1.1,<2')")
    model = env.settings().transcribe_model
    try:
        from faster_whisper.utils import download_model

        path = download_model(model, local_files_only=True)
        return Check.ok(f"model {model} cached at {path}")
    except Exception as exc:  # not cached (or offline): first meeting will download it
        return Check.warn(f"model {model} not cached yet; first transcription downloads it ({type(exc).__name__})")


def check_storage(env: DoctorEnv) -> Check:
    from .storage.repo import Repository

    root = env.data_dir()
    repo = Repository(root / "index.sqlite")
    try:
        mode, version = repo.journal_mode(), repo.user_version()
    finally:
        repo.close()
    return Check.ok(f"{root} (sqlite {mode}, schema v{version}, FTS5)")


def check_disk(env: DoctorEnv) -> Check:
    root = env.data_dir()
    root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root).free / 1e9
    detail = f"{free:.1f} GB free (~20 MB per speaker-hour at 48 kbps)"
    return Check.ok(detail) if free >= MIN_FREE_GB else Check.warn(detail)


def check_llm(env: DoctorEnv) -> Check:
    """Reachability of the analysis model plus the configured chain; no fallback is a warning."""
    ok, detail = env.llm_status()
    getter = getattr(env, "llm_store", None)
    store = getter() if callable(getter) else None
    if store is None:
        return Check.ok(detail) if ok else Check.warn(detail)
    from . import llm_config

    try:
        v = llm_config.view(store)
    except Exception as exc:  # unreadable Hermes config
        return Check.warn(f"{detail}; cannot read {llm_config.CONFIG_PATH}: {type(exc).__name__}: {exc}")
    chain = " → ".join([v.effective_primary.label(), *(lk.label() for lk in v.fallback_chain)])
    parts = [detail, f"chain: {chain}", f"timeout {v.timeout or '-'}s", *v.problems]
    if not ok:
        return Check.warn("; ".join(parts))
    if not v.fallback_chain:
        return Check.warn("; ".join(parts + [t("doctor.llm_no_fallback", env.settings().ui_language)]))
    return Check.ok("; ".join(parts))


def check_delivery(env: Any) -> Check:
    """Where notes go (DESIGN §19): last resolution seen by the gateway and meetings waiting for a channel."""
    import json

    s = env.settings()
    if not s.delivery_discord_enabled:
        return Check.ok("disabled (delivery_discord_enabled=false)")
    service = getattr(env, "service", None)
    if not callable(service):
        return Check.ok("no runtime")
    from .discord_ui.destination import REPORT_KV

    svc = service()
    waiting = svc.waiting_destination()
    parts: list[str] = []
    problems: list[str] = []
    for key, raw in sorted(svc.repo.kv_prefix(REPORT_KV).items()):
        try:
            rep = json.loads(raw)
        except ValueError:
            continue
        source = key[len(REPORT_KV) + 1:] or "?"
        g = rep.get("guild") or {}
        where = f"{g.get('name') or g.get('id') or 'no server'}"
        target = (rep.get("targets") or ["—"])[0]
        parts.append(f"{source}: server {where} ({g.get('source') or '-'}), notes channel {target}")
        problems += [st["detail"] for st in rep.get("steps") or () if st.get("detail")
                     and st.get("status") not in ("ok", "unset", "none")]
        problems += [w for w in rep.get("warnings") or () if isinstance(w, str) and w]
    if not parts:
        parts.append("nothing delivered yet (channels are resolved on the first delivery)")
    dm_getter = getattr(svc, "dm_notes", None)
    dm_notes = dm_getter() if callable(dm_getter) else {}
    if dm_notes:
        problems.append(f"{len(dm_notes)} meeting(s) still in a direct message: {next(iter(dm_notes.values()))}")
    if waiting:
        first = next(iter(waiting.values()))
        return Check.warn("; ".join(parts + [f"{len(waiting)} meeting(s) waiting for a channel: {first}"] + problems))
    if problems:
        return Check.warn("; ".join(parts + problems))
    return Check.ok("; ".join(parts))


def check_kanban(env: DoctorEnv) -> Check:
    if env.settings().kanban_mode == "off":
        return Check.ok("disabled (kanban_mode=off)")
    try:
        boards = env.kanban_boards()
    except Exception as exc:  # kanban is optional; report, do not fail
        return Check.warn(f"kanban unavailable: {type(exc).__name__}: {exc}")
    return Check.ok(f"{len(boards)} board(s): {', '.join(b.get('slug', '?') for b in boards)}")


def check_linear(env: DoctorEnv) -> Check:
    if env.settings().linear_mode == "off":
        return Check.ok("disabled (linear_mode=off)")
    backend = env.linear_backend()
    if backend is None:
        return Check.warn(t("sink.linear_inactive", env.settings().ui_language))
    teams = backend.teams()
    who = backend.viewer().get("name") if hasattr(backend, "viewer") else "MCP"
    return Check.ok(f"connected as {who}; teams: {', '.join(str(x.get('key') or x.get('name')) for x in teams)}")


def check_obsidian(env: DoctorEnv) -> Check:
    vault = env.settings().obsidian_vault_path.strip()
    if not vault:
        return Check.ok("disabled (obsidian_vault_path empty)")
    path = Path(vault).expanduser()
    return Check.ok(str(path)) if path.is_dir() else Check.fail(f"vault not found: {path}")


def check_capture(env: DoctorEnv) -> Check:
    ok, detail = env.capture_status()
    return Check.ok(detail) if ok else Check.warn(detail)


_RANK = {"ok": 0, "warn": 1, "fail": 2}


def _space_slugs(env: Any) -> list[str]:
    """The install's spaces (``[""]`` for a runtime without spaces: the global settings)."""
    spaces = getattr(env, "spaces", None)
    return [sp.slug for sp in spaces().all()] if callable(spaces) else [""]


def check_google_meet(env: Any) -> Check:
    """DESIGN §17, §23: per space, client stored, token refreshable, last poll, notes channel."""
    slugs = _space_slugs(env)
    if len(slugs) == 1:
        return _google_of(env, slugs[0] or None)
    results = [(slug, _google_of(env, slug)) for slug in slugs]
    worst = max((r.status for _, r in results), key=_RANK.__getitem__)
    return Check(worst, " | ".join(f"[{slug}] {r.detail}" for slug, r in results))


def _google_of(env: Any, space: Optional[str]) -> Check:
    s = env.settings(space) if space else env.settings()
    if not s.google_meet_enabled:
        return Check.ok("disabled (google_meet_enabled=false)")
    files = getattr(env, "google_files", None)
    if not callable(files):
        return Check.warn("Google Meet import not available in this runtime")
    from .google.oauth import GoogleAuthError, GoogleDisconnected

    flag = f" --space {space}" if space and len(_space_slugs(env)) > 1 else ""
    if not files(space).client_path.exists():
        return Check.fail(f"no OAuth client; run `hermes meeting-scribe google connect{flag} --client-secret <file.json>`")
    try:
        env.google_credentials(space).access_token()
    except GoogleDisconnected as exc:
        return Check.fail(str(exc))
    except (GoogleAuthError, OSError) as exc:
        return Check.warn(f"token refresh failed: {exc}")
    channel = s.google_meet_discord_channel or s.delivery_discord_channel
    parts = ["token OK", f"notes channel: {channel or 'automatic (server system channel / #' + ', #'.join(s.delivery_auto_channel_names) + ')'}"]
    no_channel = None if channel else (
        "no notes channel set: Meet notes go to the server's automatic channel, or wait if there is none; "
        f"choose one with `hermes meeting-scribe config set google_meet_discord_channel \"#channel-name\"{flag}`")
    try:
        st = env.meet_importer(space).status()
    except Exception:  # storage issues are reported by the storage check
        st = {}
    if st.get("last_poll_at"):
        parts.append(f"last poll {st['last_poll_at']} ({'ok' if st.get('last_poll_ok') == '1' else 'error'})")
    if st.get("last_poll_ok") == "0":
        return Check.warn("; ".join(parts + [f"error: {st.get('last_error', '?')}"]))
    if st.get("records_given_up"):
        return Check.warn("; ".join(parts + [f"{st['records_given_up']} conference(s) skipped after repeated errors "
                                             f"({st.get('records_given_up_last', '')})"]))
    if no_channel:
        return Check.warn("; ".join(parts + [no_channel]))
    return Check.ok("; ".join(parts))


VOICE_LIMIT_NOTE = ("the bot joins one voice channel per server at a time: different servers record in parallel, "
                    "a second channel of the same server waits until the first recording stops")


def check_spaces(env: Any) -> Check:
    """DESIGN §23: spaces and their servers, the bot's servers no space owns, baseline backups."""
    spaces_of = getattr(env, "spaces", None)
    repo_of = getattr(env, "repo", None)
    if not callable(spaces_of) or not callable(repo_of):
        return Check.ok("no runtime")
    from .storage.baseline import backups

    spaces = spaces_of().all()
    repo = repo_of()
    parts = [f"{sp.slug} ({sp.name}): " + (", ".join(f"{n or g} ({g})" if n else g for g, n in sp.guilds)
                                             or "no Discord server") for sp in spaces]
    guilds, seen = repo.bot_guilds()
    stray = [(g, n) for g, n in guilds if repo.space_of_guild(g) is None]
    problems: list[str] = []
    if len(spaces) > 1 and stray:
        names = ", ".join(f"{n} ({g})" if n else g for g, n in stray)
        problems.append(f"the bot is in {len(stray)} server(s) no space owns, nothing is recorded there: {names}; "
                        "assign one with `hermes meeting-scribe space add-guild <space> <server-id>`")
    if seen is None:
        parts.append("the bot's servers are listed after the gateway connects to Discord")
    old = backups(env.data_dir())
    if old:
        parts.append(f"{len(old)} backup(s) of the pre-spaces store kept: " + ", ".join(p.name for p in old))
    parts.append(VOICE_LIMIT_NOTE)
    return Check.warn("; ".join(problems + parts)) if problems else Check.ok("; ".join(parts))


registry = CheckRegistry()
for _name, _fn in (("settings", check_settings), ("ffmpeg", check_ffmpeg), ("faster_whisper", check_faster_whisper),
                   ("storage", check_storage), ("disk", check_disk), ("llm", check_llm), ("kanban", check_kanban),
                   ("linear", check_linear), ("obsidian", check_obsidian), ("capture", check_capture),
                   ("google_meet", check_google_meet), ("delivery", check_delivery), ("spaces", check_spaces)):
    registry.register_check(_name, _fn)


def register_check(name: str, fn: CheckFn) -> None:
    """Module-level shortcut for Phase B: ``doctor.register_check("discord_compat", probe)``."""
    registry.register_check(name, fn)
