"""Transcription subprocess: ``python -m meeting_scribe.transcribe.worker <job.json>``.

Runs in its own process so a multi-GB whisper model never lives in the gateway, a crash cannot
take the gateway down, and memory is returned to the OS when it exits. It owns its model
instance (sharing Hermes' STT singleton would thrash the gateway's voice-mode model).

Contract (all paths absolute):
  job.json  {"tracks": [{"speaker_id", "wav", "out"}], "model", "device", "compute_type",
             "cpu_threads", "language", "beam_size", "progress"}
  <out>     {"speaker_id", "done": true, "language", "duration", "segments": [{"start", "end",
             "text", "avg_logprob", "no_speech_prob", "words": [{"start","end","word","probability"}]}]}
  progress  {"done": [speaker_id...], "current", "fraction", "error"?}
Exit 0 on success, 2 on failure (error also written to the progress file).
Resumable: tracks whose ``out`` already says ``done`` are skipped.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Optional, Sequence


def _write(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _cuda_devices() -> int:
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except (ImportError, RuntimeError, OSError):
        return 0


def pick_device(device: str, compute_type: str, *, cuda_devices: Optional[int] = None) -> tuple[str, str]:
    """``auto``/``cuda`` fall back to CPU int8 when no CUDA device exists (DESIGN §6)."""
    n = _cuda_devices() if cuda_devices is None else cuda_devices
    dev = "cuda" if device in ("auto", "cuda") and n > 0 else "cpu"
    if compute_type != "auto" and not (dev == "cpu" and device == "cuda"):
        return dev, compute_type
    return dev, "float16" if dev == "cuda" else "int8"


def load_model(job: dict) -> Any:
    from faster_whisper import WhisperModel

    device, compute = pick_device(job.get("device", "auto"), job.get("compute_type", "auto"))
    return WhisperModel(job["model"], device=device, compute_type=compute,
                        cpu_threads=int(job.get("cpu_threads") or 0))


def _is_done(out: Path) -> bool:
    try:
        return bool(json.loads(out.read_text(encoding="utf-8")).get("done"))
    except (OSError, ValueError):
        return False


def _segment(seg: Any) -> dict:
    return {"start": float(seg.start), "end": float(seg.end), "text": str(seg.text),
            "avg_logprob": float(seg.avg_logprob), "no_speech_prob": float(seg.no_speech_prob),
            "words": [{"start": float(w.start), "end": float(w.end), "word": str(w.word),
                       "probability": float(w.probability)} for w in (seg.words or ())]}


def run_job(job_path: Path, model_factory: Callable[[dict], Any] = load_model) -> None:
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    progress = Path(job["progress"])
    tracks = job["tracks"]
    done = [t["speaker_id"] for t in tracks if _is_done(Path(t["out"]))]
    _write(progress, {"done": done, "current": None, "fraction": len(done) / max(1, len(tracks))})
    pending = [t for t in tracks if t["speaker_id"] not in done]
    if not pending:
        return
    model = model_factory(job)
    language = None if job.get("language") in (None, "", "auto") else job["language"]
    for track in pending:
        _write(progress, {"done": done, "current": track["speaker_id"], "fraction": len(done) / len(tracks)})
        segments, info = model.transcribe(
            track["wav"], language=language, beam_size=int(job.get("beam_size", 5)), vad_filter=True,
            word_timestamps=True, condition_on_previous_text=False)
        result = {"speaker_id": track["speaker_id"], "segments": [_segment(s) for s in segments],
                  "language": getattr(info, "language", None), "duration": getattr(info, "duration", None),
                  "done": True}
        _write(Path(track["out"]), result)
        done.append(track["speaker_id"])
    _write(progress, {"done": done, "current": None, "fraction": 1.0})


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        sys.stderr.write("usage: python -m meeting_scribe.transcribe.worker <job.json>\n")
        return 2
    job_path = Path(args[0])
    try:
        run_job(job_path, model_factory=load_model)
        return 0
    except Exception as exc:  # the parent needs the reason, not a bare exit code
        traceback.print_exc()
        try:
            progress = Path(json.loads(job_path.read_text(encoding="utf-8"))["progress"])
            _write(progress, {"error": f"{type(exc).__name__}: {exc}"})
        except (OSError, ValueError, KeyError):
            sys.stderr.write("could not write error to progress file\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
