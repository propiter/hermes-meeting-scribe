from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone

import subprocess
from pathlib import Path

import pytest

from meeting_scribe.audio.ffmpeg import FfmpegNotFound, resolve_ffmpeg
from meeting_scribe.domain.models import (
    ActionItem, Meeting, MeetingState, Notes, Speaker, Topic, Utterance,
)


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run ``async def`` tests on a fresh event loop (no pytest-asyncio dependency)."""
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    names = pyfuncitem._fixtureinfo.argnames
    kwargs = {n: pyfuncitem.funcargs[n] for n in names}
    asyncio.run(asyncio.wait_for(pyfuncitem.obj(**kwargs), timeout=20))
    return True


@pytest.fixture
def meeting() -> Meeting:
    return Meeting(id="k3v7q2ab", guild_id="100", channel_id="200", channel_name="Daily Sync",
                   started_at=datetime(2026, 9, 26, 15, 4, tzinfo=timezone.utc),
                   ended_at=datetime(2026, 9, 26, 15, 34, tzinfo=timezone.utc),
                   state=MeetingState.CAPTURED, title="Daily Sync", guild_name="Acme",
                   category_name="Engineering",
                   speakers=(Speaker("10", "Ana"), Speaker("11", "Luis")))


@pytest.fixture
def utterances() -> list[Utterance]:
    return [
        Utterance(0.0, 3.0, "10", "Ana", "Hola, revisemos la migración SMTP."),
        Utterance(3.5, 7.0, "11", "Luis", "Yo envío las credenciales el viernes."),
        Utterance(8.0, 9.0, "10", "Ana", "Perfecto, decidimos usar SES."),
    ]


@pytest.fixture
def notes() -> Notes:
    return Notes(meeting_title="Migración SMTP", tldr="Migrar a SES.", summary="Se revisó la migración.",
                 topics=(Topic("SMTP", ("Migrar a SES",)),), decisions=("Usar SES",),
                 open_questions=("¿Presupuesto?",),
                 action_items=(ActionItem(id="a0000000001", title="Enviar credenciales", owner_speaker_id="11",
                                          owner_name="Luis", due="2026-10-02", quote="Yo envío las credenciales",
                                          t0=3.5),
                               ActionItem(id="a0000000002", title="Revisar costos")),
                 language="es")



@pytest.fixture(scope="session")
def ff():
    try:
        return resolve_ffmpeg("")
    except FfmpegNotFound:
        pytest.skip("ffmpeg not available")


@pytest.fixture
def make_track(ff, tmp_path):
    def _make(name: str, seconds: float = 2.0, freq: int = 440, silence: bool = False) -> Path:
        out = tmp_path / "tracks" / f"{name}.ogg"
        out.parent.mkdir(parents=True, exist_ok=True)
        src = "anullsrc=r=48000:cl=mono" if silence else f"sine=frequency={freq}:sample_rate=48000"
        subprocess.run([str(ff.ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-t",
                        str(seconds), "-i", src, "-ac", "1", "-c:a", "libopus", "-b:a", "48k", str(out)], check=True)
        return out
    return _make
