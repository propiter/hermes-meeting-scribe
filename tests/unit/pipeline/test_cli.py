from datetime import timedelta
import argparse
import json

import pytest

from meeting_scribe import cli
from meeting_scribe.domain.models import MeetingState, Stage

from .test_commands import make, processed


class FakeRuntime:
    """Just enough runtime for the CLI: a real service over tmp storage + dict config."""

    def __init__(self, service, cfg):
        self._service = service
        self.cfg = cfg
        self.started = False

    def service(self):
        return self._service

    def settings(self):
        from meeting_scribe.config import settings_from_mapping
        return settings_from_mapping(self.cfg)

    def set_config(self, key, value):
        self.cfg[key] = value

    def doctor_env(self):
        return None


def parse(argv):
    parser = argparse.ArgumentParser(prog="hermes meeting-scribe")
    cli.setup_parser(parser)
    return parser.parse_args(argv)


@pytest.fixture
def rt(prepo, layout, settings, clock, meeting):
    _, service, runner = make(prepo, layout, settings, clock)
    mid = processed(service, runner, meeting)
    r = FakeRuntime(service, {})
    r.mid = mid
    return r


def run(rt, argv, capsys):
    code = cli.dispatch(parse(argv), rt)
    return code, capsys.readouterr().out


def test_list_and_show(rt, capsys):
    code, out = run(rt, ["list"], capsys)
    assert code == 0 and rt.mid in out and "Informe semanal" in out
    code, out = run(rt, ["show", rt.mid[:5]], capsys)
    assert code == 0 and "Enviar informe" in out
    code, out = run(rt, ["show", "zzz"], capsys)
    assert code == 1


def test_status_json(rt, capsys):
    code, out = run(rt, ["status", "--json"], capsys)
    data = json.loads(out)
    assert code == 0 and data["recent"][0]["id"] == rt.mid


def test_export(rt, capsys, tmp_path):
    code, out = run(rt, ["export", rt.mid, "--format", "json", "--out", str(tmp_path / "x.json")], capsys)
    assert code == 0 and json.loads((tmp_path / "x.json").read_text())["notes"]["meeting_title"] == "Informe semanal"
    code, out = run(rt, ["export", rt.mid, "--format", "md"], capsys)
    assert code == 0 and "# Informe semanal" in out


def test_reprocess_now(rt, capsys):
    code, out = run(rt, ["reprocess", rt.mid, "--from", "analyze", "--now"], capsys)
    assert code == 0 and "done" in out
    assert rt.service().repo.get_meeting(rt.mid).state is MeetingState.DONE


def test_reprocess_queue_only(rt, capsys):
    code, out = run(rt, ["reprocess", rt.mid, "--from", "deliver"], capsys)
    assert code == 0 and rt.service().repo.get_job(rt.mid).state == "queued"


def test_config_get_set(rt, capsys):
    code, out = run(rt, ["config", "get", "kanban_mode"], capsys)
    assert code == 0 and out.strip() == "approve"
    code, _ = run(rt, ["config", "set", "linear.mode", "off"], capsys)  # legacy spelling still accepted
    assert code == 0 and rt.cfg["linear_mode"] == "off" and "linear.mode" not in rt.cfg
    code, _ = run(rt, ["config", "set", "kanban_mode", "auto"], capsys)
    assert code == 0 and rt.cfg["kanban_mode"] == "auto"
    code, out = run(rt, ["config", "set", "kanban_mode", "sometimes"], capsys)
    assert code == 2
    code, out = run(rt, ["config", "get"], capsys)
    assert "transcribe_model" in out


def test_setup_non_interactive(rt, capsys):
    code, out = run(rt, ["setup", "--non-interactive", "--language", "es", "--model", "small",
                         "--kanban-mode", "off", "--owners", "1,2", "--no-autojoin"], capsys)
    assert code == 0
    assert rt.cfg["transcribe_language"] == "es" and rt.cfg["analysis_language"] == "es"
    assert rt.cfg["ui_language"] == "es" and rt.cfg["transcribe_model"] == "small"
    assert rt.cfg["kanban_mode"] == "off" and rt.cfg["owners"] == ["1", "2"]
    assert rt.cfg["autojoin_enabled"] is False


def test_setup_interactive_uses_defaults_on_enter(rt, capsys, monkeypatch):
    answers = iter(["en", "", "", "", "", "", "", "", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers, ""))
    code, out = run(rt, ["setup"], capsys)
    assert code == 0 and rt.cfg["transcribe_language"] == "en"
    # Enter keeps the current value WITHOUT pinning it, so future default changes still apply.
    assert "transcribe_model" not in rt.cfg and rt.settings().transcribe_model == "medium"


def test_setup_rejects_bad_value(rt, capsys):
    code, out = run(rt, ["setup", "--non-interactive", "--kanban-mode", "sometimes"], capsys)
    assert code == 2 and rt.cfg == {}


def test_doctor_exit_code(rt, capsys, monkeypatch):
    from meeting_scribe.doctor import Check, CheckRegistry
    reg = CheckRegistry()
    reg.register_check("x", lambda env: Check.fail("nope"))
    monkeypatch.setattr(cli, "doctor_registry", lambda: reg)
    code, out = run(rt, ["doctor"], capsys)
    assert code == 1 and "nope" in out
    code, out = run(rt, ["doctor", "--json"], capsys)
    assert json.loads(out)["checks"][0]["status"] == "fail"


def test_no_subcommand_prints_help(rt, capsys):
    code, out = run(rt, [], capsys)
    assert code == 0 and "setup" in out


# -- config list / schema / llm (DESIGN §18) ---------------------------------------------------------
class MemAux:
    def __init__(self):
        self.task = {}

    def user_task_config(self):
        return self.task

    def main_model(self):
        from meeting_scribe.llm_config import Link
        return Link("mainprov", "main-model")

    def write(self, values):
        self.task.update(values)

    def probe(self, link, timeout):
        return (link.provider != "broken", "answered" if link.provider != "broken" else "402 no credit")


@pytest.fixture
def lrt(rt):
    rt.aux = MemAux()
    rt.llm_store = lambda: rt.aux
    return rt


def test_config_list_shows_origin_and_groups(rt, capsys):
    rt.cfg["kanban_mode"] = "off"
    code, out = run(rt, ["config", "list", "--json"], capsys)
    rows = {r["key"]: r for r in json.loads(out)["settings"]}
    assert code == 0 and rows["kanban_mode"]["origin"] == "configured" and rows["linear_mode"]["origin"] == "default"
    assert rows["kanban_mode"]["group"] == "integrations"
    code, out = run(rt, ["config", "list"], capsys)
    assert "[Integrations]" in out and "kanban_mode = off  (configured)" in out


def test_config_list_shows_the_channel_a_name_resolved_to(rt, capsys):
    from meeting_scribe.discord_ui.destination import REPORT_KV

    rt.cfg["google_meet_discord_channel"] = "meet-notes"
    rt.service().repo.kv_set(f"{REPORT_KV}.google_meet", json.dumps({
        "guild": {"id": "100", "name": "Example Team", "source": "only"},
        "steps": [{"key": "google_meet_discord_channel", "value": "meet-notes", "status": "ok",
                   "channel_id": "610", "channel_name": "meet-notes"}], "targets": ["610"]}))
    code, out = run(rt, ["config", "list"], capsys)
    assert "google_meet_discord_channel = meet-notes  (configured)  → #meet-notes (610)" in out
    assert "Example Team (100, only)" in out


def test_config_schema_json(rt, capsys):
    code, out = run(rt, ["config", "schema", "--json", "--lang", "es"], capsys)
    doc = json.loads(out)
    assert code == 0 and doc["version"] == 1 and doc["language"] == "es"
    assert any(f["key"] == "llm_fallback_chain" for f in doc["fields"])


def test_config_set_channel_name_retries_waiting_deliveries(rt, capsys):
    from meeting_scribe.pipeline.runner import WAITING_KV

    svc = rt.service()
    svc.repo.kv_set(WAITING_KV + rt.mid, "waiting for a Discord channel")
    svc.repo.enqueue_job(rt.mid, Stage.DELIVER, now=svc.clock.now() + timedelta(hours=1))
    code, out = run(rt, ["config", "set", "delivery_discord_channel", "#meeting-notes"], capsys)
    assert code == 0 and rt.cfg["delivery_discord_channel"] == "meeting-notes"
    assert "1 delivery" in out and svc.repo.next_job(now=svc.clock.now()) is not None


def test_status_lists_meetings_waiting_for_a_channel(rt, capsys):
    from meeting_scribe.pipeline.runner import WAITING_KV

    rt.service().repo.kv_set(WAITING_KV + rt.mid, "waiting for a Discord channel: set one with `config set`")
    code, out = run(rt, ["status"], capsys)
    assert "waiting for a Discord channel" in out and f"! {rt.mid}:" in out


def test_llm_show_set_fallback_and_test(lrt, capsys):
    code, out = run(lrt, ["llm", "show"], capsys)
    assert code == 0 and "auto → mainprov/main-model" in out and "no fallback chain" in out
    code, out = run(lrt, ["llm", "set", "--provider", "prov-a", "--model", "vendor/model-a"], capsys)
    assert code == 0 and lrt.aux.task == {"provider": "prov-a", "model": "vendor/model-a"}
    code, out = run(lrt, ["llm", "fallback", "add", "broken"], capsys)
    assert code == 2 and "model" in out and "fallback_chain" not in lrt.aux.task
    run(lrt, ["llm", "fallback", "add", "broken:m1"], capsys)
    run(lrt, ["llm", "fallback", "add", "prov-c:m2"], capsys)
    code, out = run(lrt, ["llm", "show", "--json"], capsys)
    data = json.loads(out)
    assert [f["provider"] for f in data["fallback_chain"]] == ["broken", "prov-c"]
    code, out = run(lrt, ["llm", "test"], capsys)
    assert code == 0 and "[  OK] primary: prov-a/vendor/model-a" in out and "[FAIL] fallback 1" in out
    code, out = run(lrt, ["llm", "fallback", "remove", "1"], capsys)
    assert code == 0 and lrt.aux.task["fallback_chain"] == [{"provider": "prov-c", "model": "m2"}]
    code, out = run(lrt, ["llm", "fallback", "add", "bad provider"], capsys)
    assert code == 2
    code, _ = run(lrt, ["llm", "fallback", "clear"], capsys)
    assert code == 0 and lrt.aux.task["fallback_chain"] == []


def test_llm_without_hermes_config_says_so(rt, capsys):
    code, out = run(rt, ["llm", "show"], capsys)
    assert code == 1 and "not available" in out


def test_status_and_doctor_explain_notes_left_in_a_dm(rt, capsys):
    from meeting_scribe.domain.models import KV_DM_NOTES

    hint = f"notes in a direct message; run `hermes meeting-scribe reprocess {rt.mid} --from deliver`"
    rt.service().repo.kv_set(KV_DM_NOTES + rt.mid, hint)
    code, out = run(rt, ["status"], capsys)
    assert f"! {rt.mid}: {hint}" in out
    code, out = run(rt, ["status", "--json"], capsys)
    assert json.loads(out)["dm_notes"] == {rt.mid: hint}


def test_llm_set_to_the_default_says_so(lrt, capsys):
    code, out = run(lrt, ["llm", "set", "--provider", "auto"], capsys)
    assert code == 0 and "default" in out and "Saved" not in out
    code, out = run(lrt, ["llm", "set", "--provider", "prov-a"], capsys)
    assert code == 0 and "Saved" in out
