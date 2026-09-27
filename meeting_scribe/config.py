"""Typed plugin settings (DESIGN §10) read from ``plugins.entries.meeting-scribe.settings``.

``SPEC`` is the single source of truth; ``plugin.yaml`` ``config_schema`` must mirror it (a unit
test enforces both directions). Keys are FLAT (``kanban_mode``): Hermes stores dotted keys as
nested YAML but its Desktop form reads them flat (DESIGN §15, review finding 10). Settings are loaded through an injected getter
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
_SNOWFLAKE_WRAP_RE = re.compile(r"^<[#@][!&]?(\d+)>$")


@dataclass(frozen=True)
class Opt:
    kind: str  # str | int | float | bool | list
    default: Any
    description: str
    choices: tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    snowflake: bool = False  # a Discord id: empty or digits (``<#id>`` / ``<@id>`` are unwrapped)

    @property
    def yaml_type(self) -> str:
        return self.kind

    @property
    def yaml_default(self) -> Any:
        return list(self.default) if isinstance(self.default, tuple) else self.default


SPEC: dict[str, Opt] = {
    "commands_aliases": Opt("list", ("meet", "rec"), "Extra slash-command names routed to /meeting."),
    "autojoin_enabled": Opt("bool", True, "Join a voice channel automatically when people gather."),
    "autojoin_min_humans": Opt("int", 2, "Humans required in a voice channel to auto-join.", minimum=1),
    "autojoin_grace_seconds": Opt("int", 20, "Seconds the channel must stay populated before joining.",
                                  minimum=0),
    "autojoin_channels": Opt("list", (), "Voice channel ids/names allowed for auto-join (empty = all)."),
    "autojoin_ignore_channels": Opt("list", (), "Voice channel ids/names never auto-joined."),
    "autoleave_grace_seconds": Opt("int", 60, "Seconds with no humans before the recording stops.",
                                   minimum=0),
    "limits_max_duration_minutes": Opt("int", 240, "Hard cap on a single recording.", minimum=1),
    "audio_retention": Opt("str", "multitrack", "Audio kept after processing.",
                           choices=("multitrack", "mixed", "none")),
    "audio_bitrate_kbps": Opt("int", 48, "Opus bitrate per speaker track.", minimum=8, maximum=256),
    "audio_ffmpeg_path": Opt("str", "", "Explicit ffmpeg binary (empty = auto-detect)."),
    "transcribe_model": Opt("str", "medium", "faster-whisper model (tiny/base/small/medium/large-v3...)."),
    "transcribe_device": Opt("str", "auto", "Inference device.", choices=("auto", "cpu", "cuda")),
    "transcribe_compute_type": Opt("str", "auto", "CTranslate2 compute type.",
                                   choices=("auto", "int8", "int8_float16", "float16", "float32")),
    "transcribe_cpu_threads": Opt("int", 0, "CPU threads for whisper (0 = cores minus 2).", minimum=0),
    "transcribe_language": Opt("str", "auto", "Spoken language code (auto = detect; pin it if you can)."),
    "transcribe_beam_size": Opt("int", 5, "Beam size for decoding.", minimum=1, maximum=10),
    "analysis_language": Opt("str", "auto", "Notes language (auto = transcript language)."),
    "analysis_chunk_chars": Opt("int", 12000, "Transcript chunk size for map-reduce analysis.",
                                minimum=2000),
    "projects_min_confidence": Opt("float", 0.6, "Minimum confidence to auto-assign a project.",
                                   minimum=0.0, maximum=1.0),
    "delivery_discord_enabled": Opt("bool", True, "Post notes to Discord."),
    "delivery_discord_channel": Opt("str", "", "Notes channel id (empty = voice text chat, then home).",
                                    snowflake=True),
    "delivery_discord_thread": Opt("bool", True, "Post notes in a thread when possible."),
    "delivery_project_threads": Opt("bool", True, "Post each task in a thread of its project's channel."),
    "delivery_dm_assignees": Opt("bool", True, "DM each assignee their tasks with buttons after delivery."),
    "delivery_discord_transcript": Opt("bool", True, "Attach the full transcript (Markdown file) to the notes."),
    "google_meet_enabled": Opt("bool", False, "Import Google Meet transcripts (needs `google connect`)."),
    "google_meet_poll_minutes": Opt("int", 5, "Minutes between Google Meet polls.", minimum=2, maximum=1440),
    "google_meet_discord_channel": Opt("str", "", "Discord text channel id for Google Meet notes "
                                       "(empty = delivery_discord_channel, then home).", snowflake=True),
    "project_channels": Opt("list", (), "Explicit project to channel map, entries like 'Project name=channel_id'."),
    "project_match_min_score": Opt("float", 0.8, "Minimum fuzzy score to route a task to a channel by name.",
                                   minimum=0.0, maximum=1.0),
    "channel_name_ignore_prefixes": Opt("list", (), "Decorative leading words ignored in channel names."),
    "owners": Opt("list", (), "Discord user ids whose tasks may go to Kanban (empty = first allowed user)."),
    "kanban_mode": Opt("str", "approve", "Kanban delivery of owner tasks.", choices=MODES),
    "kanban_board": Opt("str", "", "Kanban board slug (empty = default board)."),
    "linear_mode": Opt("str", "approve", "Linear issue creation.", choices=MODES),
    "linear_default_team": Opt("str", "", "Linear team key/id used when no project resolves."),
    "obsidian_vault_path": Opt("str", "", "Obsidian vault path (empty = disabled)."),
    "obsidian_folder": Opt("str", "Meetings", "Folder inside the vault for notes."),
    "ui_language": Opt("str", "en", "Language of bot messages.", choices=("en", "es")),
    "consent_announce": Opt("bool", True, "Announce recording in the channel chat."),
    "consent_nickname_prefix": Opt("str", "[REC] ", "Nickname prefix while recording (empty = off)."),
}
# Pre-0.2 dotted names. ``ctx.set_config("kanban.mode")`` stored NESTED YAML while Hermes' Desktop
# settings form reads ``settings[key]`` FLAT, so dotted keys always showed their defaults there
# (review finding 10). Canonical keys are now flat; the old nested values are still read as a fallback
# (and the dotted spelling is accepted by the CLI) so existing configs keep working.
LEGACY_KEYS: dict[str, str] = {
    "commands_aliases": "commands.aliases", "autojoin_enabled": "autojoin.enabled",
    "autojoin_min_humans": "autojoin.min_humans", "autojoin_grace_seconds": "autojoin.grace_seconds",
    "autojoin_channels": "autojoin.channels", "autojoin_ignore_channels": "autojoin.ignore_channels",
    "autoleave_grace_seconds": "autoleave.grace_seconds",
    "limits_max_duration_minutes": "limits.max_duration_minutes", "audio_retention": "audio.retention",
    "audio_bitrate_kbps": "audio.bitrate_kbps", "audio_ffmpeg_path": "audio.ffmpeg_path",
    "transcribe_model": "transcribe.model", "transcribe_device": "transcribe.device",
    "transcribe_compute_type": "transcribe.compute_type", "transcribe_cpu_threads": "transcribe.cpu_threads",
    "transcribe_language": "transcribe.language", "transcribe_beam_size": "transcribe.beam_size",
    "analysis_language": "analysis.language", "analysis_chunk_chars": "analysis.chunk_chars",
    "projects_min_confidence": "projects.min_confidence", "delivery_discord_enabled": "delivery.discord.enabled",
    "delivery_discord_channel": "delivery.discord.channel", "delivery_discord_thread": "delivery.discord.thread",
    "kanban_mode": "kanban.mode", "kanban_board": "kanban.board", "linear_mode": "linear.mode",
    "linear_default_team": "linear.default_team", "obsidian_vault_path": "obsidian.vault_path",
    "obsidian_folder": "obsidian.folder", "ui_language": "ui.language", "consent_announce": "consent.announce",
    "consent_nickname_prefix": "consent.nickname_prefix",
}
_BY_LEGACY = {v: k for k, v in LEGACY_KEYS.items()}
_MISSING = object()


def canonical_key(key: str) -> str:
    """Flat canonical name for ``key`` (accepts the legacy dotted spelling); ``KeyError`` if unknown."""
    flat = _BY_LEGACY.get(key, key)
    if flat not in SPEC:
        raise KeyError(key)
    return flat

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
    if opt.snowflake:
        value_s = _SNOWFLAKE_WRAP_RE.sub(r"\1", value_s.strip())
        if value_s and not value_s.isdigit():
            raise ValueError("expected a numeric Discord channel id (Developer Mode → Copy Channel ID)")
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
    delivery_project_threads: bool
    delivery_dm_assignees: bool
    delivery_discord_transcript: bool
    google_meet_enabled: bool
    google_meet_poll_minutes: int
    google_meet_discord_channel: str
    project_channels: tuple[str, ...]
    project_match_min_score: float
    channel_name_ignore_prefixes: tuple[str, ...]
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
            raw = getter(key, _MISSING)
            if raw is _MISSING and key in LEGACY_KEYS:
                raw = getter(LEGACY_KEYS[key], _MISSING)  # value saved by a pre-0.2 version (nested)
            try:
                values[key] = opt.default if raw is None or raw is _MISSING else _coerce(opt, raw)
            except (ValueError, TypeError) as exc:
                warnings.append(f"{key}={raw!r} is invalid ({exc}); using {opt.yaml_default!r}")
                values[key] = opt.default
        values["commands_aliases"] = _normalize_aliases(values["commands_aliases"])
        return cls(**values, warnings=tuple(warnings))

    @classmethod
    def defaults(cls) -> "Settings":
        return cls.load(lambda key, default=None: default)

    @property
    def effective_cpu_threads(self) -> int:
        return self.transcribe_cpu_threads or max(1, (os.cpu_count() or 1) - 2)

    def project_channel_map(self) -> dict[str, str]:
        """``project_channels`` as ``{folded project name: channel id}``; malformed entries are ignored."""
        from .domain.text import fold

        out: dict[str, str] = {}
        for entry in self.project_channels:
            name, sep, channel = entry.rpartition("=")
            if sep and fold(name) and channel.strip():
                out[fold(name)] = channel.strip()
        return out

    def as_dict(self) -> dict[str, Any]:
        """Canonical-key view (``config`` subcommand / ``/meeting config``)."""
        names = {f.name for f in fields(self)}
        return {k: getattr(self, k) for k in SPEC if k in names}


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
    """Convenience for tests/CLI: load from a mapping of canonical (or legacy dotted) keys."""
    flat = {_BY_LEGACY.get(k, k): v for k, v in values.items()}
    return Settings.load(lambda key, default=None: flat.get(key, default))


def validate_value(key: str, raw: Any) -> Any:
    """Coerce one user-supplied value (CLI ``config set`` / setup) into what we store in config.yaml.

    Raises ``KeyError`` for unknown keys and ``ValueError`` (naming the key) for invalid values.
    """
    opt = SPEC[canonical_key(key)]
    try:
        value = _coerce(opt, raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{key}: {exc}") from exc
    return list(value) if isinstance(value, tuple) else value
