"""Process identity for job leases and recording ownership (DESIGN §15, review finding 2).

Several processes can open the same profile database: the gateway (which owns live capture and
the worker), a CLI/TUI ``hermes chat`` running ``/meeting`` commands, ``hermes meeting-scribe
reprocess --now``… Each gets an owner id ``<host>:<pid>:<nonce>``. Job rows carry the id of the
worker that leased them and a heartbeat; recording rows carry the id of the capturing process.
Liveness is judged by the heartbeat for jobs and by ``kill(pid, 0)`` for recordings (the SQLite
index is local — WAL does not work across hosts — so a same-host pid probe is sufficient).
"""
from __future__ import annotations

import os
import socket
import uuid
from functools import lru_cache
from typing import Optional

from ..domain.text import is_ascii_digits


@lru_cache(maxsize=1)
def _nonce() -> str:
    return uuid.uuid4().hex[:8]


def process_owner_id() -> str:
    """Stable for the life of this process; different after a restart even if the pid is reused."""
    return f"{socket.gethostname()}:{os.getpid()}:{_nonce()}"


def owner_alive(owner: Optional[str]) -> bool:
    """True when ``owner`` names ANOTHER process on this host that still runs.

    Our own id is reported dead on purpose: a recording we own that is not in our live set is an
    orphan of an earlier session of this same process (e.g. the capture task crashed).
    """
    if not owner or owner == process_owner_id():
        return False
    host, _, rest = owner.partition(":")
    pid_text = rest.partition(":")[0]
    if host != socket.gethostname() or not is_ascii_digits(pid_text):
        return False
    pid = int(pid_text)
    if pid == os.getpid():
        return False  # same pid, different nonce: a previous incarnation (fork/reload), not alive
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def capturing(owner: Optional[str], own: Optional[str] = None) -> bool:
    """True when the recording owned by ``owner`` is being captured right now: by us (``own``, the
    caller's own owner id; default this process) or by another live process on this host. Any
    process may ask — the CLI and Desktop read the gateway's recordings this way."""
    return bool(owner) and (owner == (own or process_owner_id()) or owner_alive(owner))


def owner_dead(owner: Optional[str]) -> bool:
    """True only when ``owner`` is PROVABLY gone: this host, a different process id that no longer
    exists, or a previous incarnation of our own pid. Unknown hosts or malformed ids are not dead —
    only their lease expiry may free their work."""
    if not owner or owner == process_owner_id():
        return False
    host, _, rest = owner.partition(":")
    pid_text = rest.partition(":")[0]
    if host != socket.gethostname() or not is_ascii_digits(pid_text):
        return False
    pid = int(pid_text)
    if pid == os.getpid():
        return True  # same pid, different nonce: an earlier incarnation of this process
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False
