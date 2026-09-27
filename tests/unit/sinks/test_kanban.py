from meeting_scribe.domain.models import ActionStatus, Candidate
from meeting_scribe.sinks.kanban import KanbanSink


class FakeKanban:
    def __init__(self):
        self.created = []

    def create_task(self, *, title, body, idempotency_key, project_id, board):
        self.created.append(dict(title=title, body=body, key=idempotency_key, project_id=project_id, board=board))
        return f"t_{len(self.created)}"

    def list_boards(self):
        return [{"slug": "default", "name": "Default", "archived": False}]


def _sink(repo, settings, gw, owners=("11",), resolve=lambda m, n, i: None):
    return KanbanSink(settings, repo, gw, owners=lambda: owners, project_for=resolve)


def test_off_mode_disabled(repo, settings_of):
    assert _sink(repo, settings_of(kanban__mode="off"), FakeKanban()).enabled() is False


def test_approve_mode_delivers_only_approved_owner_items(tmp_path, repo, meeting, notes, settings_of):
    gw = FakeKanban()
    repo.sync_action_items(meeting.id, notes.action_items)
    sink = _sink(repo, settings_of(kanban__mode="approve"), gw)
    res = sink.deliver(meeting, notes, tmp_path)
    assert gw.created == [] and res.ok and res.skipped
    repo.set_action_status(meeting.id, "a0000000001", ActionStatus.APPROVED)  # global status alone: not enough
    sink.deliver(meeting, notes, tmp_path)
    assert gw.created == []
    repo.set_item_sink_status(meeting.id, "a0000000001", "kanban", "approved")
    sink.deliver(meeting, notes, tmp_path)
    assert [c["title"] for c in gw.created] == ["Enviar credenciales"]
    assert repo.get_action_item(meeting.id, "a0000000001").status is ActionStatus.DELIVERED


def test_auto_mode_is_idempotent_and_owner_only(tmp_path, repo, meeting, notes, settings_of):
    gw = FakeKanban()
    repo.sync_action_items(meeting.id, notes.action_items)
    sink = _sink(repo, settings_of(kanban__mode="auto"), gw)
    sink.deliver(meeting, notes, tmp_path)
    sink.deliver(meeting, notes, tmp_path)
    assert len(gw.created) == 1  # a0000000002 has no owner; second run skipped via deliveries
    c = gw.created[0]
    assert c["key"] == "mtg:k3v7q2ab:a0000000001" and c["board"] is None
    assert "Yo envío las credenciales" in c["body"] and str(tmp_path) in c["body"] and "00:03" in c["body"]
    assert repo.get_delivery("kanban", "mtg:k3v7q2ab:a0000000001")["external_id"] == "t_1"


def test_dismissed_never_delivered(tmp_path, repo, meeting, notes, settings_of):
    gw = FakeKanban()
    repo.sync_action_items(meeting.id, notes.action_items)
    repo.set_action_status(meeting.id, "a0000000001", ActionStatus.DISMISSED)
    _sink(repo, settings_of(kanban__mode="auto"), gw).deliver(meeting, notes, tmp_path)
    assert gw.created == []


def test_project_mapping_to_kanban(tmp_path, repo, meeting, notes, settings_of):
    gw = FakeKanban()
    repo.sync_action_items(meeting.id, notes.action_items)
    hermes = Candidate("hermes:p1", "Website", "hermes", {"project_id": "p1", "board_slug": "web"})
    sink = _sink(repo, settings_of(kanban__mode="auto", kanban__board="ops"), gw, resolve=lambda m, n, i: hermes)
    sink.deliver(meeting, notes, tmp_path)
    assert gw.created[0]["project_id"] == "p1" and gw.created[0]["board"] == "web"


def test_kanban_board_candidate_selects_board(tmp_path, repo, meeting, notes, settings_of):
    gw = FakeKanban()
    board = Candidate("kanban:ops", "Ops", "kanban", {"board": "ops"})
    sink = _sink(repo, settings_of(kanban__mode="auto"), gw, resolve=lambda m, n, i: board)
    ext = sink.deliver_item(meeting, notes, notes.action_items[0], tmp_path)
    assert ext == "t_1" and gw.created[0]["board"] == "ops" and gw.created[0]["project_id"] is None
    assert sink.deliver_item(meeting, notes, notes.action_items[0], tmp_path) == "t_1"  # idempotent
    assert len(gw.created) == 1


def test_configured_board_used_without_project(tmp_path, repo, meeting, notes, settings_of):
    gw = FakeKanban()
    _sink(repo, settings_of(kanban__mode="auto", kanban__board="ops"), gw).deliver_item(
        meeting, notes, notes.action_items[0], tmp_path)
    assert gw.created[0]["board"] == "ops"


def test_errors_are_reported_not_raised(tmp_path, repo, meeting, notes, settings_of):
    class Broken(FakeKanban):
        def create_task(self, **kw):
            raise PermissionError("fenced")
    repo.sync_action_items(meeting.id, notes.action_items)
    res = _sink(repo, settings_of(kanban__mode="auto"), Broken()).deliver(meeting, notes, tmp_path)
    assert not res.ok and "fenced" in res.errors[0]
