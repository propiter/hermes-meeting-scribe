"""Live Discord voice capture (Phase B).

Will hold ``compat.py`` (adapter probe), ``receiver.py`` (ScribeReceiver + TimedBuffer),
``tracks.py`` (per-speaker live Opus writers), ``session.py`` and ``autojoin.py``.
It hands finished recordings to :class:`meeting_scribe.pipeline.service.MeetingService`.
Intentionally empty: ``register()`` detects the missing ``install`` entry point and
reports capture as unavailable instead of pretending to record.
"""
