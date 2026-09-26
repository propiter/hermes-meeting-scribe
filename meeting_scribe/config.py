"""Typed plugin settings (DESIGN §10) read from ``plugins.entries.meeting-scribe.settings``.

``SPEC`` is the single source of truth; ``plugin.yaml`` ``config_schema`` must mirror it (a unit
test enforces both directions). Settings are loaded through an injected getter
(``ctx.get_config``) and re-read per operation so an edit in the Desktop form takes effect
without restarting the gateway. Invalid values never crash the plugin: they fall back to the
default and surface as ``warnings`` (shown by ``doctor``).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Mapping, Optional

Getter = Callable[..., Any]
PRIMARY_COMMAND = "meeting"
MODES = ("approve", "auto", "off")
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class Opt:
    kind: str  # str | int | float | bool | list
    default: Any
    description: str
    choices: tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None

    @property
    def yaml_type(self) -> str:
        return self.kind

    @property
    def yaml_default(self) -> Any:
        return list(self.default) if isinstance(self.default, tuple) else self.default


SPEC: dict[str, Opt] = {
    "commands.aliases": Opt("list", ("meet", "rec"), "Extra slash-command names routed to /meeting."),
    "autojoin.enabled": Opt("bool", True, "Join a voice channel automatically when people gather."),
    "autojoin.min_humans": Opt("int", 2, "Humans required in a voice channel to auto-join.", minimum=1),
    "autojoin.grace_seconds": Opt("int", 20, "Seconds the channel must stay populated before joining.",
                                  minimum=0),
    "autojoin.channels": Opt("list", (), "Voice channel ids/names allowed for auto-join (empty = all)."),
    "autojoin.ignore_channels": Opt("list", (), "Voice channel ids/names never auto-joined."),
    "autoleave.grace_seconds": Opt("int", 60, "Seconds with no humans before the recording stops.",
                                   minimum=0),
    "limits.max_duration_minutes": Opt("int", 240, "Hard cap on a single recording.", minimum=1),
    "audio.retention": Opt("str", "multitrack", "Audio kept after processing.",
                           choices=("multitrack", "mixed", "none")),
    "audio.bitrate_kbps": Opt("int", 48, "Opus bitrate per speaker track.", minimum=8, maximum=256),
    "audio.ffmpeg_path": Opt("str", "", "Explicit ffmpeg binary (empty = auto-detect)."),
    "transcribe.model": Opt("str", "medium", "faster-whisper model (tiny/base/small/medium/large-v3...)."),
    "transcribe.device": Opt("str", "auto", "Inference device.", choices=("auto", "cpu", "cuda")),
    "transcribe.compute_type": Opt("str", "auto", "CTranslate2 compute type.",
                                   choices=("auto", "int8", "int8_float16", "float16", "float32")),
    "transcribe.cpu_threads": Opt("int", 0, "CPU threads for whisper (0 = cores minus 2).", minimum=0),
    "transcribe.language": Opt("str", "auto", "Spoken language code (auto = detect; pin it if you can)."),
    "transcribe.beam_size": Opt("int", 5, "Beam size for decoding.", minimum=1, maximum=10),
    "analysis.language": Opt("str", "auto", "Notes language (auto = transcript language)."),
    "analysis.chunk_chars": Opt("int", 12000, "Transcript chunk size for map-reduce analysis.",
                                minimum=2000),
    "projects.min_confidence": Opt("float", 0.6, "Minimum confidence to auto-assign a project.",
                                   minimum=0.0, maximum=1.0),
    "delivery.discord.enabled": Opt("bool", True, "Post notes to Discord."),
    "delivery.discord.channel": Opt("str", "", "Notes channel id (empty = voice text chat, then home)."),
    "delivery.discord.thread": Opt("bool", True, "Post notes in a thread when possible."),
    "owners": Opt("list", (), "Discord user ids whose tasks may go to Kanban (empty = first allowed user)."),
    "kanban.mode": Opt("str", "approve", "Kanban delivery of owner tasks.", choices=MODES),
    "kanban.board": Opt("str", "", "Kanban board slug (empty = default board)."),
    "linear.mode": Opt("str", "approve", "Linear issue creation.", choices=MODES),
    "linear.default_team": Opt("str", "", "Linear team key/id used when no project resolves."),
    "obsidian.vault_path": Opt("str", "", "Obsidian vault path (empty = disabled)."),
    "obsidian.folder": Opt("str", "Meetings", "Folder inside the vault for notes."),
    "ui.language": Opt("str", "en", "Language of bot messages.", choices=("en", "es")),
    "consent.announce": Opt("bool", True, "Announce recording in the channel chat."),
    "consent.nickname_prefix": Opt("str", "[REC] ", "Nickname prefix while recording (empty = off)."),
}

_TRUE = {"1", "true", "yes", "on", "si", "sí"}
_FALSE = {"0", "false", "no", "off", ""}


def _coerce(opt: Opt, raw: Any) -> Any:
    """Return the coerced value or raise ``ValueError`` describing why it is invalid."""
    if opt.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, str)) and str(raw).strip().lower() in _TRUE | _FALSE:
            return str(raw).strip().lower() in _TRUE
        raise ValueError("expected a boolean")
    if opt.kind in ("int", "float"):
        if isinstance(raw, bool):
            raise ValueError("expected a number")
        value: float = int(str(raw).strip()) if opt.kind == "int" else float(raw)
        if (opt.minimum is not None and value < opt.minimum) or (opt.maximum is not None and value > opt.maximum):
            raise ValueError(f"out of range [{opt.minimum}, {opt.maximum}]")
        return value
    if opt.kind == "list":
        items = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else [raw]
        return tuple(str(i).strip() for i in items if str(i).strip())
    value_s = "" if raw is None else str(raw)
    if opt.choices and value_s not in opt.choices:
        raise ValueError(f"expected one of {', '.join(opt.choices)}")
    return value_s


def _normalize_aliases(values: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for v in values:
        name = v.strip().lstrip("/").lower()
        if name != PRIMARY_COMMAND and _ALIAS_RE.match(name) and name not in out:
            out.append(name)
    return tuple(out)


@dataclass(frozen=True)
class Settings:
    commands_aliases: tuple[str, ...]
    autojoin_enabled: bool
    autojoin_min_humans: int
    autojoin_grace_seconds: int
    autojoin_channels: tuple[str, ...]
    autojoin_ignore_channels: tuple[str, ...]
    autoleave_grace_seconds: int
    limits_max_duration_minutes: int
    audio_retention: str
    audio_bitrate_kbps: int
    audio_ffmpeg_path: str
    transcribe_model: str
    transcribe_device: str
    transcribe_compute_type: str
    transcribe_cpu_threads: int
    transcribe_language: str
    transcribe_beam_size: int
    analysis_language: str
    analysis_chunk_chars: int
    projects_min_confidence: float
    delivery_discord_enabled: bool
    delivery_discord_channel: str
    delivery_discord_thread: bool
    owners: tuple[str, ...]
    kanban_mode: str
    kanban_board: str
    linear_mode: str
    linear_default_team: str
    obsidian_vault_path: str
    obsidian_folder: str
    ui_language: str
    consent_announce: bool
    consent_nickname_prefix: str
    warnings: tuple[str, ...] = field(default=(), compare=False)

    @classmethod
    def load(cls, getter: Getter) -> "Settings":
        values: dict[str, Any] = {}
        warnings: list[str] = []
        for key, opt in SPEC.items():
            raw = getter(key, opt.default)
            try:
                values[key.replace(".", "_")] = opt.default if raw is None else _coerce(opt, raw)
            except (ValueError, TypeError) as exc:
                warnings.append(f"{key}={raw!r} is invalid ({exc}); using {opt.yaml_default!r}")
                values[key.replace(".", "_")] = opt.default
        values["commands_aliases"] = _normalize_aliases(values["commands_aliases"])
        return cls(**values, warnings=tuple(warnings))

    @classmethod
    def defaults(cls) -> "Settings":
        return cls.load(lambda key, default=None: default)

    @property
    def effective_cpu_threads(self) -> int:
        return self.transcribe_cpu_threads or max(1, (os.cpu_count() or 1) - 2)

    def as_dict(self) -> dict[str, Any]:
        """Dotted-key view (``config`` subcommand / ``/meeting config``)."""
        names = {f.name for f in fields(self)}
        return {k: getattr(self, k.replace(".", "_")) for k in SPEC if k.replace(".", "_") in names}


def effective_owners(settings: Settings, secret: Callable[[str], Optional[str]]) -> tuple[str, ...]:
    """Configured owners, else the first ``DISCORD_ALLOWED_USERS`` entry (DESIGN §8).

    ``secret`` is ``agent.secret_scope.get_secret`` in production so the lookup honours the
    active profile; Phase B may extend this with the home-channel owner.
    """
    if settings.owners:
        return settings.owners
    allowed = [x.strip() for x in (secret("DISCORD_ALLOWED_USERS") or "").split(",") if x.strip()]
    return (allowed[0],) if allowed else ()


def schema_for_manifest() -> dict[str, dict[str, Any]]:
    """``config_schema`` mapping rendered from ``SPEC`` (used to regenerate plugin.yaml)."""
    out: dict[str, dict[str, Any]] = {}
    for key, opt in SPEC.items():
        entry: dict[str, Any] = {"type": opt.yaml_type, "default": opt.yaml_default,
                                 "description": opt.description}
        if opt.choices:
            entry["choices"] = list(opt.choices)
        out[key] = entry
    return out


def settings_from_mapping(values: Mapping[str, Any]) -> Settings:
    """Convenience for tests/CLI: load from a flat dotted mapping."""
    return Settings.load(lambda key, default=None: values.get(key, default))


def validate_value(key: str, raw: Any) -> Any:
    """Coerce one user-supplied value (CLI ``config set`` / setup) into what we store in config.yaml.

    Raises ``KeyError`` for unknown keys and ``ValueError`` (naming the key) for invalid values.
    """
    opt = SPEC[key]
    try:
        value = _coerce(opt, raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{key}: {exc}") from exc
    return list(value) if isinstance(value, tuple) else value
