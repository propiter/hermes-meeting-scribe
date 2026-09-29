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
from .config import TASKS_MEETING, TASKS_PROJECTS, TASKS_PROJECTS_INLINE, Settings
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


def _stale_channel(s: Settings, repo: Any, source: str, rep: dict[str, Any]) -> str:
    """Plain words for an explicit channel setting changed since the delivery ``rep`` of ``source``
    describes (``""`` when the report still matches the configuration)."""
    steps = {st.get("key"): st for st in rep.get("steps") or () if isinstance(st, dict)}
    keys = ("google_meet_discord_channel",) if source == "google_meet" else ()
    for key in keys + ("delivery_discord_channel",):
        if key not in steps and not getattr(s, key):
            continue
        now = current_channel(s, repo, key, steps.get(key))
        if now is steps.get(key):
            if now.get("channel_id"):  # the chain stopped here: later keys were never consulted
                return ""
            continue
        if not now:
            return f"{key} was cleared after the last delivery; the next one resolves the notes channel again"
        if now.get("channel_id"):
            return f"notes channel {key} = {now['value']} → #{now['channel_name']} ({now['channel_id']}), " \
                   "from the channel list; confirmed on the next delivery"
        return now.get("detail") or f"{key}: {NEXT_DELIVERY}"
    return ""


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
        kind = next((st.get("kind") for st in rep.get("steps") or () if st.get("channel_id") == target
                     and st.get("kind") in ("forum", "media")), "")
        channel = f"{kind} {target} (one post per meeting)" if kind else f"channel {target}"
        stale = _stale_channel(s, svc.repo, source, rep)
        if stale:  # the report resolved a value that is no longer configured: say what holds now
            parts.append(f"{source}: server {where} ({g.get('source') or '-'}), {stale}")
            continue
        parts.append(f"{source}: server {where} ({g.get('source') or '-'}), notes {channel}")
        problems += [st["detail"] for st in rep.get("steps") or () if st.get("detail")
                     and st.get("status") not in ("ok", "unset", "none")]
        problems += [w for w in rep.get("warnings") or () if isinstance(w, str) and w]
    if not parts:
        parts.append("nothing delivered yet (channels are resolved on the first delivery)")
    parts.append(tasks_placement_line(s))
    route_parts, route_problems = routes_summary(s, svc.repo)
    parts += route_parts
    problems += route_problems
    problems += withdraw_problems(svc.repo)
    dm_getter = getattr(svc, "dm_notes", None)
    dm_notes = dm_getter() if callable(dm_getter) else {}
    if dm_notes:
        problems.append(f"{len(dm_notes)} meeting(s) still in a direct message: {next(iter(dm_notes.values()))}")
    unreachable_getter = getattr(svc, "dm_unreachable", None)
    unreachable = unreachable_getter() if callable(unreachable_getter) else {}
    if unreachable:
        mid, why = next(iter(unreachable.items()))
        problems.append(f"{len(unreachable)} direct-messages meeting(s) did not reach every participant "
                        f"({mid}: {why})")
    if waiting:
        first = next(iter(waiting.values()))
        return Check.warn("; ".join(parts + [f"{len(waiting)} meeting(s) waiting for a channel: {first}"] + problems))
    if problems:
        return Check.warn("; ".join(parts + problems))
    return Check.ok("; ".join(parts))


def tasks_placement_line(s: Settings) -> str:
    """Where the tasks of a (non-private) meeting are posted, in plain words (DESIGN §16.1)."""
    dm = "each assignee also gets a DM" if s.delivery_dm_assignees else "no assignee DMs (delivery_dm_assignees=false)"
    where = {
        TASKS_MEETING: "every task goes with the notes (one place per meeting; 📁 Move takes one to a project channel)",
        TASKS_PROJECTS: "tasks go to their project's channel, in a thread per meeting",
        TASKS_PROJECTS_INLINE: "tasks go straight into their project's channel",
    }[s.delivery_tasks_placement]
    return (f"tasks: delivery_tasks_placement={s.delivery_tasks_placement}: {where}; {dm}; private and "
            "direct-message meetings keep their own rules; a change applies to new deliveries and to "
            "`reprocess <id> --from deliver`")


def withdraw_problems(repo: Any) -> list[str]:
    """Private meetings whose public copies could not be fully withdrawn yet (DESIGN §19.2): they are
    emptied/renamed where possible and retried on every delivery; the bot needs the named permissions."""
    import json

    from .discord_ui.withdraw import PENDING_KV

    out = []
    for key, raw in sorted(repo.kv_prefix(PENDING_KV).items()):
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            data = {}
        missing = ", ".join(data.get("missing") or ()) or "Manage Messages, Manage Threads"
        out.append(f"private meeting {key[len(PENDING_KV):]}: {data.get('items', '?')} public copy(ies) not fully "
                   f"withdrawn yet (retried on each delivery); give the bot {missing} in those channels, then run "
                   f"`hermes meeting-scribe reprocess {key[len(PENDING_KV):]} --from deliver`")
    return out


def route_rows(s: Settings, repo: Optional[Any]) -> list[dict[str, Any]]:
    """Every ``meeting_routes`` rule of ``s`` with what the gateway last resolved for it (DESIGN §19.2);
    without storage (``repo=None``) the rules as written."""
    import json

    from .discord_ui.destination import ROUTES_REPORT_KV

    raw = repo.kv_prefix(ROUTES_REPORT_KV) if repo is not None else {}
    own = ROUTES_REPORT_KV + (s.space or "")
    seen: dict[str, dict[str, Any]] = {}
    # this space's report first; the global view (no space) takes any space's check of the same rule
    for key in sorted(raw, key=lambda k: k != own):
        if s.space and key != own:
            continue
        try:
            rows = json.loads(raw[key] or "[]")
        except ValueError:
            continue
        for r in rows if isinstance(rows, list) else ():
            if isinstance(r, dict) and r.get("origin"):
                seen.setdefault(str(r["origin"]), r)
    catalog = space_catalog(repo, s.space) if repo is not None else None
    rows = []
    for rule in s.routes():
        row = {"origin": rule.origin, "kind": rule.kind, "channel": rule.channel, "private": rule.private,
               "mode": rule.mode, "status": "invalid" if rule.error else "not_checked", "detail": rule.error,
               "text": rule.text}
        if catalog is not None and not rule.error:  # names and a first check from the channel catalog
            checked = catalog.verify(rule)
            row.update(origin_check=checked["origin_check"], target_check=checked["target_check"])
            target = checked["target_check"] or {}
            if target.get("id"):
                row.update(channel_id=target["id"], channel_name=target["name"], target_kind=target["kind"],
                           public=target["public"])
            if checked["status"] != "not_checked":
                row.update(status="ok" if checked["status"] == "ok" else "problem", detail=checked["detail"])
            if checked["warning"]:
                row["warning"] = checked["warning"]
        found = seen.get(rule.origin)
        if found and not rule.error and found.get("channel") == rule.channel:
            row.update({k: v for k, v in found.items() if k not in ("origin", "kind", "channel", "private")})
        rows.append(row)
    return rows


def space_guilds(repo: Any, space: str) -> Optional[list[str]]:
    """The Discord servers whose channels ``space`` may see (DESIGN §23); ``None``: every server the bot
    reported. An install with ONE space (or none yet) owns every server — an unowned one joins it on
    first use (``claim_guild``) — so nothing of another team can show. With several spaces, only the
    servers assigned to ``space`` (none: it sees no channel); the operator's global view (no space)
    sees the servers assigned to some space, never an unowned one."""
    rows = repo.list_spaces()
    if len(rows) <= 1 and (not space or not rows or rows[0].slug == space):
        return None
    if not space:
        return list(dict.fromkeys(g for r in rows for g in r.guild_ids))
    row = next((r for r in rows if r.slug == space), None)
    return list(row.guild_ids) if row is not None else []


def space_catalog(repo: Any, space: str) -> Any:
    """The channel catalog of ``space``'s servers only; a space without servers sees no channel."""
    from .channel_catalog import Catalog

    return Catalog.load(repo, space_guilds(repo, space))


CHANNEL_KEYS = ("delivery_discord_channel", "google_meet_discord_channel", "delivery_fallback_channel")
NEXT_DELIVERY = "changed since the last delivery; it will be checked on the next one"


def current_channel(s: Settings, repo: Optional[Any], key: str, step: Optional[dict[str, Any]]) -> dict[str, Any]:
    """What ``config list``/``doctor`` show for channel setting ``key``: the gateway's last resolution
    (``step``) only while it resolved the value configured NOW; otherwise the channel catalog's view of
    the current value (like ``meeting_routes``), or ``not_checked`` until the next delivery. A report
    of an older value must never pass for the current one."""
    from .channel_catalog import TARGET_KINDS
    from .config import channel_ref

    kind, ref = channel_ref(getattr(s, key))
    if step and str(step.get("value") or "") == ref:
        return step
    if not kind:
        return {}
    catalog = space_catalog(repo, s.space) if repo is not None else None
    found = catalog.find(ref, TARGET_KINDS) if catalog is not None else None
    if found is not None and found.status == "ok":
        return {"value": ref, "status": "ok", "channel_id": found.id, "channel_name": found.name,
                "kind": found.kind, "source": "catalog"}
    if found is not None and found.status not in ("unknown", "no_servers"):
        return {"value": ref, "status": found.status, "detail": f"{key}: {found.detail}"}
    return {"value": ref, "status": "not_checked", "detail": f"{key}: {NEXT_DELIVERY}"}


def route_text(row: dict[str, Any]) -> str:
    """``Leadership (voice channel, 300) → forum #leadership-notes (123, private), private``."""
    kinds = {"voice": "voice channel", "category": "category", "meet": "Google Meet",
             "any": "unreadable, covers every meeting"}
    origin = (row.get("origin_check") or {})
    where = kinds.get(row["kind"], row["kind"])
    label = row["origin"]
    if origin.get("id"):
        label = origin.get("name") or label
        where += f", {origin['id']}"
    mode = row.get("mode") or ("private" if row.get("private") else "normal")
    if mode == "dm":
        return f"{label} ({where}) → direct messages to each participant, dm"
    target = row.get("channel_name") or row.get("channel") or "?"
    target = f"#{target}" if not str(target).isdigit() else str(target)
    extra = []
    if row.get("channel_id") and (row.get("channel_name") or str(row["channel_id"]) != str(row.get("channel"))):
        extra.append(str(row["channel_id"]))
    if row.get("public") is not None:
        extra.append("visible to everyone" if row["public"] else "private channel")
    if extra:
        target += f" ({', '.join(extra)})"
    kind = {"forum": "forum ", "media": "forum "}.get(str(row.get("target_kind") or ""), "")
    return f"{label} ({where}) → {kind}{target}, {mode}"


def routes_summary(s: Settings, repo: Any) -> tuple[list[str], list[str]]:
    """``(lines, problems)`` for doctor: each rule resolved, and what is wrong or risky."""
    lines, problems = [], []
    for row in route_rows(s, repo):
        checked = "" if row["status"] != "not_checked" else " (not checked against Discord yet)"
        lines.append("meeting_routes: " + route_text(row) + checked)
        where = f"meeting_routes[{row['origin']}]"
        if row["status"] == "invalid":
            scope = "every meeting of this space waits" if row["kind"] == "any" else "its meetings wait"
            problems.append(f"{where}: {row['detail']}; {scope} (never published openly) until it is fixed")
        elif row["status"] not in ("ok", "not_checked"):
            problems.append(f"{where}: {row.get('detail') or row['status']}; its meetings wait")
        if row.get("warning") and str(row["warning"]) not in problems:
            problems.append(str(row["warning"]))
    return lines, problems


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


def check_owner(env: Any) -> Check:
    """DESIGN §1.5: which profile owns the installation (records, processes, keeps the data)."""
    status = getattr(env, "owner_status", None)
    detail = status() if callable(status) else ""
    return Check.ok(detail) if detail else Check.ok("single profile")


def check_capture(env: DoctorEnv) -> Check:
    ok, detail = env.capture_status()
    return Check.ok(detail) if ok else Check.warn(detail)


MISSING_AUDIO_RECENT = 20


def check_missing_audio(env: Any) -> Check:
    """Recent recordings in which someone was in the call but their voice never arrived (DESIGN §4.1)."""
    service = getattr(env, "service", None)
    if not callable(service):
        return Check.ok("no runtime")
    hits = [f"{m.id} ({', '.join(m.missing_audio_names)})"
            for m in service().repo.list_meetings(limit=MISSING_AUDIO_RECENT) if m.missing_audio]
    if not hits:
        return Check.ok(f"every participant was heard in the last {MISSING_AUDIO_RECENT} meetings")
    return Check.warn(f"audio of people in the call was not captured in {len(hits)} of the last "
                      f"{MISSING_AUDIO_RECENT} meetings: " + "; ".join(hits)
                      + ". Discord did not deliver their voice; see the gateway log (\"audio not captured\")")


_RANK = {"ok": 0, "warn": 1, "fail": 2}


def _space_slugs(env: Any) -> list[str]:
    """The install's spaces (``[""]`` for a runtime without spaces: the global settings)."""
    spaces = getattr(env, "spaces", None)
    return ([sp.slug for sp in spaces().all()] or [""]) if callable(spaces) else [""]


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
    if not callable(spaces_of):
        return Check.ok("no runtime")
    from .storage.baseline import backups

    registry_ = spaces_of()
    spaces, repo = registry_.all(), registry_.repo
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
for _name, _fn in (("owner", check_owner), ("settings", check_settings), ("ffmpeg", check_ffmpeg), ("faster_whisper", check_faster_whisper),
                   ("storage", check_storage), ("disk", check_disk), ("llm", check_llm), ("kanban", check_kanban),
                   ("linear", check_linear), ("obsidian", check_obsidian), ("capture", check_capture),
                   ("missing_audio", check_missing_audio),
                   ("google_meet", check_google_meet), ("delivery", check_delivery), ("spaces", check_spaces)):
    registry.register_check(_name, _fn)


def register_check(name: str, fn: CheckFn) -> None:
    """Module-level shortcut for Phase B: ``doctor.register_check("discord_compat", probe)``."""
    registry.register_check(name, fn)
