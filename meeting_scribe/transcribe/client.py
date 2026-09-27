"""Gateway-side client of the transcription worker (implements the ``Transcriber`` port).

Steps: ensure per-speaker tracks exist (restoring them from ``recording.mka`` for reprocess),
decode each to 16 kHz wav in ``.work/``, launch the worker with ``sys.executable`` under ``nice``,
poll its progress file, enforce a timeout scaled by the SUM of track durations (the worker
processes tracks sequentially), then merge results.
``.work/`` survives failures so a retry resumes from finished tracks.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from ..audio.archive import extract_tracks
from ..audio.ffmpeg import Ffmpeg, decode_to_wav, probe_duration
from ..config import Settings
from ..domain.models import Meeting, Speaker, Utterance
from .filters import RawSegment, RawWord
from .merge import TrackResult, merge_tracks

# Rough CPU real-time factors (audio seconds processed per wall second is 1/factor).
_RTF = {"tiny": 0.15, "base": 0.25, "small": 0.6, "medium": 1.5, "large": 3.0, "turbo": 1.5}


class TranscriptionError(RuntimeError):
    pass


def realtime_factor(model: str) -> float:
    return next((v for k, v in _RTF.items() if k in model), 2.0)


def worker_timeout(audio_seconds: float, model: str, *, floor: float = 600.0) -> float:
    """Generous (4× the estimate + model load) so slow CPUs finish, bounded so a hang is caught."""
    return max(floor, audio_seconds * realtime_factor(model) * 4 + 300)


def _segments(raw: Sequence[Mapping[str, Any]]) -> list[RawSegment]:
    return [RawSegment(start=float(s["start"]), end=float(s["end"]), text=str(s["text"]),
                       avg_logprob=float(s.get("avg_logprob", 0.0)), no_speech_prob=float(s.get("no_speech_prob", 0.0)),
                       words=tuple(RawWord(float(w["start"]), float(w["end"]), str(w["word"]),
                                           float(w.get("probability", 1.0))) for w in s.get("words") or ()))
            for s in raw]


class SubprocessTranscriber:
    def __init__(self, settings: Callable[[], Settings], ffmpeg: Callable[[], Ffmpeg], *,
                 command: Optional[Sequence[str]] = None, extra_job: Optional[Mapping[str, Any]] = None,
                 timeout_floor: float = 600.0, poll_interval: float = 1.0) -> None:
        self._settings = settings
        self._ffmpeg = ffmpeg
        self._command = list(command) if command else [sys.executable, "-m", "meeting_scribe.transcribe.worker"]
        self._extra = dict(extra_job or {})
        self._floor = timeout_floor
        self._poll = poll_interval

    # -- helpers --------------------------------------------------------------------------------
    def _tracks(self, ff: Ffmpeg, folder: Path) -> tuple[dict[str, Path], bool]:
        tracks = {p.stem: p for p in sorted((folder / "tracks").glob("*.ogg"))}
        if tracks:
            return tracks, False
        archive = folder / "recording.mka"
        if archive.exists():
            return extract_tracks(ff, archive, folder / "tracks"), True
        raise TranscriptionError(f"no audio tracks in {folder}")

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        pkg_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = os.pathsep.join(p for p in (pkg_root, env.get("PYTHONPATH", "")) if p)
        return env

    def _launch(self, job_path: Path, log: Any) -> subprocess.Popen[bytes]:
        """stderr goes to a file, not a pipe: a chatty worker would fill a pipe buffer and hang."""
        cmd = self._command + [str(job_path)]
        if shutil.which("nice"):
            cmd = ["nice", "-n", "10"] + cmd
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log, env=self._env())

    # -- port -----------------------------------------------------------------------------------
    def transcribe(self, meeting: Meeting, folder: Path,
                   progress: Optional[Callable[[str, float], None]] = None) -> list[Utterance]:
        settings, ff = self._settings(), self._ffmpeg()
        tracks, restored = self._tracks(ff, folder)
        work = folder / ".work"
        work.mkdir(parents=True, exist_ok=True)
        job_tracks, total = [], 0.0
        for sid, src in tracks.items():
            wav = work / f"{sid}.wav"
            if not wav.exists():
                decode_to_wav(ff, src, wav)
            total += probe_duration(ff, src)  # the worker runs tracks one after another (finding 9)
            job_tracks.append({"speaker_id": sid, "wav": str(wav), "out": str(work / f"{sid}.json")})
        job = {"tracks": job_tracks, "model": settings.transcribe_model, "device": settings.transcribe_device,
               "compute_type": settings.transcribe_compute_type, "cpu_threads": settings.effective_cpu_threads,
               "language": settings.transcribe_language, "beam_size": settings.transcribe_beam_size,
               "progress": str(work / "progress.json"), **self._extra}
        job_path = work / "job.json"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        self._run(job_path, work / "progress.json", worker_timeout(total, settings.transcribe_model,
                                                                  floor=self._floor), progress)
        by_id = {s.user_id: s for s in meeting.speakers}
        results = []
        for tr in job_tracks:
            data = json.loads(Path(tr["out"]).read_text(encoding="utf-8"))
            speaker = by_id.get(tr["speaker_id"], Speaker(tr["speaker_id"], tr["speaker_id"]))
            # Tracks are timeline-aligned to meeting t0 by the capture writer, so offset is 0.
            results.append(TrackResult(speaker, 0.0, _segments(data.get("segments") or ())))
        shutil.rmtree(work, ignore_errors=True)
        if restored:
            shutil.rmtree(folder / "tracks", ignore_errors=True)
        return merge_tracks(results)

    def _run(self, job_path: Path, progress_path: Path, timeout: float,
             progress: Optional[Callable[[str, float], None]]) -> None:
        log_path = job_path.with_name("worker.log")
        with open(log_path, "wb") as log:
            proc = self._launch(job_path, log)
            deadline = time.monotonic() + timeout
            last = -1.0
            while proc.poll() is None:
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.wait()
                    raise TranscriptionError(f"transcription worker timed out after {timeout:.0f}s")
                last = self._report(progress_path, progress, last)
                time.sleep(self._poll)
        self._report(progress_path, progress, last)
        stderr = log_path.read_text(encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise TranscriptionError(f"worker exited {proc.returncode}: {stderr.strip()[-1500:]}")

    @staticmethod
    def _report(path: Path, cb: Optional[Callable[[str, float], None]], last: float) -> float:
        if cb is None or not path.exists():
            return last
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return last  # the worker replaces the file atomically; a race just means "not yet"
        fraction = float(data.get("fraction") or 0.0)
        if fraction != last:
            cb(str(data.get("current") or ""), fraction)
        return fraction
