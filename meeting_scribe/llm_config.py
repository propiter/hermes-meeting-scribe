"""Models and fallbacks for meeting analysis (DESIGN §18).

Single source of truth: Hermes' own config block ``auxiliary.meeting_scribe`` — the same one Hermes'
auxiliary router reads at call time (``agent.auxiliary_client._get_auxiliary_task_config``: plugin
defaults layered under the user's values). The plugin never keeps a copy: ``llm show`` reads it,
``llm set`` / ``llm fallback …`` write it through Hermes' config writer (``save_config`` with
``merge_existing``, which preserves comments and sibling keys), and ``llm test`` probes each link.

Keys used: ``provider`` (``auto`` = Hermes' main model), ``model``, ``base_url``, ``timeout`` and
``fallback_chain`` (a list of ``{provider, model?, base_url?}``). Hermes walks the chain on rate
limits, connection errors and payment errors (402) — NOT when a call hangs; that is what the
plugin's own ``analysis_timeout_seconds`` wall clock is for.

Hermes imports stay lazy (unit tests and the validator sandbox run without Hermes); a store is
injected so everything else is tested with a dictionary.
"""
from __future__ import annotations

import contextvars
import re
import threading
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
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{6,}|Bearer\s+\S+|api[_-]?key[=:]\s*\S+)", re.IGNORECASE)


@dataclass(frozen=True)
class Link:
    provider: str
    model: str = ""
    base_url: str = ""

    def label(self) -> str:
        text = f"{self.provider}/{self.model}" if self.model else self.provider
        return f"{text} @ {self.base_url}" if self.base_url else text

    def to_dict(self) -> dict[str, str]:
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
                "base_url": self.primary.base_url, "timeout": self.timeout,
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
        else:
            chain.append(lk)
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


def _write_chain(store: AuxStore, chain: Sequence[Link]) -> LlmView:
    store.write({"fallback_chain": [lk.to_dict() for lk in chain]})
    return view(store)


def fallback_set(store: AuxStore, links: Sequence[Link]) -> LlmView:
    return _write_chain(store, [validate_link(lk) for lk in links])


def fallback_add(store: AuxStore, link: Link, position: Optional[int] = None) -> LlmView:
    chain = list(view(store).fallback_chain)
    link = validate_link(link)
    if link in chain:
        raise ValueError(f"{link.label()} is already in the fallback chain")
    index = len(chain) if position is None else max(0, min(len(chain), position - 1))
    chain.insert(index, link)
    return _write_chain(store, chain)


def fallback_remove(store: AuxStore, which: str) -> LlmView:
    """By 1-based position, ``provider`` (every entry of it) or ``provider:model``."""
    chain = list(view(store).fallback_chain)
    which = _clean(which)
    if re.fullmatch(r"[0-9]+", which):
        i = int(which) - 1
        if not 0 <= i < len(chain):
            raise ValueError(f"no fallback at position {which} (chain has {len(chain)})")
        del chain[i]
    else:
        target = parse_link(which)
        kept = [lk for lk in chain if not (lk.provider == target.provider and (not target.model
                                                                                or lk.model == target.model))]
        if len(kept) == len(chain):
            raise ValueError(f"{which} is not in the fallback chain")
        chain = kept
    return _write_chain(store, chain)


def fallback_clear(store: AuxStore) -> LlmView:
    return _write_chain(store, [])


# -- probing ----------------------------------------------------------------------------------------
def redact(text: str) -> str:
    """Never echo credentials: Hermes' redactor when available, plus a conservative local pass."""
    try:
        from agent.redact import redact_sensitive_text
        text = redact_sensitive_text(text, force=True)
    except Exception:  # outside Hermes (tests): local pass only
        pass
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


def run_with_deadline(fn: Callable[[], Any], seconds: float, *, name: str) -> Any:
    """Run ``fn`` in a daemon thread (profile contextvars carried) and wait at most ``seconds``.

    On timeout the thread is ABANDONED: Python cannot kill it; it finishes (or hangs) in the
    background and its result is discarded. Raises :class:`TimeoutError`."""
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
