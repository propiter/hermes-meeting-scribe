"""An exclusive, re-entrant lock shared by threads AND processes (``fcntl.flock`` on a lock file).

Used for the Google token files and for retiring a pre-spaces database. Where ``fcntl`` is
unavailable (Windows) only the in-process lock applies.
"""
from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path
from typing import Iterator

try:  # POSIX: cross-process lock
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

_HELD = threading.local()  # lock paths this thread already holds (the lock is re-entrant)
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    key = str(path)
    held: set[str] = getattr(_HELD, "paths", None) or set()
    _HELD.paths = held
    if key in held:
        yield
        return
    with _THREAD_LOCKS_GUARD:
        tlock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    with tlock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(key, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            held.add(key)
            try:
                yield
            finally:
                held.discard(key)
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
