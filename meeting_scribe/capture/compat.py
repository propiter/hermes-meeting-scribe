"""Compatibility probe for the Hermes/discord.py voice internals the scribe relies on (DESIGN §4).

We subclass ``VoiceReceiver`` and use a handful of private adapter/connection attributes. Rather
than failing mid-meeting when Hermes refactors them, :func:`probe` checks the surface up front
(presence, callability, arity, plus one *source* check: the receiver must still append decoded
PCM with ``self._buffers[ssrc].extend(`` because that is the seam ``TimedBuffer`` hooks). If the
probe fails, capture is disabled with a clear message and ``doctor`` reports what changed.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Optional

BUFFER_SEAM = "self._buffers[ssrc].extend("

# (owner label, attribute, kind) — kind: "method" (callable on the class), "attr" (instance attr
# set in __init__), "coro" (async method).
_RECEIVER = (("start", "method"), ("stop", "method"), ("map_ssrc", "method"), ("_on_packet", "method"))
_RECEIVER_ATTRS = ("_lock", "_buffers", "_ssrc_to_user", "_dave_session", "_vc")
_ADAPTER = (("leave_voice_channel", "coro"), ("get_user_voice_channel", "coro"), ("_resolve_channel", "coro"),
            ("send", "coro"))
_ADAPTER_ATTRS = ("_voice_clients", "_voice_locks", "_client")
_CONN = (("add_socket_listener", "method"), ("remove_socket_listener", "method"), ("send_packet", "method"))
_CONN_ATTRS = ("hook", "secret_key", "ssrc", "dave_session")


@dataclass(frozen=True)
class CompatResult:
    ok: bool
    problems: tuple[str, ...]
    checked: tuple[str, ...]

    def summary(self) -> str:
        if self.ok:
            return f"{len(self.checked)} voice internals verified"
        return "; ".join(self.problems)


def _init_source(cls: type) -> str:
    try:
        return inspect.getsource(cls.__init__)
    except (OSError, TypeError):
        return ""


def _has_instance_attr(cls: type, name: str) -> bool:
    """True when ``name`` is a class attribute or assigned as ``self.<name>`` in ``__init__``."""
    if name in getattr(cls, "__annotations__", {}) or hasattr(cls, name):
        return True
    src = _init_source(cls)
    return f"self.{name} " in src or f"self.{name}:" in src or f"self.{name}=" in src


def _check_methods(label: str, cls: type, spec: tuple[tuple[str, str], ...], problems: list[str],
                   checked: list[str]) -> None:
    for name, kind in spec:
        checked.append(f"{label}.{name}")
        fn = getattr(cls, name, None)
        if not callable(fn):
            problems.append(f"{label}.{name} missing or not callable")
        elif kind == "coro" and not inspect.iscoroutinefunction(fn):
            problems.append(f"{label}.{name} is no longer a coroutine")


def _check_attrs(label: str, cls: type, names: tuple[str, ...], problems: list[str], checked: list[str]) -> None:
    for name in names:
        checked.append(f"{label}.{name}")
        if not _has_instance_attr(cls, name):
            problems.append(f"{label}.{name} missing")


def _check_receiver(cls: type, problems: list[str], checked: list[str]) -> None:
    _check_methods("VoiceReceiver", cls, _RECEIVER, problems, checked)
    _check_attrs("VoiceReceiver", cls, _RECEIVER_ATTRS, problems, checked)
    checked.append("VoiceReceiver.__init__(voice_client, allowed_user_ids)")
    try:
        params = list(inspect.signature(cls.__init__).parameters.values())[1:]
        required = [p for p in params if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY,
                                                                              p.POSITIONAL_OR_KEYWORD)]
        if len(required) != 1:
            problems.append(f"VoiceReceiver.__init__ signature changed: {inspect.signature(cls.__init__)}")
    except (TypeError, ValueError) as exc:
        problems.append(f"VoiceReceiver.__init__ not introspectable: {exc}")
    on_packet = getattr(cls, "_on_packet", None)
    if callable(on_packet):
        try:
            src = inspect.getsource(on_packet)
        except (OSError, TypeError):
            src = ""
        if BUFFER_SEAM not in src:
            problems.append(f"VoiceReceiver._on_packet no longer contains `{BUFFER_SEAM}`")


def probe(receiver_cls: Optional[type], adapter_cls: Optional[type], conn_cls: Optional[type],
          check_auth: Optional[Callable[..., Any]]) -> CompatResult:
    problems: list[str] = []
    checked: list[str] = []
    for label, cls, fn in (("VoiceReceiver", receiver_cls, _check_receiver),
                           ("DiscordAdapter", adapter_cls, None), ("VoiceConnectionState", conn_cls, None)):
        if cls is None:
            problems.append(f"{label} not importable")
            continue
        if fn is not None:
            fn(cls, problems, checked)
    if adapter_cls is not None:
        _check_methods("DiscordAdapter", adapter_cls, _ADAPTER, problems, checked)
        _check_attrs("DiscordAdapter", adapter_cls, _ADAPTER_ATTRS, problems, checked)
    if conn_cls is not None:
        _check_methods("VoiceConnectionState", conn_cls, _CONN, problems, checked)
        _check_attrs("VoiceConnectionState", conn_cls, _CONN_ATTRS, problems, checked)
    checked.append("_component_check_auth")
    if not callable(check_auth):
        problems.append("_component_check_auth missing (button authorization helper)")
    return CompatResult(not problems, tuple(problems), tuple(checked))


def probe_hermes() -> CompatResult:
    """Probe the live Hermes Discord adapter module and discord.py (imports lazily)."""
    try:
        from plugins.platforms.discord import adapter as mod  # type: ignore[import-not-found]
    except Exception as exc:  # any import failure means capture cannot work
        return CompatResult(False, (f"Hermes Discord adapter not importable: {type(exc).__name__}: {exc}",), ())
    try:
        from discord.voice_state import VoiceConnectionState
    except Exception as exc:  # discord.py missing or restructured
        conn: Optional[type] = None
        extra = (f"discord.voice_state.VoiceConnectionState not importable: {type(exc).__name__}: {exc}",)
    else:
        conn, extra = VoiceConnectionState, ()
    res = probe(getattr(mod, "VoiceReceiver", None), getattr(mod, "DiscordAdapter", None), conn,
                getattr(mod, "_component_check_auth", None))
    if not extra:
        return res
    return CompatResult(False, extra + tuple(p for p in res.problems if "VoiceConnectionState not" not in p),
                        res.checked)
