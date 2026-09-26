import json
from types import SimpleNamespace

from meeting_scribe.transcribe import worker


class FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, path, **kw):
        self.calls.append((path, kw))
        words = [SimpleNamespace(start=0.1, end=0.4, word=" hola", probability=0.9)]
        segs = [SimpleNamespace(start=0.0, end=1.0, text=" hola", avg_logprob=-0.2, no_speech_prob=0.1, words=words),
                SimpleNamespace(start=1.0, end=2.0, text=" mundo", avg_logprob=-0.3, no_speech_prob=0.1, words=None)]
        return iter(segs), SimpleNamespace(duration=2.0, language="es", language_probability=0.99)


def _job(tmp_path, n=2, language="auto"):
    tracks = []
    for i in range(n):
        wav = tmp_path / f"{i}.wav"
        wav.write_bytes(b"")
        tracks.append({"speaker_id": str(i), "wav": str(wav), "out": str(tmp_path / f"{i}.json")})
    job = {"tracks": tracks, "model": "tiny", "device": "cpu", "compute_type": "int8", "cpu_threads": 2,
           "language": language, "beam_size": 5, "progress": str(tmp_path / "progress.json")}
    path = tmp_path / "job.json"
    path.write_text(json.dumps(job))
    return path


def test_run_job_writes_results_and_progress(tmp_path):
    model = FakeModel()
    worker.run_job(_job(tmp_path), model_factory=lambda job: model)
    res = json.loads((tmp_path / "0.json").read_text())
    assert res["speaker_id"] == "0" and res["language"] == "es" and res["done"] is True
    assert res["segments"][0]["words"][0]["word"] == " hola"
    assert res["segments"][1]["words"] == []
    prog = json.loads((tmp_path / "progress.json").read_text())
    assert prog["done"] == ["0", "1"] and prog["fraction"] == 1.0
    _, kw = model.calls[0]
    assert kw["vad_filter"] is True and kw["word_timestamps"] is True
    assert kw["condition_on_previous_text"] is False and kw["beam_size"] == 5 and kw["language"] is None


def test_language_pinned(tmp_path):
    model = FakeModel()
    worker.run_job(_job(tmp_path, 1, "es"), model_factory=lambda job: model)
    assert model.calls[0][1]["language"] == "es"


def test_resume_skips_done_tracks(tmp_path):
    job = _job(tmp_path)
    (tmp_path / "0.json").write_text(json.dumps({"speaker_id": "0", "done": True, "segments": []}))
    model = FakeModel()
    worker.run_job(job, model_factory=lambda j: model)
    assert [c[0] for c in model.calls] == [str(tmp_path / "1.wav")]


def test_model_not_loaded_when_everything_done(tmp_path):
    job = _job(tmp_path, 1)
    (tmp_path / "0.json").write_text(json.dumps({"speaker_id": "0", "done": True, "segments": []}))

    def boom(_job):
        raise AssertionError("model must not load")
    worker.run_job(job, model_factory=boom)


def test_device_selection():
    assert worker.pick_device("auto", "auto", cuda_devices=0) == ("cpu", "int8")
    assert worker.pick_device("auto", "auto", cuda_devices=1) == ("cuda", "float16")
    assert worker.pick_device("cpu", "float32", cuda_devices=1) == ("cpu", "float32")
    assert worker.pick_device("cuda", "auto", cuda_devices=0) == ("cpu", "int8")


def test_main_returns_error_code_and_writes_error(tmp_path, monkeypatch):
    job = _job(tmp_path, 1)

    def broken(_job):
        raise RuntimeError("no model")
    monkeypatch.setattr(worker, "load_model", broken)
    assert worker.main([str(job)]) == 2
    assert "no model" in json.loads((tmp_path / "progress.json").read_text())["error"]
