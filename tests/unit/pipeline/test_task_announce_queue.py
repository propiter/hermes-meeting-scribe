"""The queue of what Discord still has to show for a meeting's tasks (``kv tasks.announce.<meeting>``,
DESIGN §16.2). An assignment, a refresh after a task went to Linear and the sink taking what it showed
all rewrite the SAME row from different threads: each read-modify-write is one transaction, and a
record queued again while the sink was showing the previous one is not dropped."""
from __future__ import annotations

import threading
import time

from meeting_scribe.pipeline import task_assign
from meeting_scribe.pipeline.task_assign import Actor

from .test_task_tools import ANA, world  # noqa: F401  (fixture)


def test_a_refresh_racing_an_assignment_of_another_task_does_not_drop_it(world, monkeypatch):
    """The refresh reads the queue, and the assignment of ANOTHER task commits while the refresh is
    between its read and its write: without one transaction the refresh wrote back its stale read and
    the assignment's announcement was lost (no card edit, no DM panel, no ping)."""
    service, mid, _, _, _ = world
    repo = service.repo
    real_get = repo.kv_get
    reading = threading.Event()

    def slow_get(key):
        value = real_get(key)
        if threading.current_thread().name == "refresh" and key.startswith(task_assign.ANNOUNCE_KV):
            reading.set()
            time.sleep(0.3)  # the assignment runs now, if it can
        return value

    monkeypatch.setattr(repo, "kv_get", slow_get)
    refresh = threading.Thread(target=task_assign.queue_refresh, args=(repo, mid, "report"), name="refresh")
    refresh.start()
    assert reading.wait(5)
    service.assign_task(mid, "fix-mail", "me", Actor(ANA))
    refresh.join(5)
    assert set(task_assign.pending_announcements(repo, mid)) == {"fix-mail", "report"}


def test_a_record_queued_again_while_the_sink_shows_it_stays_queued(world):
    """The sink read the queue and is editing the card; meanwhile the task is sent to Linear and the same
    refresh is queued again. Taking what was shown must keep the newer request (the card must show Linear)."""
    service, mid, _, _, _ = world
    repo = service.repo
    task_assign.queue_refresh(repo, mid, "report")
    shown = task_assign.pending_announcements(repo, mid)  # what the sink is showing
    task_assign.queue_refresh(repo, mid, "report")  # queued again meanwhile
    task_assign.take_announcements(repo, mid, shown)
    assert "report" in task_assign.pending_announcements(repo, mid)
    task_assign.take_announcements(repo, mid, task_assign.pending_announcements(repo, mid))
    assert task_assign.pending_announcements(repo, mid) == {}
