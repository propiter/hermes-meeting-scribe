"""ffmpeg/ffprobe helpers shared by capture (Phase B), transcription and archiving.

Lives outside ``capture`` (DESIGN listed ``capture/ffmpeg.py``) so transcription and archive do
not depend on the Discord-specific package.
"""
