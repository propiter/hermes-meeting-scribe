"""Phase B doctor checks (DESIGN §11): compat probe, voice deps, intents, permissions."""
from __future__ import annotations

from types import SimpleNamespace

from meeting_scribe.capture import checks
from meeting_scribe.capture.compat import CompatResult


def env(capture=None):
    return SimpleNamespace(capture=capture)


def test_compat_ok_and_incompatible(monkeypatch):
    monkeypatch.setattr(checks, "probe_hermes", lambda: CompatResult(True, (), ("a", "b")))
    assert checks.check_discord_compat(env()).status == "ok"
    monkeypatch.setattr(checks, "probe_hermes", lambda: CompatResult(False, ("VoiceReceiver._on_packet changed",), ()))
    res = checks.check_discord_compat(env())
    assert res.status == "fail" and "_on_packet" in res.detail


def test_compat_prefers_live_adapter_result(monkeypatch):
    monkeypatch.setattr(checks, "probe_hermes", lambda: (_ for _ in ()).throw(AssertionError("not used")))
    cap = SimpleNamespace(compat_result=CompatResult(True, (), ("x",)), adapter=object())
    assert checks.check_discord_compat(env(cap)).status == "ok"


def test_voice_deps(monkeypatch):
    monkeypatch.setattr(checks, "_module_available", lambda name: True)
    monkeypatch.setattr(checks, "_opus_loaded", lambda: (True, "libopus.so.0"))
    assert checks.check_voice_deps(env()).status == "ok"
    monkeypatch.setattr(checks, "_module_available", lambda name: name != "davey")
    res = checks.check_voice_deps(env())
    assert res.status == "fail" and "davey" in res.detail
    monkeypatch.setattr(checks, "_module_available", lambda name: True)
    monkeypatch.setattr(checks, "_opus_loaded", lambda: (False, "not found"))
    assert checks.check_voice_deps(env()).status == "fail"


def _guild(**perms):
    base = dict(connect=True, send_messages=True, create_public_threads=True, manage_nicknames=False,
                view_channel=True)
    base.update(perms)
    me = SimpleNamespace(guild_permissions=SimpleNamespace(**base))
    return SimpleNamespace(name="Acme", me=me)


def test_intents_and_permissions_without_adapter_are_hints():
    assert checks.check_discord_intents(env()).status == "ok"
    res = checks.check_discord_permissions(env())
    assert res.status == "ok" and "Connect" in res.detail


def test_intents_from_live_client():
    client = SimpleNamespace(intents=SimpleNamespace(voice_states=False), guilds=[])
    cap = SimpleNamespace(adapter=SimpleNamespace(_client=client), compat_result=None)
    assert checks.check_discord_intents(env(cap)).status == "fail"
    client.intents.voice_states = True
    assert checks.check_discord_intents(env(cap)).status == "ok"


def test_permissions_per_guild():
    client = SimpleNamespace(intents=SimpleNamespace(voice_states=True), guilds=[_guild()])
    cap = SimpleNamespace(adapter=SimpleNamespace(_client=client), compat_result=None)
    res = checks.check_discord_permissions(env(cap))
    assert res.status == "ok" and "Manage Nicknames" in res.detail  # optional → mentioned, not fatal
    client.guilds = [_guild(connect=False, create_public_threads=False)]
    res = checks.check_discord_permissions(env(cap))
    assert res.status == "warn" and "Connect" in res.detail and "Create Public Threads" in res.detail


def test_register_adds_all_checks():
    added = {}
    checks.register(lambda name, fn: added.setdefault(name, fn))
    assert set(added) == {"discord_compat", "discord_voice_deps", "discord_intents", "discord_permissions"}
