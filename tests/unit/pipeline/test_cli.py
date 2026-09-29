from dataclasses import replace
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

    def repo(self):
        return self._service.repo

    def spaces(self):
        from meeting_scribe.spaces import Spaces
        return Spaces(self.repo, lambda key, default=None: self.cfg.get(key, default))

    def settings(self, space=None):
        from meeting_scribe.config import settings_from_mapping
        if space:
            return self.spaces().settings(space)
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


def test_setup_asks_where_tasks_go_and_explains_the_choices(rt, capsys, monkeypatch):
    prompts = []

    def answer(prompt=""):
        prompts.append(prompt)
        return "projects" if prompt.startswith("delivery_tasks_placement") else ""
    monkeypatch.setattr("builtins.input", answer)
    code, out = run(rt, ["setup"], capsys)
    assert code == 0 and "delivery_tasks_placement [meeting]: " in prompts
    assert "projects_inline" in out and rt.cfg["delivery_tasks_placement"] == "projects"
    code, _ = run(rt, ["setup", "--non-interactive", "--tasks-placement", "meeting"], capsys)
    assert code == 0 and rt.cfg["delivery_tasks_placement"] == "meeting"


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


def test_config_list_ignores_the_report_of_a_channel_changed_since(rt, capsys):
    """Regression: the last delivery resolved an older value; the new one must not show its channel."""
    from meeting_scribe.discord_ui.destination import REPORT_KV

    repo = rt.service().repo
    repo.kv_set(f"{REPORT_KV}.discord", json.dumps({
        "guild": {"id": "100", "name": "Example Team", "source": "meeting"}, "targets": ["610"],
        "steps": [{"key": "delivery_discord_channel", "value": "", "status": "unset"},
                  {"key": "voice_text", "status": "ok", "channel_id": "610", "channel_name": "general"}]}))
    rt.cfg["delivery_discord_channel"] = "620"
    code, out = run(rt, ["config", "list", "--group", "delivery"], capsys)
    assert "#general" not in out
    assert "delivery_discord_channel = 620  (configured)  ! delivery_discord_channel: changed since the last " \
           "delivery; it will be checked on the next one" in out
    repo.set_bot_guilds([("100", "Example Team")])
    repo.set_guild_channels("100", [{"id": "620", "name": "meeting-notes", "type": "text", "public": True}])
    code, out = run(rt, ["config", "list", "--group", "delivery"], capsys)
    assert "delivery_discord_channel = 620  (configured)  → #meeting-notes (620)" in out
    rt.cfg["delivery_discord_channel"] = "missing-notes"
    code, out = run(rt, ["config", "list", "--json"], capsys)
    row = {r["key"]: r for r in json.loads(out)["settings"]}["delivery_discord_channel"]
    assert row["resolved"]["status"] == "missing" and "channel_id" not in row["resolved"]


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


def test_status_and_doctor_name_people_whose_audio_was_not_captured(rt, capsys):
    from dataclasses import replace
    from types import SimpleNamespace

    from meeting_scribe.doctor import check_missing_audio
    from meeting_scribe.domain.models import Speaker

    repo = rt.service().repo
    m = repo.get_meeting(rt.mid)
    repo.save_meeting(replace(m, speakers=(*m.speakers, Speaker("43", "Luis")), missing_audio=("43",)))
    code, out = run(rt, ["status"], capsys)
    assert "! Could not capture the audio of: Luis" in out
    code, out = run(rt, ["status", "--json"], capsys)
    assert json.loads(out)["recent"][0]["missing_audio"] == ["Luis"]
    res = check_missing_audio(SimpleNamespace(service=rt.service))
    assert res.status == "warn" and f"{rt.mid} (Luis)" in res.detail
    repo.save_meeting(replace(m, missing_audio=()))
    assert check_missing_audio(SimpleNamespace(service=rt.service)).status == "ok"


def test_status_and_doctor_explain_notes_left_in_a_dm(rt, capsys):
    from meeting_scribe.domain.models import KV_DM_NOTES

    hint = f"notes in a direct message; run `hermes meeting-scribe reprocess {rt.mid} --from deliver`"
    rt.service().repo.kv_set(KV_DM_NOTES + rt.mid, hint)
    code, out = run(rt, ["status"], capsys)
    assert f"! {rt.mid}: {hint}" in out
    code, out = run(rt, ["status", "--json"], capsys)
    assert json.loads(out)["dm_notes"] == {rt.mid: hint}


def test_status_names_dm_meetings_that_missed_a_participant(rt, capsys):
    from meeting_scribe import privacy

    rt.service().repo.kv_set(privacy.DM_UNREACHABLE_KV + rt.mid, "10 (direct messages closed)")
    code, out = run(rt, ["status"], capsys)
    assert code == 0 and rt.mid in out and "10 (direct messages closed)" in out
    code, out = run(rt, ["status", "--json"], capsys)
    assert json.loads(out)["dm_unreachable"] == {rt.mid: "10 (direct messages closed)"}


def test_llm_set_to_the_default_says_so(lrt, capsys):
    code, out = run(lrt, ["llm", "set", "--provider", "auto"], capsys)
    assert code == 0 and "default" in out and "Saved" not in out
    code, out = run(lrt, ["llm", "set", "--provider", "prov-a"], capsys)
    assert code == 0 and "Saved" in out


def test_reprocess_of_a_discarded_meeting_is_refused(rt, capsys):
    from dataclasses import replace
    repo = rt.service().repo
    repo.save_meeting(replace(repo.get_meeting(rt.mid), state=MeetingState.EMPTY))
    code, out = run(rt, ["reprocess", rt.mid, "--from", "transcribe"], capsys)
    assert code == 1 and "nothing to reprocess" in out
    assert repo.get_meeting(rt.mid).state is MeetingState.EMPTY


def test_config_set_and_list_meeting_routes(rt, capsys):
    """DESIGN §19.2: strict on write (canonical form, clear errors), each rule shown by ``config list``."""
    from meeting_scribe.discord_ui.destination import ROUTES_REPORT_KV

    code, _ = run(rt, ["config", "set", "meeting_routes", "Leadership = leadership-notes:privada, meet:abc-* = 610"],
                  capsys)
    assert code == 0 and rt.cfg["meeting_routes"] == ["Leadership=#leadership-notes:private", "meet:abc-*=610"]
    code, out = run(rt, ["config", "set", "meeting_routes", "Leadership = #notes:hidden"], capsys)
    assert code != 0 and "unknown option 'hidden'" in out
    rt.service().repo.kv_set(ROUTES_REPORT_KV + rt.service().repo.get_meeting(rt.mid).space, json.dumps([
        {"origin": "Leadership", "kind": "voice", "channel": "leadership-notes", "private": True, "status": "ok",
         "channel_id": "700", "channel_name": "leadership-notes", "target_kind": "text", "public": False}]))
    code, out = run(rt, ["config", "list"], capsys)
    assert "· Leadership (voice channel) → #leadership-notes (700, private channel), private" in out
    assert "· meet:abc-* (Google Meet) → 610, normal  (not checked against Discord yet)" in out
    code, out = run(rt, ["config", "list", "--json"], capsys)
    row = next(r for r in json.loads(out)["settings"] if r["key"] == "meeting_routes")
    assert [r["origin"] for r in row["routes"]] == ["Leadership", "meet:abc-*"]


def test_private_move_is_the_explicit_way_to_move_a_private_meeting(rt, capsys):
    from meeting_scribe import privacy

    code, out = run(rt, ["private-move", rt.mid, "800"], capsys)
    assert code == 1 and "not private" in out
    privacy.remember(rt.service().repo, rt.mid, "Daily Sync", "700")
    privacy.remember(rt.service().repo, rt.mid, "", "800")  # a later delivery never re-anchors it
    assert privacy.record(rt.service().repo, rt.mid)["channel"] == "700"
    assert run(rt, ["private-move", rt.mid, "#general"], capsys)[0] == 2
    code, out = run(rt, ["private-move", rt.mid, "<#800>"], capsys)
    assert code == 0 and "800" in out
    assert privacy.record(rt.service().repo, rt.mid) == {"rule": "Daily Sync", "channel": "800"}
    assert rt.service().repo.get_job(rt.mid).stage is Stage.DELIVER


def _live_owner():
    """The owner id of ANOTHER running process on this host (the test runner's parent): the gateway."""
    import os
    import socket
    return f"{socket.gethostname()}:{os.getppid()}:feedbeef"


def _dead_owner():
    import socket
    import subprocess
    import sys
    proc = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    return f"{socket.gethostname()}:{proc.stdout.strip()}:deadbeef"


def test_status_headline_names_the_gateways_recording_read_from_the_database(rt, capsys, meeting):
    # Regression: the headline was a fixed "Not recording right now" string, so the CLI (a process
    # without capture) denied a recording the gateway was writing — and operators restart on it.
    repo = rt.service().repo
    live = replace(meeting, id="rec1live", state=MeetingState.RECORDING, ended_at=None, title="Planning")
    repo.save_meeting(live)
    repo.set_capture_owner(live.id, _live_owner())
    code, out = run(rt, ["status"], capsys)
    head = out.splitlines()[0]
    assert "Not recording" not in out
    assert head.startswith("Recording now:") and "`rec1live` Planning" in head
    code, out = run(rt, ["status", "--json"], capsys)
    assert json.loads(out)["recording"] == [{"id": "rec1live", "space": live.space, "title": "Planning",
                                             "channel": live.channel_name,
                                             "started_at": live.started_at.isoformat(), "live": True}]


def test_status_says_when_a_recording_row_has_no_live_capture(rt, capsys, meeting):
    repo = rt.service().repo
    orphan = replace(meeting, id="rec2dead", state=MeetingState.RECORDING, ended_at=None)
    repo.save_meeting(orphan)
    repo.set_capture_owner(orphan.id, _dead_owner())
    code, out = run(rt, ["status"], capsys)
    lines = out.splitlines()
    assert lines[0].startswith("Not recording right now")
    assert "`rec2dead` is marked as recording, but the process capturing it is gone" in lines[1]
