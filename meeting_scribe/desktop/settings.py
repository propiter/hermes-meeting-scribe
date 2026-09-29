"""Settings, LLM chain, Google and doctor for the Desktop page (dashboard process, no Runtime).

Every value comes from the same place the CLI uses: plugin settings under
``plugins.entries.meeting-scribe.settings`` (written through Hermes' own ``save_plugin_setting``,
which enforces managed installs, administrator-managed keys and the cross-process lock) and the
model chain under ``auxiliary.meeting_scribe`` (``llm_config.HermesAuxStore``). The form is
generated from ``config.config_schema`` — the page holds no copy of the setting list.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional

from .. import doctor, llm_config, privacy
from ..config import (DESTINATION_KEYS, LEGACY_KEYS, RETIRED_KEYS, SPEC, Settings, canonical_key, config_schema, space_keys,
                      validate_value)
from ..llm_config import redact
from ..storage.repo import Repository
from ..storage.spaces import SpaceRow
from .queries import WAITING_KV

PLUGIN_ID = "meeting-scribe"
_MISSING = object()


# -- plugin settings store ------------------------------------------------------------------------
@dataclass
class SettingsStore:
    """``read()`` returns the plugin's config entry mapping; ``write(key, value)`` persists one key."""

    read: Callable[[], Mapping[str, Any]]
    write: Callable[[str, Any], None]

    def _lookup(self, key: str, default: Any = None) -> Any:
        entry = self.read() or {}
        for sub in ("settings", "config"):  # ``config`` is Hermes' legacy subtree
            node: Any = entry.get(sub) if isinstance(entry, Mapping) else None
            for seg in key.split("."):
                if not isinstance(node, Mapping) or seg not in node:
                    node = _MISSING
                    break
                node = node[seg]
            if node is not _MISSING:
                return node
        return default

    def settings(self, space: str = "", overrides: Optional[Mapping[str, Any]] = None) -> Settings:
        """The global values, with ``space``'s ``overrides`` on top when given."""
        return Settings.load(self._lookup, space, overrides)

    def origin(self, key: str) -> str:
        retired = [old for old, (new, _) in RETIRED_KEYS.items() if new == key]  # read in its place
        for name in (key, LEGACY_KEYS.get(key), *retired):
            if name and self._lookup(name, _MISSING) not in (_MISSING, None):
                return "configured"
        return "default"


def hermes_settings_store() -> SettingsStore:
    """Reads/writes through Hermes (resolved per call, so a request profile scope applies)."""
    def read() -> Mapping[str, Any]:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.plugins_state import _plugin_settings_entry

        return _plugin_settings_entry(load_config_readonly() or {}, PLUGIN_ID) or {}

    def write(key: str, value: Any) -> None:
        from hermes_cli.plugins_state import _plugin_relative_segments, save_plugin_setting

        save_plugin_setting(PLUGIN_ID, _plugin_relative_segments(key), value)
    return SettingsStore(read, write)


def settings_view(store: SettingsStore, lang: str, space: Optional[SpaceRow] = None) -> dict[str, Any]:
    """Schema (groups, fields with their ``scope``, localized labels) + current value/origin of every
    plugin setting. With ``space``, the values that space sees: origin ``space`` for its overrides."""
    overrides = dict(space.overrides) if space else {}
    settings = store.settings(space.slug, overrides) if space else store.settings()
    invalid = {w.split("=", 1)[0].removeprefix(f"space {space.slug}: " if space else "") for w in settings.warnings}
    values = {}
    for key in SPEC:
        value = getattr(settings, key)
        origin = ("invalid" if key in invalid else "space" if SPEC[key].scope == "space" and overrides.get(key) is not None
                  else store.origin(key))
        values[key] = {"value": list(value) if isinstance(value, tuple) else value, "origin": origin}
    return {"schema": config_schema(lang), "values": values, "warnings": list(settings.warnings),
            "global": [k for k, o in SPEC.items() if o.scope == "global"], "space": list(space_keys()),
            "space_slug": space.slug if space else None,
            "overrides": {k: v for k, v in overrides.items() if k in SPEC}}


def set_setting(store: SettingsStore, repo: Optional[Repository], key: str, raw: Any) -> dict[str, Any]:
    """Validate with the CLI's rules and persist; KeyError unknown key, ValueError invalid value,
    PermissionError managed install/key. A destination change re-queues deliveries waiting for a
    channel (a DB write the gateway worker picks up; no worker runs here)."""
    key = canonical_key(key)
    value = validate_value(key, raw)
    store.write(key, value)
    return {"key": key, "value": value, "scope": "global", "requeued": _nudge(repo, key)}


def set_space_setting(store: SettingsStore, repo: Repository, slug: str, key: str, raw: Any) -> dict[str, Any]:
    """``slug``'s override of ``key`` (``raw=None`` clears it, the global value applies again), validated
    like ``hermes meeting-scribe space set``; a machine-wide key is a ``SpaceError`` (400)."""
    from ..spaces import Spaces

    key, value = Spaces(lambda: repo, store._lookup).set_override(slug, canonical_key(key), raw)
    return {"key": key, "value": value, "scope": "space", "space": slug, "requeued": _nudge(repo, key)}


def _nudge(repo: Optional[Repository], key: str) -> int:
    if repo is not None and key in DESTINATION_KEYS:
        return requeue_waiting(repo)
    return 0


def requeue_waiting(repo: Repository) -> int:
    from datetime import datetime, timezone

    n = 0
    for k in repo.kv_prefix(WAITING_KV):
        mid = k[len(WAITING_KV):]
        job = repo.get_job(mid)
        if job is not None and job.state == "queued":
            repo.enqueue_job(mid, job.stage, now=datetime.now(timezone.utc))
            n += 1
    return n


# -- LLM chain ------------------------------------------------------------------------------------
def llm_view(store: Any) -> dict[str, Any]:
    return llm_config.view(store).to_dict()


def llm_update(store: Any, body: Mapping[str, Any]) -> dict[str, Any]:
    """``{provider?, model?, base_url?, timeout?, fallback_chain?: [{provider, model, base_url?}]}``.

    Validation is ``llm_config``'s (same as ``hermes meeting-scribe llm set|fallback set``); a base_url
    with embedded credentials is stored as typed but only ever displayed through ``safe_url``."""
    # Stage the complete edit before persisting anything. The real host holds its
    # cross-process config lock around the read/validate/write transaction.
    if hasattr(store, "update_atomic"):
        store.update_atomic(lambda staged: _llm_update(staged, body))
    else:
        staged = llm_config.BufferedAuxStore(store)
        _llm_update(staged, body)
        store.write(staged.changes)
    return llm_view(store)


def _llm_update(store: Any, body: Mapping[str, Any]) -> dict[str, Any]:
    current = llm_config.view(store)
    primary = {k: body[k] for k in ("provider", "model", "base_url", "timeout") if k in body}
    if "timeout" in primary and primary["timeout"] is not None:
        primary["timeout"] = float(primary["timeout"])
    if primary.get("timeout") is None:
        primary.pop("timeout", None)
    # The page only ever sees ``safe_url`` of a stored base_url: sending that display form back must
    # not overwrite the real value (which may carry credentials the page never had).
    if current.primary.base_url and primary.get("base_url") == llm_config.safe_url(current.primary.base_url):
        primary.pop("base_url")
    if primary:
        llm_config.set_primary(store, **primary)
    if "fallback_chain" in body:
        chain = body["fallback_chain"]
        if not isinstance(chain, list) or len(chain) > 10:
            raise ValueError("fallback_chain must be a list of at most 10 entries")
        if not all(isinstance(e, Mapping) for e in chain):
            raise ValueError("every fallback entry must be an object")
        shown = {(lk.provider, lk.model, llm_config.safe_url(lk.base_url) if lk.base_url else ""): lk
                 for lk in current.fallback_chain}
        links = []
        for e in chain:
            wanted = llm_config.Link(str(e.get("provider") or "").strip(), str(e.get("model") or "").strip(),
                                     str(e.get("base_url") or "").strip())
            links.append(shown.get((wanted.provider, wanted.model, wanted.base_url), wanted))
        if links:
            llm_config.fallback_set(store, links)
        else:
            llm_config.fallback_clear(store)
    if not any(k in body for k in ("provider", "model", "base_url", "timeout", "fallback_chain")):
        raise ValueError("nothing to change")
    return llm_view(store)


# -- doctor ---------------------------------------------------------------------------------------
@dataclass
class DashboardDoctorEnv:
    """What ``doctor.run_checks`` needs, without a Runtime. Checks that only the gateway can answer
    (live capture, LLM reachability through the gateway's client) say so instead of guessing."""

    store: SettingsStore
    root: Path
    repo: Repository
    secret: Callable[[str], Optional[str]] = lambda _name: None
    kanban: Any = None
    aux_store: Any = None
    owner: str = ""  # ``home.Owner.describe()``: the profile whose data this backend serves

    def owner_status(self) -> str:
        return f"this page shows the data of the owner, {self.owner}" if self.owner else ""

    def settings(self, space: Optional[str] = None) -> Settings:
        return self.spaces().settings(space)

    def spaces(self) -> Any:
        from ..spaces import Spaces

        return Spaces(lambda: self.repo, self.store._lookup, lambda: self.root)

    def data_dir(self) -> Path:
        return self.root

    def kanban_boards(self) -> list[dict[str, Any]]:
        if self.kanban is None:
            from ..sinks.kanban import HermesKanban
            self.kanban = HermesKanban()
        return self.kanban.list_boards()

    def linear_backend(self) -> Any:
        from ..sinks.linear import select_backend
        return select_backend(lambda: self.secret("LINEAR_API_KEY"), None)

    def capture_status(self) -> tuple[bool, str]:
        return True, "live capture runs inside the gateway (see `hermes meeting-scribe doctor` there)"

    def llm_status(self) -> tuple[bool, str]:
        return True, "not probed from Desktop (run `hermes meeting-scribe llm test`)"

    def llm_store(self) -> Any:
        return self.aux_store

    def google_files(self, space: Optional[str] = None) -> Any:
        from ..google.oauth import GoogleFiles
        return GoogleFiles(lambda: self.root, self.spaces().resolve(space).slug)

    def google_credentials(self, space: Optional[str] = None) -> Any:
        from ..google.oauth import GoogleCredentials
        return GoogleCredentials(self.google_files(space))

    def service(self) -> Any:
        from ..domain.models import KV_DM_NOTES

        repo = self.repo
        return SimpleNamespace(
            repo=repo,
            waiting_destination=lambda: {k[len(WAITING_KV):]: v for k, v in repo.kv_prefix(WAITING_KV).items()},
            dm_notes=lambda: {k[len(KV_DM_NOTES):]: v for k, v in repo.kv_prefix(KV_DM_NOTES).items()},
            dm_unreachable=lambda: {k[len(privacy.DM_UNREACHABLE_KV):]: v
                                    for k, v in repo.kv_prefix(privacy.DM_UNREACHABLE_KV).items()})

    def meet_importer(self, space: Optional[str] = None) -> Any:
        from ..google.importer import status_kv

        KV = status_kv(self.spaces().resolve(space).slug)
        return SimpleNamespace(status=lambda: {k[len(KV):]: v for k, v in self.repo.kv_prefix(KV).items()})


def run_doctor(env: DashboardDoctorEnv) -> dict[str, Any]:
    results, code = doctor.run_checks(doctor.registry, env)
    return {"exit_code": code, "checks": [{"name": r.name, "status": r.status, "detail": redact(r.detail)}
                                          for r in results]}


def dumps(value: Any) -> str:  # tests: prove no secret leaks through any response shape
    return json.dumps(value, default=str, ensure_ascii=False)
