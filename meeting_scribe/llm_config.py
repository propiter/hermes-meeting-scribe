"""Models and fallbacks for meeting analysis (DESIGN §18).

Single source of truth: Hermes' own config block ``auxiliary.meeting_scribe`` — the same one Hermes'
auxiliary router reads at call time (``agent.auxiliary_client._get_auxiliary_task_config``: plugin
defaults layered under the user's values). The plugin never keeps a copy: ``llm show`` reads it,
``llm set`` / ``llm fallback …`` write it through Hermes' config writer (``save_config`` with
``merge_existing``, which preserves comments and sibling keys), and ``llm test`` probes each link.

Keys used: ``provider`` (``auto`` = Hermes' main model), ``model``, ``base_url``, ``timeout`` and
``fallback_chain`` (a list of ``{provider, model, base_url?}`` — Hermes skips an entry without a
model). Entries written by hand may carry more keys (``key_env``, ``api_key``, ``api_mode``,
``transport``…): every chain edit works on the original entries and keeps them untouched.
Printed URLs never show credentials (userinfo and query string are hidden). Hermes walks the chain on rate
limits, connection errors and payment errors (402) — NOT when a call hangs; that is what the
plugin's own ``analysis_timeout_seconds`` wall clock is for.

Hermes imports stay lazy (unit tests and the validator sandbox run without Hermes); a store is
injected so everything else is tested with a dictionary.
"""
from __future__ import annotations

import contextvars
import logging
import re
import threading
from urllib.parse import urlsplit, urlunsplit
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

AUX_TASK = "meeting_scribe"
CONFIG_PATH = f"auxiliary.{AUX_TASK}"
# Declared through ``ctx.register_auxiliary_task(defaults=...)``: no provider is imposed — ``auto``
# means "Hermes' main model". The timeout matches ``analysis_timeout_seconds``' default.
TASK_DEFAULTS: dict[str, Any] = {"provider": "auto", "model": "", "timeout": 600}
PROBE_PROMPT = "Reply with OK."
PROBE_TIMEOUT = 60.0
_PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
log = logging.getLogger(__name__)
MAX_ABANDONED = 2  # hung calls (per task) left running before new ones are refused (review M5)
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{6,}|Bearer\s+\S+|api[_-]?key[=:]\s*\S+)", re.IGNORECASE)


def safe_url(url: str) -> str:
    """``url`` without userinfo, query string or fragment (they may carry tokens), for display."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[url]"
    if not parts.scheme or not parts.netloc:
        return url.split("?", 1)[0].split("#", 1)[0]
    host = parts.netloc.rsplit("@", 1)[-1]
    shown = urlunsplit((parts.scheme, host, parts.path, "", ""))
    return shown + ("?…" if parts.query else "")


@dataclass(frozen=True)
class Link:
    provider: str
    model: str = ""
    base_url: str = ""

    def label(self) -> str:
        text = f"{self.provider}/{self.model}" if self.model else self.provider
        return f"{text} @ {safe_url(self.base_url)}" if self.base_url else text

    def to_dict(self) -> dict[str, str]:
        """For display/JSON output (the base_url is shown without credentials)."""
        out = self.entry()
        if self.base_url:
            out["base_url"] = safe_url(self.base_url)
        return out

    def entry(self) -> dict[str, str]:
        """What is written to config.yaml for a NEW chain entry."""
        out = {"provider": self.provider}
        if self.model:
            out["model"] = self.model
        if self.base_url:
            out["base_url"] = self.base_url
        return out


@dataclass(frozen=True)
class LlmView:
    primary: Link
    fallback_chain: tuple[Link, ...]
    timeout: Optional[float]
    sources: Mapping[str, str]  # key -> "hermes-config" | "plugin-default" | "hermes-main"
    main: Link  # Hermes' main model (what ``auto`` resolves to first)
    problems: tuple[str, ...] = field(default=())

    @property
    def effective_primary(self) -> Link:
        if self.primary.provider in ("", "auto", "main"):
            return Link(self.main.provider or "auto", self.primary.model or self.main.model, self.primary.base_url)
        return self.primary

    def to_dict(self) -> dict[str, Any]:
        return {"config_path": CONFIG_PATH, "provider": self.primary.provider, "model": self.primary.model,
                "base_url": safe_url(self.primary.base_url) if self.primary.base_url else "", "timeout": self.timeout,
                "effective": self.effective_primary.to_dict(), "main": self.main.to_dict(),
                "fallback_chain": [lk.to_dict() for lk in self.fallback_chain], "sources": dict(self.sources),
                "problems": list(self.problems)}


class AuxStore(Protocol):
    def user_task_config(self) -> Mapping[str, Any]: ...

    def main_model(self) -> Link: ...

    def write(self, values: Mapping[str, Any]) -> None: ...

    def probe(self, link: Link, timeout: float) -> tuple[bool, str]: ...


# -- parsing ----------------------------------------------------------------------------------------
def _clean(value: Any) -> str:
    return str(value or "").strip()


def _link(raw: Any) -> Optional[Link]:
    if not isinstance(raw, Mapping) or not _clean(raw.get("provider")):
        return None
    return Link(_clean(raw.get("provider")), _clean(raw.get("model")), _clean(raw.get("base_url")))


def parse_link(spec: str) -> Link:
    """``provider``, ``provider:model`` or ``custom:name:model`` (models may contain ``/`` and ``:``)."""
    text = _clean(spec)
    head, sep, rest = text.partition(":")
    if head == "custom" and sep:
        name, sep2, model = rest.partition(":")
        provider, model = f"custom:{name}", model if sep2 else ""
    else:
        provider, model = head, rest if sep else ""
    return validate_link(Link(provider, model.strip()))


def validate_link(link: Link) -> Link:
    if not _PROVIDER_RE.match(link.provider):
        raise ValueError(f"invalid provider {link.provider!r} (e.g. openrouter, anthropic, openai-codex, custom:name)")
    if any(c.isspace() for c in link.model):
        raise ValueError(f"invalid model {link.model!r} (no spaces)")
    if link.base_url and not re.match(r"^https?://\S+$", link.base_url):
        raise ValueError(f"invalid base_url {link.base_url!r} (http(s)://…)")
    return link


def view(store: AuxStore) -> LlmView:
    user = dict(store.user_task_config() or {})
    sources: dict[str, str] = {}
    problems: list[str] = []

    def pick(key: str) -> Any:
        if key in user and user[key] not in (None, ""):
            sources[key] = "hermes-config"
            return user[key]
        sources[key] = "plugin-default"
        return TASK_DEFAULTS.get(key, "")
    provider = _clean(pick("provider")) or "auto"
    model = _clean(pick("model"))
    base_url = _clean(user.get("base_url"))
    raw_timeout = pick("timeout")
    try:
        timeout = float(raw_timeout) if raw_timeout not in (None, "") else None
    except (TypeError, ValueError):
        timeout = None
        problems.append(f"{CONFIG_PATH}.timeout={raw_timeout!r} is not a number")
    chain_raw = user.get("fallback_chain")
    chain: list[Link] = []
    if chain_raw is not None and not isinstance(chain_raw, list):
        problems.append(f"{CONFIG_PATH}.fallback_chain must be a list")
    for i, entry in enumerate(chain_raw if isinstance(chain_raw, list) else []):
        lk = _link(entry)
        if lk is None:
            problems.append(f"{CONFIG_PATH}.fallback_chain[{i}] has no provider; Hermes skips it")
            continue
        chain.append(lk)
        if not lk.model:
            problems.append(f"{CONFIG_PATH}.fallback_chain[{i}] ({lk.provider}) has no model; Hermes skips it. "
                            f"Remove it (`llm fallback remove {len(chain)}`) and add it again as {lk.provider}:MODEL")
    sources["fallback_chain"] = "hermes-config" if chain_raw else "none"
    return LlmView(Link(provider, model, base_url), tuple(chain), timeout, sources, store.main_model(),
                   tuple(problems))


# -- writes -----------------------------------------------------------------------------------------
def set_primary(store: AuxStore, *, provider: Optional[str] = None, model: Optional[str] = None,
                base_url: Optional[str] = None, timeout: Optional[float] = None) -> LlmView:
    values: dict[str, Any] = {}
    if provider is not None:
        values["provider"] = validate_link(Link(_clean(provider) or "auto")).provider
    if model is not None:
        values["model"] = validate_link(Link("auto", _clean(model))).model
    if base_url is not None:
        values["base_url"] = validate_link(Link("auto", "", _clean(base_url))).base_url
    if timeout is not None:
        if timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds")
        values["timeout"] = timeout
    if not values:
        raise ValueError("nothing to change (use --provider, --model, --base-url or --timeout)")
    store.write(values)
    return view(store)


def defaults_among(values: Mapping[str, Any]) -> list[str]:
    """Keys whose value equals the plugin default: Hermes may drop them when saving (they are the
    default anyway), so the CLI says "using the default" instead of promising they were written."""
    out = []
    for key, value in values.items():
        default = TASK_DEFAULTS.get(key, _NO_DEFAULT)
        if default is _NO_DEFAULT:
            continue
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            same = isinstance(value, (int, float)) and float(value) == float(default)
        else:
            same = _clean(value) == _clean(default) or (key == "provider" and _clean(value) in ("", "auto"))
        if same:
            out.append(key)
    return out


_NO_DEFAULT = object()


def _require_model(link: Link) -> Link:
    link = validate_link(link)
    if not link.model:
        raise ValueError(f"a fallback needs a model: use {link.provider}:MODEL (Hermes skips a fallback without one)")
    return link


def _raw_chain(store: AuxStore) -> list[Any]:
    """The stored entries as they are (hand-written keys included); a non-list counts as empty."""
    raw = (store.user_task_config() or {}).get("fallback_chain")
    return list(raw) if isinstance(raw, list) else []


def _listed(raw: Sequence[Any]) -> list[tuple[int, Link]]:
    """``(index in raw, link)`` of the entries ``view`` lists (those with a provider)."""
    return [(i, lk) for i, lk in ((i, _link(e)) for i, e in enumerate(raw)) if lk is not None]


def _write_raw(store: AuxStore, raw: Sequence[Any]) -> LlmView:
    store.write({"fallback_chain": [dict(e) if isinstance(e, Mapping) else e for e in raw]})
    return view(store)


def fallback_set(store: AuxStore, links: Sequence[Link]) -> LlmView:
    """Replace the chain; an entry already present (same provider/model/base_url) keeps its extra keys."""
    links = [_require_model(lk) for lk in links]
    raw = _raw_chain(store)
    existing = {lk: dict(raw[i]) for i, lk in reversed(_listed(raw))}  # first occurrence wins
    return _write_raw(store, [existing.get(lk, lk.entry()) for lk in links])


def fallback_add(store: AuxStore, link: Link, position: Optional[int] = None) -> LlmView:
    link = _require_model(link)
    raw = _raw_chain(store)
    listed = _listed(raw)
    if any(lk == link for _i, lk in listed):
        raise ValueError(f"{link.label()} is already in the fallback chain")
    if position is None or position - 1 >= len(listed):
        raw.append(link.entry())
    else:
        raw.insert(listed[max(0, position - 1)][0], link.entry())
    return _write_raw(store, raw)


def fallback_remove(store: AuxStore, which: str) -> LlmView:
    """By 1-based position (as listed by ``llm show``), ``provider`` (every entry of it) or ``provider:model``."""
    raw = _raw_chain(store)
    listed = _listed(raw)
    which = _clean(which)
    if re.fullmatch(r"[0-9]+", which):
        i = int(which) - 1
        if not 0 <= i < len(listed):
            raise ValueError(f"no fallback at position {which} (chain has {len(listed)})")
        drop = {listed[i][0]}
    else:
        target = parse_link(which)
        drop = {i for i, lk in listed if lk.provider == target.provider and (not target.model or lk.model == target.model)}
        if not drop:
            raise ValueError(f"{which} is not in the fallback chain")
    return _write_raw(store, [e for i, e in enumerate(raw) if i not in drop])


def fallback_clear(store: AuxStore) -> LlmView:
    return _write_raw(store, [])


# -- probing ----------------------------------------------------------------------------------------
def redact(text: str) -> str:
    """Never echo credentials: Hermes' redactor when available, plus a conservative local pass."""
    try:
        from agent.redact import redact_sensitive_text
        text = redact_sensitive_text(text, force=True)
    except Exception:  # outside Hermes (tests): local pass only
        pass
    text = _URL_RE.sub(lambda m: safe_url(m.group(0)), text)
    return _SECRET_RE.sub("[redacted]", text)[:300]


def test_chain(store: AuxStore, timeout: float = PROBE_TIMEOUT) -> list[dict[str, Any]]:
    """Probe the primary link and every fallback with a tiny call; one row per link."""
    v = view(store)
    rows: list[dict[str, Any]] = []
    for role, link in [("primary", v.effective_primary)] + [(f"fallback {i}", lk)
                                                            for i, lk in enumerate(v.fallback_chain, start=1)]:
        try:
            ok, detail = store.probe(link, timeout)
        except Exception as exc:  # a broken probe is a failed link, never a crashed command
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        rows.append({"role": role, "link": link.label(), "ok": bool(ok), "detail": redact(str(detail))})
    return rows


class TooManyHungCalls(RuntimeError):
    """``MAX_ABANDONED`` earlier calls of this kind are still hanging: a new one is refused right away."""


_ABANDONED: dict[str, list[threading.Thread]] = {}
_ABANDONED_LOCK = threading.Lock()


def abandoned_alive(name: str) -> int:
    with _ABANDONED_LOCK:
        alive = [t for t in _ABANDONED.get(name, ()) if t.is_alive()]
        _ABANDONED[name] = alive
        return len(alive)


def run_with_deadline(fn: Callable[[], Any], seconds: float, *, name: str) -> Any:
    """Run ``fn`` in a daemon thread (profile contextvars carried) and wait at most ``seconds``.

    On timeout the thread is ABANDONED: Python cannot kill it; it finishes (or hangs) in the
    background and its result is discarded. Raises :class:`TimeoutError`. At most
    ``MAX_ABANDONED`` abandoned threads per ``name`` may be alive: beyond that the call fails fast
    with :class:`TooManyHungCalls` instead of piling up threads (and provider connections)."""
    hung = abandoned_alive(name)
    if hung >= MAX_ABANDONED:
        log.warning("meeting-scribe: %d earlier %s call(s) are still hanging; not starting another", hung, name)
        raise TooManyHungCalls(f"{hung} earlier {name} call(s) are still running after their deadline; "
                               "not starting another until they end (check the provider, or restart the gateway)")
    ctx = contextvars.copy_context()
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = ctx.run(fn)
        except BaseException as exc:  # handed to the caller
            box["error"] = exc
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        with _ABANDONED_LOCK:
            _ABANDONED.setdefault(name, []).append(thread)
        log.warning("meeting-scribe: %s call abandoned after %.0fs (it keeps running in the background)", name, seconds)
        raise TimeoutError(f"no answer within {seconds:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# -- Hermes-backed store ----------------------------------------------------------------------------
class HermesAuxStore:
    """Reads/writes ``auxiliary.meeting_scribe`` in the active profile's config.yaml."""

    def user_task_config(self) -> Mapping[str, Any]:
        from hermes_cli.config import load_config_readonly

        aux = (load_config_readonly() or {}).get("auxiliary") or {}
        task = aux.get(AUX_TASK) if isinstance(aux, Mapping) else None
        return dict(task) if isinstance(task, Mapping) else {}

    def main_model(self) -> Link:
        from hermes_cli.config import load_config_readonly

        model = (load_config_readonly() or {}).get("model")
        if isinstance(model, Mapping):
            return Link(_clean(model.get("provider")) or "auto", _clean(model.get("default") or model.get("model")),
                        _clean(model.get("base_url")))
        return Link("auto", _clean(model))

    def write(self, values: Mapping[str, Any]) -> None:
        import contextlib

        from hermes_cli import config as hc

        if hc.is_managed():
            raise PermissionError("this Hermes installation is managed; configuration cannot be changed here")
        try:
            from hermes_cli import managed_scope
            managed = [k for k in values if managed_scope.is_key_managed(f"{CONFIG_PATH}.{k}")]
        except Exception:  # older Hermes without managed scopes
            managed = []
        if managed:
            raise PermissionError(f"{CONFIG_PATH}.{managed[0]} is administrator-managed")
        try:  # the cross-process lock Hermes' own plugin-settings writer uses; best effort
            from hermes_cli.plugins_state import _locked_plugin_state
            lock: Any = _locked_plugin_state(hc.get_config_path())
        except Exception:
            lock = contextlib.nullcontext()
        preserve = {("auxiliary", AUX_TASK, key) for key in values}
        with lock:
            hc.read_user_config_raw()  # fail closed on a malformed config.yaml before writing
            hc.save_config({"auxiliary": {AUX_TASK: dict(values)}}, preserve_keys=preserve, merge_existing=True)

    def probe(self, link: Link, timeout: float) -> tuple[bool, str]:
        from agent.auxiliary_client import resolve_provider_client

        def call() -> tuple[bool, str]:
            client, model = resolve_provider_client(link.provider, link.model or None,
                                                    explicit_base_url=link.base_url or None, task=AUX_TASK)
            if client is None:
                return False, "provider not available (missing credentials or unknown provider)"
            resp = client.chat.completions.create(model=model or link.model,
                                                  messages=[{"role": "user", "content": PROBE_PROMPT}],
                                                  max_tokens=5, timeout=timeout)
            used = getattr(resp, "model", None) or model or link.model
            return True, f"answered ({used})"
        return run_with_deadline(call, timeout + 5, name="meeting-scribe-llm-probe")


# -- schema (virtual ``llm`` group) -----------------------------------------------------------------
def schema_fields(lang: str) -> list[dict[str, Any]]:
    from .i18n import t

    def f(key: str, kind: str, sub: str, **extra: Any) -> dict[str, Any]:
        return {"key": key, "type": kind, "group": "llm", "label": t(f"cfg.{key}.label", lang),
                "help": t(f"cfg.{key}.help", lang), "storage": "hermes", "path": f"{CONFIG_PATH}.{sub}", **extra}
    return [
        f("llm_provider", "str", "provider", default="auto", format="llm_provider",
          cli="hermes meeting-scribe llm set --provider"),
        f("llm_model", "str", "model", default="", cli="hermes meeting-scribe llm set --model"),
        f("llm_base_url", "str", "base_url", default="", cli="hermes meeting-scribe llm set --base-url"),
        f("llm_fallback_chain", "list", "fallback_chain", default=[],
          item={"provider": "str", "model": "str", "base_url": "str"},
          cli="hermes meeting-scribe llm fallback add|remove|clear|set"),
    ]
