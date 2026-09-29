"""Secret scope for the plugin's background work on a multi-profile Hermes host (DESIGN §1.4).

The pipeline worker and the Google Meet pollers run outside any turn, so no profile scope is bound
around them. On a single-profile host that is fine: ``get_secret`` reads the process environment.
Once the process hosts several profiles (``gateway.multiplex_profiles``, a hosted room, a Desktop
``?profile=`` request, ``migrate --multiplex``) Hermes fails closed on an unscoped read. Every job
and every poll therefore binds the OWNER profile's secrets (not the launch profile's: the gateway may
have been started as another profile) for its own duration, and only when nothing is bound already.

Only Hermes' public ``agent.secret_scope`` API is used, probed at call time: a host without it (or
without multiplexing) gets a no-op, so older Hermes versions behave exactly as before.
"""
from __future__ import annotations

import contextlib
import inspect
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterator, Optional

_API = ("is_multiplex_active", "current_secret_scope", "set_secret_scope", "reset_secret_scope",
        "build_profile_secret_scope")


def _host_api() -> Optional[ModuleType]:
    """Hermes' secret-scope module when it offers everything we use; None on hosts that predate it."""
    try:
        from agent import secret_scope
    except ImportError:
        return None
    return secret_scope if all(callable(getattr(secret_scope, name, None)) for name in _API) else None


def _stamps_home(set_scope: Callable[..., object]) -> bool:
    """Newer hosts record which home a scope was built for (``profile_home=``); older ones do not."""
    try:
        return "profile_home" in inspect.signature(set_scope).parameters
    except (TypeError, ValueError):
        return False


@contextlib.contextmanager
def owner_job_scope(owner_home: Callable[[], Path]) -> Iterator[None]:
    """Bind the owner profile's secrets around one unit of background work, when the host needs it.

    No-op when the host does not multiplex profiles, when a scope is already bound (a turn, a
    Desktop request) or when the host has no secret-scope API. ``owner_home`` is only called when a
    scope is actually bound, so resolving the owner never costs anything on a single-profile host.
    """
    api = _host_api()
    if api is None or not api.is_multiplex_active() or api.current_secret_scope() is not None:
        yield
        return
    home = Path(owner_home())
    secrets = api.build_profile_secret_scope(home)
    if _stamps_home(api.set_secret_scope):
        token = api.set_secret_scope(secrets, profile_home=str(home))
    else:
        token = api.set_secret_scope(secrets)
    try:
        yield
    finally:
        api.reset_secret_scope(token)
