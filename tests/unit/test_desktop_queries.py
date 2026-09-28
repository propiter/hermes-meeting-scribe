"""Desktop read side (library, detail, transcript, status, Google) over an isolated SQLite."""
import json
import time
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from meeting_scribe.desktop.control import HEARTBEAT_KV, Commands
from meeting_scribe.desktop.queries import Library, decode_cursor, encode_cursor, google_status
from meeting_scribe.domain.models import MeetingState, Stage
from meeting_scribe.storage.artifacts import write_notes
from meeting_scribe.storage.layout import Layout
from meeting_scribe.storage.repo import Repository


@pytest.fixture
def repo(tmp_path):
    from meeting_scribe.config import Settings
    from meeting_scribe.spaces import bootstrap

    r = Repository(tmp_path / "index.sqlite")
    bootstrap(r, Settings.defaults(), tmp_path)  # the ``main`` space (DESIGN §23)
    yield r
    r.close()


def seed(repo, meeting, utterances, n=3):
    ids = []
    for i in range(n):
        m = replace(meeting, id=f"m{i}", started_at=datetime(2026, 9, 20 + i, 10, tzinfo=timezone.utc),
                    title=f"Meeting {i}", state=MeetingState.DONE)
        repo.save_meeting(m)
        repo.replace_utterances(m.id, utterances)
        ids.append(m.id)
    return ids


def test_cursor_roundtrip_and_rejects_garbage():
    assert decode_cursor(encode_cursor(["a", "b"]), (str, str)) == ["a", "b"]
    assert decode_cursor("", (str, str)) is None
    for bad in ("%%%", encode_cursor(["a"]), encode_cursor([1, 2]), encode_cursor({"a": 1}), "e30"):
        with pytest.raises(ValueError):
            decode_cursor(bad, (str, str))
    with pytest.raises(ValueError):
        decode_cursor(encode_cursor([True, 1]), (float, int))


def test_library_pagination_search_and_filters(repo, tmp_path, meeting, utterances):
    seed(repo, meeting, utterances)
    repo.save_meeting(replace(meeting, id="g0", source="google_meet", external_id="conferenceRecords/x",
                              started_by="42", started_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
                              state=MeetingState.FAILED, title="Imported"))
    lib = Library(repo, tmp_path)
    page = lib.meetings(limit=2)
    assert [m["id"] for m in page["items"]] == ["m2", "m1"]
    assert all(k not in page["items"][0] for k in ("folder", "started_by", "external_id"))
    rest = lib.meetings(limit=2, cursor=page["next_cursor"])
    assert [m["id"] for m in rest["items"]] == ["m0", "g0"] and rest["next_cursor"] is None
    assert [m["id"] for m in lib.meetings(q="credenciales viernes")["items"]] == ["m2", "m1", "m0"]
    assert [m["id"] for m in lib.meetings(q="Imported")["items"]] == ["g0"]
    assert lib.meetings(q='" OR 1=1 --')["items"] == []  # user text never becomes FTS/SQL syntax
    assert [m["id"] for m in lib.meetings(source="google_meet")["items"]] == ["g0"]
    assert [m["id"] for m in lib.meetings(state="failed")["items"]] == ["g0"]
    assert [m["id"] for m in lib.meetings(state="done", since="2026-09-21", until="2026-09-21")["items"]] == ["m1"]
    assert lib.meetings(state="processing")["items"] == []
    for bad in ({"source": "zoom"}, {"state": "nope"}, {"since": "yesterday"}, {"limit": 0},
                {"since": "2026-09-22", "until": "2026-09-21"}):
        with pytest.raises(ValueError):
            lib.meetings(**bad)
    facets = lib.facets()
    assert facets["total"] == 4 and facets["states"]["done"] == 3 and facets["sources"]["google_meet"] == 1


def test_detail_notes_tasks_sinks_and_redacted_job(repo, tmp_path, meeting, notes):
    layout = Layout(lambda: tmp_path)
    folder = layout.meeting_folder(meeting)
    folder.mkdir(parents=True)
    m = replace(meeting, folder=layout.relative(folder), state=MeetingState.DONE, started_by="42")
    repo.save_meeting(m)
    write_notes(folder, m, notes, "es")
    repo.sync_action_items(m.id, notes.action_items)
    first = notes.action_items[0].id
    repo.set_item_sink_status(m.id, first, "linear", "delivered")
    repo.upsert_delivery(m.id, "linear", f"mtg:{m.id}:{first}", external_id="ENG-1",
                         url="https://linear.app/x/issue/ENG-1?token=abc")
    repo.upsert_delivery(m.id, "kanban", f"mtg:{m.id}:{first}", external_id="t1", url="javascript:alert(1)")
    repo.upsert_delivery(m.id, "discord", f"mtg:{m.id}:task:{first}",
                         external_id=json.dumps({"channel": 55, "target": "77", "messages": [1]}),
                         url="https://discord.com/channels/1/77/1")
    repo.enqueue_job(m.id, Stage.DELIVER, now=datetime.now(timezone.utc))
    repo._x("UPDATE jobs SET error=? WHERE meeting_id=?", ("HTTP 401 Bearer sk-abcdef123456", m.id))
    repo.kv_set("pipeline.waiting_destination." + m.id, "set a channel")
    d = Library(repo, tmp_path).detail(m.id)
    assert d["notes"]["decisions"] == list(notes.decisions)
    assert d["notes"]["open_questions"] == list(notes.open_questions)
    task = d["tasks"][0]
    assert task["sinks"]["linear"] == {"status": "delivered", "url": "https://linear.app/x/issue/ENG-1?…"}
    assert task["sinks"]["kanban"]["url"] == ""  # never a javascript: link
    assert task["discord"] == {"channel_id": "77", "url": "https://discord.com/channels/1/77/1"}
    assert d["job"]["state"] == "queued" and "sk-abcdef123456" not in d["job"]["error"]
    assert d["waiting_destination"] == "set a channel"
    assert "started_by" not in d["meeting"] and "folder" not in d["meeting"]
    assert d["audio"] == {"available": False, "reason": "not_retained"}


def test_detail_survives_missing_or_damaged_notes(repo, tmp_path, meeting):
    repo.save_meeting(meeting)
    lib = Library(repo, tmp_path)
    assert lib.detail(meeting.id)["notes"] is None
    folder = Layout(lambda: tmp_path).meeting_folder(meeting)
    folder.mkdir(parents=True)
    (folder / "notes.json").write_text("{not json")
    assert lib.detail(meeting.id)["notes"] is None
    with pytest.raises(KeyError):
        lib.detail("missing")


def test_artifacts_refuse_symlinks_poisoned_folders_and_unknown_names(repo, tmp_path, meeting):
    layout = Layout(lambda: tmp_path)
    folder = layout.meeting_folder(meeting)
    folder.mkdir(parents=True)
    repo.save_meeting(replace(meeting, folder=layout.relative(folder)))
    lib = Library(repo, tmp_path)
    (tmp_path / "secret.json").write_text('{"refresh_token": "x"}')
    (folder / "notes.json").symlink_to(tmp_path / "secret.json")
    with pytest.raises(ValueError):
        lib.detail(meeting.id)
    with pytest.raises(ValueError):
        lib.artifact(meeting.id, "../../secret.json")
    repo.save_meeting(replace(meeting, folder="../../etc"))
    with pytest.raises(ValueError):
        lib.artifact(meeting.id, "recording.ogg")
    repo.save_meeting(replace(meeting, folder="google"))  # inside the data dir but not a meeting folder
    with pytest.raises(ValueError):
        lib.artifact(meeting.id, "notes.json")


def test_audio_availability_reasons(repo, tmp_path, meeting):
    layout = Layout(lambda: tmp_path)
    folder = layout.meeting_folder(meeting)
    folder.mkdir(parents=True)
    repo.save_meeting(replace(meeting, folder=layout.relative(folder)))
    lib = Library(repo, tmp_path)
    repo.save_meeting(replace(meeting, folder=layout.relative(folder), state=MeetingState.DONE))
    assert lib.audio(meeting.id) == {"available": False, "reason": "not_retained"}
    (folder / "recording.mka").write_bytes(b"x")
    # An archive made before listening copies existed: the page offers to prepare one.
    assert lib.audio(meeting.id) == {"available": False, "reason": "multitrack", "can_prepare": True, "original": True}
    (folder / "playback.ogg").write_bytes(b"OggS")
    got = lib.audio(meeting.id)
    assert got["available"] and got["reason"] == "ready" and got["bytes"] == 4 and got["original"]
    assert got["path"] == str(folder / "playback.ogg")
    (folder / "playback.ogg").unlink()
    (folder / "recording.mka").unlink()
    (folder / "recording.ogg").write_bytes(b"OggSx")  # mixed retention is already playable
    got = lib.audio(meeting.id)
    assert got["available"] and got["bytes"] == 5 and not got["original"]
    repo.save_meeting(replace(meeting, id="g1", source="google_meet"))
    assert lib.audio("g1") == {"available": False, "reason": "imported"}
    repo.save_meeting(replace(meeting, id="r1", state=MeetingState.RECORDING))
    assert lib.audio("r1") == {"available": False, "reason": "recording"}
    repo.save_meeting(replace(meeting, id="e1", state=MeetingState.EMPTY))
    assert lib.audio("e1") == {"available": False, "reason": "empty"}


def test_transcript_is_complete_through_pages(repo, tmp_path, meeting, utterances):
    repo.save_meeting(meeting)
    repo.replace_utterances(meeting.id, utterances)
    lib = Library(repo, tmp_path)
    seen, cursor = [], ""
    while True:
        page = lib.transcript(meeting.id, limit=2, cursor=cursor)
        seen += page["items"]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert [u["text"] for u in seen] == [u.text for u in utterances]
    assert page["total"] == 3
    with pytest.raises(ValueError):
        lib.transcript(meeting.id, cursor=encode_cursor(["a", "b"]))
    with pytest.raises(KeyError):
        lib.transcript("missing")


def test_status_worker_jobs_waiting_and_commands(repo, tmp_path, meeting):
    repo.save_meeting(replace(meeting, state=MeetingState.DONE))
    lib = Library(repo, tmp_path)
    assert lib.status()["worker"]["state"] == "unknown"
    repo.kv_set(HEARTBEAT_KV, str(time.time()))
    assert lib.status()["worker"]["state"] == "recent"
    repo.kv_set(HEARTBEAT_KV, "1")
    assert lib.status()["worker"]["state"] == "stale"
    repo.kv_set(HEARTBEAT_KV, "garbage")
    assert lib.status()["worker"]["state"] == "unknown"
    repo.enqueue_job(meeting.id, Stage.DELIVER, now=datetime.now(timezone.utc))
    repo.kv_set("pipeline.waiting_destination." + meeting.id, "no channel Bearer sk-zzzzzzzzzz")
    Commands(repo).submit("r1", meeting.id, {"action": "reprocess", "stage": "deliver"})
    st = lib.status()
    assert st["counts"] == {"running": 0, "queued": 1, "failed": 0}
    assert st["jobs"][0]["title"] == meeting.title
    assert st["waiting_destination"][0]["meeting_id"] == meeting.id
    assert "sk-zzzzzzzzzz" not in json.dumps(st)
    assert st["commands"][0]["id"] == "r1"
    # what was asked travels with the row (the page names the action), the raw body does not
    assert {k: st["commands"][0][k] for k in ("action", "stage", "title")} == {
        "action": "reprocess", "stage": "deliver", "title": meeting.title}
    assert "body" not in st["commands"][0]
    assert lib.detail(meeting.id)["command"]["state"] == "queued"


def test_google_status_never_exposes_tokens(repo, tmp_path):
    gdir = tmp_path / "google" / "main"  # the only space's connection (DESIGN §23)
    gdir.mkdir(parents=True)
    st = google_status(tmp_path, repo, enabled=False, space="main")
    assert st["connected"] is False and st["client_stored"] is False
    assert "google connect" in st["commands"]["connect"]
    (gdir / "client.json").write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "CSECRET"}}))
    (gdir / "token.json").write_text(json.dumps({"refresh_token": "RTOKEN", "access_token": "ATOKEN",
                                                 "connected_at": 1700000000.0}))
    repo.kv_set("google.main.last_poll_at", "2026-09-26T10:00:00+00:00")
    repo.kv_set("google.main.last_error", "HTTP 401 Bearer ya29.secretsecret")
    st = google_status(tmp_path, repo, enabled=True, space="main")
    assert st["connected"] and st["enabled"] and st["connected_at"] == 1700000000.0
    assert st["last_poll_at"].startswith("2026-09-26")
    blob = json.dumps(st)
    for secret in ("RTOKEN", "ATOKEN", "CSECRET", "cid", "ya29.secretsecret"):
        assert secret not in blob


def test_discarded_recordings_have_their_own_group_not_failed(repo, tmp_path, meeting):
    from meeting_scribe.desktop.queries import STATE_GROUPS

    assert sorted(s for g in STATE_GROUPS.values() for s in g) == sorted(s.value for s in MeetingState)
    repo.save_meeting(replace(meeting, id="e0", state=MeetingState.EMPTY))
    repo.save_meeting(replace(meeting, id="f0", state=MeetingState.FAILED))
    lib = Library(repo, tmp_path)
    facets = lib.facets()
    assert facets["states"]["empty"] == 1 and facets["states"]["failed"] == 1
    assert [m["id"] for m in lib.meetings(state="empty")["items"]] == ["e0"]
    assert [m["id"] for m in lib.meetings(state="failed")["items"]] == ["f0"]


def test_channel_and_project_filters_row_summary_and_facets(repo, tmp_path, meeting, utterances, notes):
    seed(repo, meeting, utterances)
    repo.save_meeting(replace(meeting, id="c9", channel_id="300", channel_name="Soporte", project="Proyecto Alfa",
                              state=MeetingState.DONE, started_at=datetime(2026, 9, 25, tzinfo=timezone.utc)))
    repo.sync_action_items("m1", notes.action_items)
    repo._x("UPDATE action_items SET data=json_set(data, '$.project', 'Proyecto Beta') WHERE meeting_id='m1'")
    lib = Library(repo, tmp_path)
    assert [m["id"] for m in lib.meetings(channel="300")["items"]] == ["c9"]
    assert [m["id"] for m in lib.meetings(project="proyecto alfa")["items"]] == ["c9"]
    assert [m["id"] for m in lib.meetings(project="Proyecto Beta")["items"]] == ["m1"]  # through its tasks
    assert [m["id"] for m in lib.meetings(q="alfa")["items"]] == ["c9"]  # the project name is searchable
    row = next(m for m in lib.meetings()["items"] if m["id"] == "m1")
    assert row["people"] == 2 and row["task_count"] == len(notes.action_items)
    assert row["job_stage"] is None and row["waiting_destination"] is False
    facets = lib.facets()
    assert {c["id"]: c["count"] for c in facets["channels"]} == {"200": 3, "300": 1}
    assert next(c for c in facets["channels"] if c["id"] == "300")["name"] == "Soporte"
    assert facets["projects"] == [{"name": "Proyecto Alfa", "count": 1}, {"name": "Proyecto Beta", "count": 1}]


def test_problem_kind_speaks_the_operator_language():
    from meeting_scribe.desktop.queries import problem_kind
    assert problem_kind("FfmpegNotFound: ffmpeg/ffprobe not found") == "ffmpeg"
    assert problem_kind("RuntimeError: CUDA out of memory") == "model"
    assert problem_kind("LLMError: the model returned invalid JSON") == "llm"
    assert problem_kind("Forbidden: Missing Access to channel") == "destination"
    assert problem_kind("KeyError: 3") == "unknown"
    assert problem_kind("") == "unknown"


def test_history_is_a_readable_timeline(repo, tmp_path, meeting):
    repo.save_meeting(replace(meeting, state=MeetingState.FAILED))
    repo.enqueue_job(meeting.id, Stage.ANALYZE, now=datetime.now(timezone.utc))
    repo._x("UPDATE jobs SET state='failed', error=?, failed_stage='analyze' WHERE meeting_id=?",
            ("LLMError: timed out", meeting.id))
    Commands(repo).submit("r" * 16, meeting.id, {"action": "prepare_audio"})
    d = Library(repo, tmp_path).detail(meeting.id)
    kinds = [e["kind"] for e in d["history"]]
    assert kinds[:2] == ["started", "ended"] and "failed" in kinds and kinds.count("command") == 1
    failed = next(e for e in d["history"] if e["kind"] == "failed")
    assert failed["problem"] == "llm" and failed["stage"] == "analyze"
    assert d["job"]["problem"] == "llm"
    cmd = next(e for e in d["history"] if e["kind"] == "command")
    assert cmd["action"] == "prepare_audio" and cmd["state"] == "queued"
    assert d["meeting"]["people"] == 2 and d["projects"] == []


def test_google_status_is_the_asked_spaces_own(repo, tmp_path):
    """Each space has its own Google connection; the commands name the space once there are several."""
    from meeting_scribe.desktop.queries import google_summary

    repo.insert_space("team", "Team")
    (tmp_path / "google" / "team").mkdir(parents=True)
    (tmp_path / "google" / "team" / "client.json").write_text("{}")
    (tmp_path / "google" / "team" / "token.json").write_text(json.dumps({"refresh_token": "RTOKEN"}))
    repo.kv_set("google.team.last_poll_at", "2026-09-26T10:00:00+00:00")
    repo.kv_set("google.team.last_poll_ok", "0")
    repo.kv_set("google.team.last_error", "temporary: HTTP 503")
    team = google_status(tmp_path, repo, enabled=True, space="team")
    main = google_status(tmp_path, repo, enabled=True, space="main")
    assert team["connected"] and not main["connected"] and "last_poll_at" not in main
    assert "--space team" in team["commands"]["connect"]
    assert google_summary(team) == {"enabled": True, "connected": True, "last_check": "2026-09-26T10:00:00+00:00",
                                    "last_import": None, "error": "temporary: HTTP 503"}
    assert google_summary(main)["error"] is None and "RTOKEN" not in json.dumps(team)


def test_library_of_one_space_never_shows_another_space(repo, tmp_path, meeting, utterances):
    repo.insert_space("team", "Team")
    repo.save_meeting(replace(meeting, id="mine1", state=MeetingState.DONE))
    repo.save_meeting(replace(meeting, id="other1", space="team", state=MeetingState.DONE))
    repo.replace_utterances("other1", utterances)
    lib = Library(repo, tmp_path, "main")
    assert [m["id"] for m in lib.meetings()["items"]] == ["mine1"]
    assert lib.meetings(q="credenciales")["items"] == []
    assert lib.facets()["total"] == 1
    with pytest.raises(KeyError):
        lib.detail("other1")
