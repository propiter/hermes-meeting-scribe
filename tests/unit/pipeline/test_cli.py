import argparse
import json

import pytest

from meeting_scribe import cli
from meeting_scribe.domain.models import MeetingState

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
    code, out = run(rt, ["config", "get", "kanban.mode"], capsys)
    assert code == 0 and out.strip() == "approve"
    code, _ = run(rt, ["config", "set", "kanban.mode", "auto"], capsys)
    assert code == 0 and rt.cfg["kanban.mode"] == "auto"
    code, out = run(rt, ["config", "set", "kanban.mode", "sometimes"], capsys)
    assert code == 2
    code, out = run(rt, ["config", "get"], capsys)
    assert "transcribe.model" in out


def test_setup_non_interactive(rt, capsys):
    code, out = run(rt, ["setup", "--non-interactive", "--language", "es", "--model", "small",
                         "--kanban-mode", "off", "--owners", "1,2", "--no-autojoin"], capsys)
    assert code == 0
    assert rt.cfg["transcribe.language"] == "es" and rt.cfg["analysis.language"] == "es"
    assert rt.cfg["ui.language"] == "es" and rt.cfg["transcribe.model"] == "small"
    assert rt.cfg["kanban.mode"] == "off" and rt.cfg["owners"] == ["1", "2"]
    assert rt.cfg["autojoin.enabled"] is False


def test_setup_interactive_uses_defaults_on_enter(rt, capsys, monkeypatch):
    answers = iter(["en", "", "", "", "", "", "", "", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers, ""))
    code, out = run(rt, ["setup"], capsys)
    assert code == 0 and rt.cfg["transcribe.language"] == "en"
    # Enter keeps the current value WITHOUT pinning it, so future default changes still apply.
    assert "transcribe.model" not in rt.cfg and rt.settings().transcribe_model == "medium"


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
