import json

from meeting_scribe.tools import SCHEMAS, MeetingTools

from .test_commands import make, processed


def test_schemas_are_function_shaped():
    names = {s["name"] for s in SCHEMAS.values()}
    assert names == {"meeting_search", "meeting_get"}
    for s in SCHEMAS.values():
        assert s["parameters"]["type"] == "object" and s["description"]


def test_search_and_get(prepo, layout, settings, clock, meeting):
    _, service, runner = make(prepo, layout, settings, clock)
    mid = processed(service, runner, meeting)
    tools = MeetingTools(lambda: service)
    res = json.loads(tools.search({"query": "informe", "limit": 3}))
    assert res["results"][0]["meeting_id"] == mid and res["results"][0]["ts"] == "00:02"
    notes = json.loads(tools.get({"meeting_id": mid[:5]}))
    assert notes["meeting"]["id"] == mid and notes["notes"]["meeting_title"] == "Informe semanal"
    tr = json.loads(tools.get({"meeting_id": mid, "part": "transcript"}))
    assert tr["transcript"][1]["text"] == "Yo envío el informe"
    tasks = json.loads(tools.get({"meeting_id": mid, "part": "tasks"}))
    assert tasks["tasks"][0]["id"] == "a1" and tasks["tasks"][0]["status"] == "pending"


def test_errors_are_json(prepo, layout, settings, clock):
    _, service, _ = make(prepo, layout, settings, clock)
    tools = MeetingTools(lambda: service)
    assert "error" in json.loads(tools.search({}))
    assert "error" in json.loads(tools.get({"meeting_id": "zzz"}))
    assert "error" in json.loads(tools.get({"meeting_id": "zzz", "part": "bogus"}))


def test_transcript_is_truncated(prepo, layout, settings, clock, meeting):
    _, service, runner = make(prepo, layout, settings, clock)
    mid = processed(service, runner, meeting)
    tools = MeetingTools(lambda: service, max_utterances=1)
    tr = json.loads(tools.get({"meeting_id": mid, "part": "transcript"}))
    assert len(tr["transcript"]) == 1 and tr["truncated"] is True
