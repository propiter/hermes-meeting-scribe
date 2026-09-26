"""Doctor checks for live capture (registered through ``doctor.register_check``).

Each receives the doctor env (the runtime) and reads ``env.capture`` when it exists: inside the
gateway the controller knows the live adapter; from ``hermes meeting-scribe doctor`` (no gateway)
the checks fall back to importing Hermes' adapter module and give static hints.
"""
from __future__ import annotations

import importlib.util
from typing import Any, Callable

from ..doctor import Check
from .compat import probe_hermes

REQUIRED_PERMS = (("view_channel", "View Channel"), ("connect", "Connect"), ("send_messages", "Send Messages"),
                  ("create_public_threads", "Create Public Threads"))
OPTIONAL_PERMS = (("manage_nicknames", "Manage Nicknames (for the [REC] prefix)"),)
VOICE_MODULES = ("nacl", "davey")


def _live_adapter(env: Any) -> Any:
    return getattr(getattr(env, "capture", None), "adapter", None)


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _opus_loaded() -> tuple[bool, str]:
    try:
        import discord.opus as opus
    except Exception as exc:  # discord.py itself missing
        return False, f"discord.py not importable: {exc}"
    if opus.is_loaded():
        return True, "libopus loaded"
    try:
        opus._load_default()
    except Exception as exc:  # ctypes lookup failure
        return False, f"libopus not loadable: {exc}"
    return (True, "libopus loadable") if opus.is_loaded() else (False, "libopus not found (install libopus0)")


def check_discord_compat(env: Any) -> Check:
    cap = getattr(env, "capture", None)
    res = getattr(cap, "compat_result", None) if _live_adapter(env) is not None else None
    if res is None:
        res = probe_hermes()
    if res.ok:
        return Check.ok(f"Hermes Discord voice internals compatible ({len(res.checked)} checks)")
    return Check.fail("live capture disabled — " + "; ".join(res.problems))


def check_voice_deps(env: Any) -> Check:
    missing = [m for m in VOICE_MODULES if not _module_available(m)]
    opus_ok, opus_detail = _opus_loaded()
    if missing or not opus_ok:
        parts = [f"missing Python modules: {', '.join(missing)}"] if missing else []
        if not opus_ok:
            parts.append(opus_detail)
        return Check.fail("; ".join(parts) + " (install Hermes' discord voice extras)")
    return Check.ok(f"pynacl, davey, {opus_detail}")


def check_discord_intents(env: Any) -> Check:
    adapter = _live_adapter(env)
    client = getattr(adapter, "_client", None)
    if client is None:
        return Check.ok("voice_states intent is enabled by Hermes' Discord adapter (checked live in the gateway)")
    if getattr(getattr(client, "intents", None), "voice_states", False):
        return Check.ok("voice_states intent enabled")
    return Check.fail("voice_states intent disabled: auto-join/auto-leave and voice resolution cannot work")


def check_discord_permissions(env: Any) -> Check:
    names = ", ".join(label for _, label in REQUIRED_PERMS)
    optional = ", ".join(label for _, label in OPTIONAL_PERMS)
    client = getattr(_live_adapter(env), "_client", None)
    guilds = list(getattr(client, "guilds", None) or [])
    if not guilds:
        return Check.ok(f"bot needs {names}; optional: {optional} (Speak is not needed)")
    problems, notes = [], []
    for guild in guilds:
        perms = getattr(getattr(guild, "me", None), "guild_permissions", None)
        if perms is None:
            continue
        lacking = [label for attr, label in REQUIRED_PERMS if not getattr(perms, attr, False)]
        if lacking:
            problems.append(f"{guild.name}: missing {', '.join(lacking)}")
        if not all(getattr(perms, attr, False) for attr, _ in OPTIONAL_PERMS):
            notes.append(f"{guild.name}: no {optional}")
    if problems:
        return Check.warn("; ".join(problems + notes))
    return Check.ok(f"{len(guilds)} guild(s) OK" + (f" ({'; '.join(notes)})" if notes else ""))


def register(add: Callable[[str, Callable[[Any], Check]], None]) -> None:
    for name, fn in (("discord_compat", check_discord_compat), ("discord_voice_deps", check_voice_deps),
                     ("discord_intents", check_discord_intents), ("discord_permissions", check_discord_permissions)):
        add(name, fn)
