"""Background work on a multi-profile Hermes host (real ``agent.secret_scope``).

The pipeline worker and the Meet pollers run outside any turn. Once the process hosts several
profiles, an unscoped ``get_secret`` raises ``UnscopedSecretError``; every job/poll must bind the
OWNER profile's secrets (not the launch profile's) for its own duration.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("agent.secret_scope", reason="Hermes is not importable (set PYTHONPATH)")


@pytest.fixture(autouse=True)
def _single_profile_after():
    yield
    from agent.secret_scope import set_multiplex_active
    from tui_gateway import launch_profile_policy

    set_multiplex_active(False)
    launch_profile_policy._snapshot = None


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Launch home (root, default profile) and a different owner profile, each with its own .env."""
    root = tmp_path / "root"
    owner = root / "profiles" / "owner"
    owner.mkdir(parents=True)
    (root / ".env").write_text("DISCORD_ALLOWED_USERS=999\n", encoding="utf-8")
    (owner / ".env").write_text("DISCORD_ALLOWED_USERS=111\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("DISCORD_ALLOWED_USERS", raising=False)
    return root, owner


def _owners():
    from meeting_scribe import hermes_adapters
    from meeting_scribe.config import Settings, effective_owners

    return effective_owners(Settings.defaults(), hermes_adapters.secret)


def _in_worker(body):
    from meeting_scribe import hermes_adapters

    out: dict = {}

    def target():
        try:
            out["v"] = body()
        except Exception as exc:  # noqa: BLE001 - reported to the assertion
            out["e"] = f"{type(exc).__name__}: {exc}"

    thread = hermes_adapters.context_spawner(target, name="ms-scope-test")
    thread.start()
    thread.join(10)
    return out


def test_unscoped_background_read_fails_closed_on_a_multiplexed_host(homes):
    """The failure the job scope exists for (guards against a host that silently stops failing)."""
    from agent.secret_scope import set_multiplex_active

    set_multiplex_active(True)
    assert "UnscopedSecretError" in _in_worker(_owners).get("e", "")


def test_job_scope_reads_the_owner_profile_not_the_launch_profile(homes):
    from agent.secret_scope import current_secret_scope, set_multiplex_active
    from meeting_scribe.job_scope import owner_job_scope

    _, owner = homes
    set_multiplex_active(True)

    def body():
        with owner_job_scope(lambda: owner):
            return _owners()

    assert _in_worker(body).get("v") == ("111",)
    assert current_secret_scope() is None  # reset afterwards


def test_job_scope_is_a_no_op_on_a_single_profile_host(homes, monkeypatch):
    from agent.secret_scope import current_secret_scope
    from meeting_scribe.job_scope import owner_job_scope

    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "333")
    with owner_job_scope(lambda: pytest.fail("the owner is not resolved when nothing is bound")):
        assert current_secret_scope() is None
        assert _owners() == ("333",)


def test_job_scope_keeps_a_scope_that_is_already_bound(homes):
    from agent.secret_scope import (build_profile_secret_scope, reset_secret_scope, set_multiplex_active,
                                    set_secret_scope)
    from meeting_scribe.job_scope import owner_job_scope

    root, _ = homes
    set_multiplex_active(True)
    token = set_secret_scope(build_profile_secret_scope(root), profile_home=str(root))
    try:
        with owner_job_scope(lambda: pytest.fail("a bound scope (a turn, a request) is not replaced")):
            assert _owners() == ("999",)
    finally:
        reset_secret_scope(token)


def test_a_worker_started_before_multiplexing_binds_the_owner_scope_per_job(homes):
    """Production shape: the worker starts at gateway boot (single profile). A hosted room or a
    Desktop ``?profile=`` request later turns the process into a multi-profile host; the next job of
    the already-running worker must still read the owner's secrets."""
    from agent.secret_scope import set_multiplex_active
    from meeting_scribe import hermes_adapters
    from meeting_scribe.job_scope import owner_job_scope
    from meeting_scribe.pipeline.runner import PipelineRunner

    _, owner = homes
    seen: list = []

    class Runner(PipelineRunner):
        def _run_one(self):  # the job body: what DELIVER does first (who may act on the buttons)
            seen.append(_owners())
            return False

    runner = Runner.__new__(Runner)
    runner.job_scope = lambda: owner_job_scope(lambda: Path(owner))
    go, done = threading.Event(), threading.Event()

    def worker():
        go.wait(5)
        try:
            runner.run_once()
        except Exception as exc:  # noqa: BLE001 - reported to the assertion
            seen.append(type(exc).__name__)
        done.set()

    hermes_adapters.context_spawner(worker, name="ms-worker").start()
    set_multiplex_active(True)
    go.set()
    assert done.wait(10)
    assert seen == [("111",)]


def test_meet_poll_binds_the_owner_scope(homes):
    from agent.secret_scope import set_multiplex_active
    from meeting_scribe.google.importer import MeetPoller
    from meeting_scribe.job_scope import owner_job_scope

    _, owner = homes
    set_multiplex_active(True)
    seen: list = []

    class Settings:
        google_meet_enabled = False

    def settings():
        seen.append(_owners())  # the poll reads settings (and through them, secrets) first
        return Settings()

    poller = MeetPoller(space="main", importer=lambda: None, repo=lambda: None, settings=settings,
                        connected_at=lambda: None, owner="me", job_scope=lambda: owner_job_scope(lambda: owner))
    assert _in_worker(poller.tick) == {"v": None}
    assert seen == [("111",)]
