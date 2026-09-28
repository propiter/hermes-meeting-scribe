from pathlib import Path
from types import SimpleNamespace

from meeting_scribe import doctor
from meeting_scribe.config import settings_from_mapping
from meeting_scribe.doctor import (
    Check, CheckRegistry, check_capture, check_ffmpeg, check_kanban, check_linear, check_obsidian,
    check_settings, check_storage, run_checks,
)


def test_registry_and_exit_code():
    reg = CheckRegistry()
    reg.register_check("a", lambda env: Check.ok("fine"))
    reg.register_check("b", lambda env: Check.warn("hmm"))
    results, code = run_checks(reg, env=None)
    assert [r.name for r in results] == ["a", "b"] and code == 0
    reg.register_check("c", lambda env: Check.fail("broken"))
    assert run_checks(reg, env=None)[1] == 1


def test_crashing_check_is_a_failure():
    reg = CheckRegistry()
    reg.register_check("x", lambda env: 1 / 0)
    results, code = run_checks(reg, env=None)
    assert results[0].status == "fail" and "ZeroDivisionError" in results[0].detail and code == 1


def test_replacing_a_check_keeps_order():
    reg = CheckRegistry()
    reg.register_check("a", lambda env: Check.fail("x"))
    reg.register_check("b", lambda env: Check.ok("y"))
    reg.register_check("a", lambda env: Check.ok("z"))
    assert [r.detail for r in run_checks(reg, env=None)[0]] == ["z", "y"]


def test_default_registry_has_core_checks():
    names = doctor.registry.names()
    for expected in ("settings", "ffmpeg", "faster_whisper", "storage", "disk", "llm", "kanban", "linear",
                     "obsidian", "capture"):
        assert expected in names


def test_format_report():
    text = doctor.format_report([doctor.CheckResult("ffmpeg", "ok", "9.0"),
                                 doctor.CheckResult("linear", "warn", "off")], "en")
    assert "OK" in text and "WARN" in text and "1 warning" in text


# -- individual checks --------------------------------------------------------------------------
def env(tmp_path: Path, **settings):
    s = settings_from_mapping(settings)
    return SimpleNamespace(settings=lambda space=None: s, data_dir=lambda: tmp_path / "data", kanban_boards=lambda: [],
                           linear_backend=lambda: None, capture_status=lambda: (False, "Phase B not installed"),
                           llm_status=lambda: (True, "task meeting_scribe registered"))


def test_settings_warnings(tmp_path):
    assert check_settings(env(tmp_path)).status == "ok"
    bad = check_settings(env(tmp_path, **{"kanban_mode": "maybe"}))
    assert bad.status == "warn" and "kanban_mode" in bad.detail


def test_ffmpeg_real(tmp_path):
    res = check_ffmpeg(env(tmp_path))
    assert res.status in ("ok", "fail")
    if res.status == "ok":
        assert "libopus" in res.detail


def test_storage_creates_db_with_fts(tmp_path):
    res = check_storage(env(tmp_path))
    assert res.status == "ok" and (tmp_path / "data" / "index.sqlite").exists()


def test_obsidian(tmp_path):
    assert check_obsidian(env(tmp_path)).status == "ok"  # disabled is fine
    assert check_obsidian(env(tmp_path, **{"obsidian_vault_path": str(tmp_path / "x")})).status == "fail"
    (tmp_path / "v").mkdir()
    assert check_obsidian(env(tmp_path, **{"obsidian_vault_path": str(tmp_path / "v")})).status == "ok"


def test_linear(tmp_path):
    assert check_linear(env(tmp_path, **{"linear_mode": "off"})).status == "ok"
    assert check_linear(env(tmp_path)).status == "warn"

    class Backend:
        def viewer(self):
            return {"name": "Pedro"}

        def teams(self):
            return [{"key": "ENG"}]

    e = env(tmp_path)
    e.linear_backend = lambda: Backend()
    res = check_linear(e)
    assert res.status == "ok" and "Pedro" in res.detail and "ENG" in res.detail


def test_kanban(tmp_path):
    e = env(tmp_path)
    e.kanban_boards = lambda: [{"slug": "default"}]
    assert check_kanban(e).status == "ok"

    def broken():
        raise ImportError("no kanban")

    e.kanban_boards = broken
    assert check_kanban(e).status == "warn"
    assert check_kanban(env(tmp_path, **{"kanban_mode": "off"})).status == "ok"


def test_capture_reports_phase_b(tmp_path):
    res = check_capture(env(tmp_path))
    assert res.status == "warn" and "Phase B" in res.detail


# -- llm chain and delivery destination (DESIGN §18, §19) -----------------------------------------
class _Aux:
    def __init__(self, task):
        self.task = task

    def user_task_config(self):
        return self.task

    def main_model(self):
        from meeting_scribe.llm_config import Link
        return Link("mainprov", "main-model")


def test_llm_check_shows_the_chain_and_warns_without_fallback(tmp_path):
    from meeting_scribe.doctor import check_llm

    e = env(tmp_path)
    e.llm_store = lambda: _Aux({})
    res = check_llm(e)
    assert res.status == "warn" and "mainprov/main-model" in res.detail and "llm fallback add" in res.detail
    e.llm_store = lambda: _Aux({"fallback_chain": [{"provider": "prov-b", "model": "m"}]})
    res = check_llm(e)
    assert res.status == "ok" and "mainprov/main-model → prov-b/m" in res.detail


def test_delivery_check_reports_waiting_meetings_and_bad_names(tmp_path):
    import json

    from meeting_scribe.discord_ui.destination import REPORT_KV
    from meeting_scribe.doctor import check_delivery
    from meeting_scribe.pipeline.runner import WAITING_KV
    from meeting_scribe.storage.repo import Repository

    repo = Repository(tmp_path / "db.sqlite")
    svc = SimpleNamespace(repo=repo, waiting_destination=lambda: {
        k[len(WAITING_KV):]: v for k, v in repo.kv_prefix(WAITING_KV).items()})
    e = env(tmp_path)
    e.service = lambda: svc
    assert check_delivery(e).status == "ok"
    repo.kv_set(f"{REPORT_KV}.google_meet", json.dumps({
        "guild": {"id": "100", "name": "Example Team", "source": "only"}, "targets": [],
        "steps": [{"key": "google_meet_discord_channel", "status": "ambiguous", "detail": "2 text channels"}]}))
    res = check_delivery(e)
    assert res.status == "warn" and "Example Team" in res.detail and "2 text channels" in res.detail
    repo.kv_set(WAITING_KV + "m1", "waiting for a Discord channel: set one with `config set`")
    res = check_delivery(e)
    assert res.status == "warn" and "1 meeting(s) waiting" in res.detail and "config set" in res.detail
    repo.close()


def test_delivery_check_shows_destination_warnings(tmp_path):
    """Review M2: a private notes channel is used when configured, but doctor says who cannot see it."""
    import json

    from meeting_scribe.discord_ui.destination import REPORT_KV
    from meeting_scribe.doctor import check_delivery
    from meeting_scribe.storage.repo import Repository

    repo = Repository(tmp_path / "db.sqlite")
    svc = SimpleNamespace(repo=repo, waiting_destination=lambda: {})
    e = env(tmp_path)
    e.service = lambda: svc
    repo.kv_set(f"{REPORT_KV}.google_meet", json.dumps({
        "guild": {"id": "100", "name": "Example Team", "source": "only"}, "targets": ["300"], "steps": [],
        "warnings": ["google_meet_discord_channel: #notes is not visible to @everyone; only its members see the notes"]}))
    res = check_delivery(e)
    assert res.status == "warn" and "@everyone" in res.detail
    repo.close()


def test_delivery_check_lists_meetings_left_in_a_dm(tmp_path):
    from meeting_scribe.doctor import check_delivery
    from meeting_scribe.storage.repo import Repository

    repo = Repository(tmp_path / "db.sqlite")
    svc = SimpleNamespace(repo=repo, waiting_destination=lambda: {},
                          dm_notes=lambda: {"m1": "run `hermes meeting-scribe reprocess m1 --from deliver`"})
    e = env(tmp_path)
    e.service = lambda: svc
    res = check_delivery(e)
    assert res.status == "warn" and "1 meeting(s)" in res.detail and "--from deliver" in res.detail
    repo.close()
