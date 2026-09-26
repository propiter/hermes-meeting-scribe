"""Live Discord voice capture (Phase B, DESIGN §4/§13).

Modules: ``compat`` (probe of Hermes' private voice internals), ``receiver`` (ScribeReceiver +
TimedBuffer), ``tracks`` (per-speaker live Opus writers), ``session`` (one recording per guild),
``controller`` (``runtime.capture``), ``autojoin`` and ``checks`` (doctor).

``install(ctx, runtime)`` only builds the controller and registers doctor checks + the unload
hook; the Discord connection is attached later by ``meeting_scribe.discord_ui``'s platform
handler (it owns the single ``register_platform_handler('discord', …)`` factory).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .. import doctor
from ..audio.ffmpeg import Ffmpeg, FfmpegNotFound
from . import checks
from .controller import CaptureManager

log = logging.getLogger(__name__)

__all__ = ["install", "CaptureManager"]


def install(ctx: Any, runtime: Any) -> CaptureManager:
    def ffmpeg() -> Optional[Ffmpeg]:
        try:
            return runtime.ffmpeg()
        except FfmpegNotFound as exc:  # the session start reports it to the user
            log.warning("meeting-scribe: %s", exc)
            return None

    manager = CaptureManager(service=runtime.service, settings=runtime.settings, ffmpeg=ffmpeg)
    runtime.capture = manager
    checks.register(doctor.register_check)
    on_unload = getattr(ctx, "on_unload", None)
    if callable(on_unload):
        def meeting_scribe_capture_shutdown() -> None:
            manager.shutdown()  # plugin unload / gateway shutdown → finalize partial recordings
        on_unload(meeting_scribe_capture_shutdown)
    return manager
