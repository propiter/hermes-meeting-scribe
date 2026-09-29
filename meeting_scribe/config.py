"""Typed plugin settings (DESIGN §10) read from ``plugins.entries.meeting-scribe.settings``.

``SPEC`` is the single source of truth; ``plugin.yaml`` ``config_schema`` must mirror it (a unit
test enforces both directions). Keys are FLAT (``kanban_mode``): Hermes stores dotted keys as
nested YAML but its Desktop form reads them flat (DESIGN §15, review finding 10). Settings are loaded through an injected getter
(``ctx.get_config``) and re-read per operation so an edit in the Desktop form takes effect
without restarting the gateway. Invalid values never crash the plugin: they fall back to the
default and surface as ``warnings`` (shown by ``doctor``).
"""
from __future__ import annotations

import math

import os
import re
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Mapping, Optional

from .domain.text import is_ascii_digits

Getter = Callable[..., Any]
PRIMARY_COMMAND = "meeting"
MODES = ("approve", "auto", "off")
# Where a meeting's task cards go (DESIGN §16.1). ``meeting``: with the summary, one place per meeting;
# ``projects``: a thread per meeting in each project's channel; ``projects_inline``: straight in it.
TASKS_MEETING, TASKS_PROJECTS, TASKS_PROJECTS_INLINE = "meeting", "projects", "projects_inline"
TASK_PLACEMENTS = (TASKS_MEETING, TASKS_PROJECTS, TASKS_PROJECTS_INLINE)
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_CHANNEL_MENTION_RE = re.compile(r"^<#([0-9]+)>$")
_NAME_MAX = 100  # Discord caps channel and server names at 100 characters


GROUPS: tuple[str, ...] = ("capture", "transcription", "analysis", "llm", "delivery", "projects", "google_meet",
                           "integrations", "privacy", "pipeline", "ui")
"""Form sections, in display order. ``llm`` is virtual: its values live in Hermes' own config
(``auxiliary.meeting_scribe``), see :mod:`meeting_scribe.llm_config`."""
FORMATS = ("", "discord_channel", "discord_guild")
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Opt:
    kind: str  # str | int | float | bool | list
    default: Any
    group: str
    choices: tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    # ``discord_channel``: a text channel id, ``<#id>`` (unwrapped) or a channel NAME resolved at runtime
    # against the server; ``discord_guild``: a server id or name.
    format: str = ""
    # ``space``: a space may override the global value (DESIGN §23); ``global``: one value for the
    # whole install (the machine: models, worker counts, ffmpeg, the shared bot's commands).
    scope: str = "space"

    @property
    def yaml_type(self) -> str:
        return self.kind

    @property
    def yaml_default(self) -> Any:
        return list(self.default) if isinstance(self.default, tuple) else self.default

    def label(self, key: str, lang: str = "en") -> str:
        from .i18n import t
        return t(f"cfg.{key}.label", lang)

    def help(self, key: str, lang: str = "en") -> str:
        from .i18n import t
        return t(f"cfg.{key}.help", lang)


_CH = "discord_channel"
AUTO_CHANNEL_NAMES = ("general", "meetings", "meeting-notes", "notes", "reuniones", "notas")
SPEC: dict[str, Opt] = {
    # capture
    "autojoin_enabled": Opt("bool", True, "capture"),
    "autojoin_min_humans": Opt("int", 2, "capture", minimum=1),
    "autojoin_grace_seconds": Opt("int", 20, "capture", minimum=0),
    "autojoin_channels": Opt("list", (), "capture"),
    "autojoin_ignore_channels": Opt("list", (), "capture"),
    "autoleave_grace_seconds": Opt("int", 60, "capture", minimum=0),
    "limits_max_duration_minutes": Opt("int", 240, "capture", minimum=1),
    "audio_bitrate_kbps": Opt("int", 48, "capture", minimum=8, maximum=256, scope="global"),
    "audio_ffmpeg_path": Opt("str", "", "capture", scope="global"),
    # transcription
    "transcribe_model": Opt("str", "medium", "transcription", scope="global"),
    "transcribe_device": Opt("str", "auto", "transcription", choices=("auto", "cpu", "cuda"), scope="global"),
    "transcribe_compute_type": Opt("str", "auto", "transcription",
                                   choices=("auto", "int8", "int8_float16", "float16", "float32"), scope="global"),
    "transcribe_cpu_threads": Opt("int", 0, "transcription", minimum=0, scope="global"),
    "transcribe_language": Opt("str", "auto", "transcription"),
    "transcribe_beam_size": Opt("int", 5, "transcription", minimum=1, maximum=10, scope="global"),
    # analysis
    "analysis_language": Opt("str", "auto", "analysis"),
    "analysis_chunk_chars": Opt("int", 12000, "analysis", minimum=2000, scope="global"),
    "analysis_timeout_seconds": Opt("int", 600, "analysis", minimum=30, maximum=7200, scope="global"),
    "analysis_max_tokens": Opt("int", 8192, "analysis", minimum=256, maximum=200000, scope="global"),
    # delivery
    "delivery_discord_enabled": Opt("bool", True, "delivery"),
    "delivery_discord_guild": Opt("str", "", "delivery", format="discord_guild"),
    "delivery_discord_channel": Opt("str", "", "delivery", format=_CH),
    "delivery_auto_channel_names": Opt("list", AUTO_CHANNEL_NAMES, "delivery"),
    "delivery_fallback_channel": Opt("str", "", "delivery", format=_CH),
    "delivery_discord_thread": Opt("bool", True, "delivery"),
    "delivery_tasks_placement": Opt("str", TASKS_MEETING, "delivery", choices=TASK_PLACEMENTS),
    "delivery_dm_assignees": Opt("bool", True, "delivery"),
    "delivery_discord_transcript": Opt("bool", True, "delivery"),
    "delivery_mention_participants": Opt("bool", True, "delivery"),
    "delivery_transcript_max_mb": Opt("int", 8, "delivery", minimum=1, maximum=500),
    "delivery_forum_tags": Opt("list", (), "delivery"),
    "delivery_forum_default_tag": Opt("list", (), "delivery"),
    "meeting_routes": Opt("list", (), "delivery", format="meeting_route"),
    # projects
    "projects_min_confidence": Opt("float", 0.6, "projects", minimum=0.0, maximum=1.0),
    "project_channels": Opt("list", (), "projects", format="project_channel"),
    "project_match_min_score": Opt("float", 0.8, "projects", minimum=0.0, maximum=1.0),
    "channel_name_ignore_prefixes": Opt("list", (), "projects"),
    # google meet
    "google_meet_enabled": Opt("bool", False, "google_meet"),
    "google_meet_poll_minutes": Opt("int", 5, "google_meet", minimum=2, maximum=1440, scope="global"),
    "google_meet_discord_channel": Opt("str", "", "google_meet", format=_CH),
    # integrations
    "owners": Opt("list", (), "integrations"),
    "kanban_mode": Opt("str", "approve", "integrations", choices=MODES),
    "kanban_board": Opt("str", "", "integrations"),
    "linear_mode": Opt("str", "approve", "integrations", choices=MODES),
    "linear_default_team": Opt("str", "", "integrations"),
    "obsidian_vault_path": Opt("str", "", "integrations"),
    "obsidian_folder": Opt("str", "Meetings", "integrations"),
    # privacy
    "audio_retention": Opt("str", "multitrack", "privacy", choices=("multitrack", "mixed", "none")),
    "consent_announce": Opt("bool", True, "privacy"),
    "consent_nickname_prefix": Opt("str", "[REC] ", "privacy"),
    # pipeline
    "pipeline_max_attempts": Opt("int", 3, "pipeline", minimum=1, maximum=20, scope="global"),
    "pipeline_workers": Opt("int", 2, "pipeline", minimum=1, maximum=8, scope="global"),
    "pipeline_max_transcriptions": Opt("int", 1, "pipeline", minimum=1, maximum=8, scope="global"),
    # ui
    "ui_language": Opt("str", "en", "ui", choices=("en", "es")),
    "commands_aliases": Opt("list", ("meet", "rec"), "ui", scope="global"),
}
CHANNEL_KEYS = tuple(k for k, o in SPEC.items() if o.format == _CH)
# Changing one of these may unblock a delivery waiting for a channel (DESIGN §19): re-queue it.
DESTINATION_KEYS = CHANNEL_KEYS + ("delivery_discord_guild", "delivery_auto_channel_names", "project_channels",
                                   "delivery_forum_tags", "delivery_forum_default_tag", "meeting_routes")
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
# Settings replaced by another one with different values: ``{old key: (new key, old value -> new value)}``.
# An old value still in config.yaml (or a space override) is read when the new key is unset, and
# ``Settings.warnings`` says how to write it the new way.
RETIRED_KEYS: dict[str, tuple[str, Callable[[bool], str]]] = {
    "delivery_project_threads": ("delivery_tasks_placement",
                                 lambda threads: TASKS_PROJECTS if threads else TASKS_PROJECTS_INLINE),
}


def raw_value(getter: Getter, key: str, space: str = "",
              overrides: Mapping[str, Any] = {}) -> tuple[Any, str]:  # noqa: B006 - read only
    """``(stored value or _MISSING, where it came from)``, most specific first: the space's override
    (then its override of a retired key ``key`` replaced), then the global value (flat, its pre-0.2
    dotted spelling, then a retired key). A retired value is returned already converted; ``where``
    names the retired key so the caller can warn."""
    retired = [(old, convert) for old, (new, convert) in RETIRED_KEYS.items() if new == key]
    if SPEC[key].scope == "space":
        if overrides.get(key) is not None:
            return overrides[key], f"space {space}: {key}"
        for old, convert in retired:
            if overrides.get(old) is not None:
                return _retired(convert, overrides[old]), f"space {space}: {old}"
    raw = getter(key, _MISSING)
    if raw is _MISSING and key in LEGACY_KEYS:
        raw = getter(LEGACY_KEYS[key], _MISSING)  # value saved by a pre-0.2 version (nested)
    if raw is None or raw is _MISSING:
        for old, convert in retired:
            value = getter(old, None)
            if value is not None:
                return _retired(convert, value), old
    return raw, key


def _retired(convert: Callable[[bool], str], raw: Any) -> Any:
    """A retired boolean converted to its replacement's value; an unreadable one stays as written (it is
    then reported invalid and the default applies)."""
    try:
        return convert(_coerce(Opt("bool", True, ""), raw))
    except ValueError:
        return raw


def stored_names(key: str) -> tuple[str, ...]:
    """Every name ``key``'s value may be stored under: itself, its pre-0.2 spelling and a retired key it
    replaced (``config list`` / the Settings page show such a value as ``configured``)."""
    retired = tuple(old for old, (new, _) in RETIRED_KEYS.items() if new == key)
    return (key, *((LEGACY_KEYS[key],) if key in LEGACY_KEYS else ()), *retired)


def retired_hint(key: str) -> str:
    """What to write instead of a retired setting (``""``: ``key`` is not one)."""
    if key not in RETIRED_KEYS:
        return ""
    new, _ = RETIRED_KEYS[key]
    return f"{key} was replaced by {new} (one of: {', '.join(SPEC[new].choices)})"


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
        try:
            value: float = int(str(raw).strip()) if opt.kind == "int" else float(raw)
        except (ValueError, TypeError, OverflowError):
            raise ValueError("expected a finite number") from None
        if opt.kind == "float" and not math.isfinite(value):
            raise ValueError("expected a finite number")
        if (opt.minimum is not None and value < opt.minimum) or (opt.maximum is not None and value > opt.maximum):
            raise ValueError(f"out of range [{opt.minimum}, {opt.maximum}]")
        return value
    if opt.kind == "list":
        items = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else [raw]
        return tuple(str(i).strip() for i in items if str(i).strip())
    value_s = "" if raw is None else str(raw)
    if opt.format == _CH:
        value_s = _channel_value(value_s)
    elif opt.format == "discord_guild":
        value_s = _name_or_id(value_s, "a Discord server id or name")
    if opt.choices and value_s not in opt.choices:
        raise ValueError(f"expected one of {', '.join(opt.choices)}")
    return value_s


def _name_or_id(raw: str, what: str) -> str:
    value = raw.strip()
    if len(value) > _NAME_MAX or any(c in value for c in "\r\n\t"):
        raise ValueError(f"expected {what}")
    return value


def _channel_value(raw: str) -> str:
    """``123`` / ``<#123>`` → ``123``; ``#name`` / ``name`` → ``name`` (resolved at runtime)."""
    value = raw.strip()
    mention = _CHANNEL_MENTION_RE.match(value)
    if mention:
        return mention.group(1)
    if value.startswith("<"):
        raise ValueError("expected a text channel (id, <#id> or name), not a user or role mention")
    if not value or is_ascii_digits(value):
        return value
    name = value.lstrip("#").strip()
    if not name:
        raise ValueError("expected a Discord channel id, <#id> or a channel name")
    return _name_or_id(name, "a Discord channel id, <#id> or a channel name (max 100 characters, one line)")


def channel_ref(value: str) -> tuple[str, str]:
    """``("id", "123")``, ``("name", "notes")`` or ``("", "")`` for a stored channel setting."""
    value = (value or "").strip()
    if not value:
        return "", ""
    return ("id", value) if is_ascii_digits(value) else ("name", value)


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
    analysis_timeout_seconds: int
    analysis_max_tokens: int
    projects_min_confidence: float
    delivery_discord_enabled: bool
    delivery_discord_guild: str
    delivery_discord_channel: str
    delivery_auto_channel_names: tuple[str, ...]
    delivery_fallback_channel: str
    delivery_transcript_max_mb: int
    delivery_forum_tags: tuple[str, ...]
    delivery_forum_default_tag: tuple[str, ...]
    meeting_routes: tuple[str, ...]
    pipeline_max_attempts: int
    pipeline_workers: int
    pipeline_max_transcriptions: int
    delivery_discord_thread: bool
    delivery_tasks_placement: str
    delivery_dm_assignees: bool
    delivery_discord_transcript: bool
    delivery_mention_participants: bool
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
    space: str = ""  # the space these values were resolved for ("" = the global defaults)
    warnings: tuple[str, ...] = field(default=(), compare=False)

    @classmethod
    def load(cls, getter: Getter, space: str = "", overrides: Optional[Mapping[str, Any]] = None) -> "Settings":
        """Global values (``getter``), with ``space``'s ``overrides`` on top for the keys a space may
        override (DESIGN §23)."""
        values: dict[str, Any] = {}
        warnings: list[str] = []
        for key, opt in SPEC.items():
            raw, where = raw_value(getter, key, space, overrides or {})
            try:
                values[key] = opt.default if raw is None or raw is _MISSING else _coerce(opt, raw)
            except (ValueError, TypeError):
                warnings.append(f"{where}=invalid; using {opt.yaml_default!r}")
                values[key] = opt.default
                continue
            if where.rsplit(": ", 1)[-1] in RETIRED_KEYS:  # e.g. delivery_project_threads, kept by an older version
                warnings.append(f"{where} is retired; read as {key}={values[key]}. Save {key} "
                                "(config set or the Settings page) to make it explicit")
        values["commands_aliases"] = _normalize_aliases(values["commands_aliases"])
        from .routes import load_routes

        warnings += load_routes(values["meeting_routes"])[1]
        return cls(**values, space=space, warnings=tuple(warnings))

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

    def routes(self) -> list[Any]:
        """``meeting_routes`` as :class:`~meeting_scribe.routes.MeetingRoute` rules (broken ones kept:
        they match and fail closed, DESIGN §19.2)."""
        from .routes import load_routes

        return load_routes(self.meeting_routes)[0]

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
    """``config_schema`` mapping rendered from ``SPEC`` (used to regenerate plugin.yaml).

    Hermes reads ``type``/``default``/``description``/``choices``/``label``; ``group``, ``format``,
    ``minimum`` and ``maximum`` are extra hints for richer forms (unknown keys are ignored)."""
    out: dict[str, dict[str, Any]] = {}
    for key, opt in SPEC.items():
        entry: dict[str, Any] = {"type": opt.yaml_type, "default": opt.yaml_default,
                                 "label": opt.label(key), "description": opt.help(key), "group": opt.group}
        if opt.choices:
            entry["choices"] = list(opt.choices)
        if opt.minimum is not None:
            entry["minimum"] = opt.minimum
        if opt.maximum is not None:
            entry["maximum"] = opt.maximum
        if opt.format:
            entry["format"] = opt.format
        out[key] = entry
    return out


def _field(key: str, opt: Opt, lang: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"key": key, "type": opt.kind, "group": opt.group, "label": opt.label(key, lang),
                             "help": opt.help(key, lang), "default": opt.yaml_default, "storage": "plugin",
                             "scope": opt.scope, "path": f"plugins.entries.meeting-scribe.settings.{key}"}
    for name in ("choices", "minimum", "maximum", "format"):
        value = getattr(opt, name)
        if value not in ((), None, ""):
            entry[name] = list(value) if isinstance(value, tuple) else value
    return entry


def config_schema(lang: str = "en") -> dict[str, Any]:
    """Stable, versioned description of every setting (``config schema --json``).

    A UI draws the whole form from it: groups in order, then fields with kind, bounds, choices,
    localized label/help, default and where the value is stored (``storage``/``path``). The ``llm``
    group is virtual: its fields live in Hermes' config under ``auxiliary.meeting_scribe``.
    Bump ``SCHEMA_VERSION`` only for incompatible shape changes (adding fields is compatible)."""
    from .i18n import normalize_language, t
    from .llm_config import schema_fields as llm_fields

    lang = normalize_language(lang)
    fields_: list[dict[str, Any]] = [_field(k, o, lang) for k, o in SPEC.items()]
    fields_ += llm_fields(lang)
    return {"version": SCHEMA_VERSION, "plugin": "meeting-scribe", "language": lang,
            "groups": [{"key": g, "label": t(f"cfg.group.{g}", lang)} for g in GROUPS], "fields": fields_}


def settings_from_mapping(values: Mapping[str, Any], space: str = "",
                          overrides: Optional[Mapping[str, Any]] = None) -> Settings:
    """Convenience for tests/CLI: load from a mapping of canonical (or legacy dotted) keys."""
    flat = {_BY_LEGACY.get(k, k): v for k, v in values.items()}
    return Settings.load(lambda key, default=None: flat.get(key, default), space, overrides)


def space_keys() -> tuple[str, ...]:
    """The settings a space may override, in form order."""
    return tuple(k for k, o in SPEC.items() if o.scope == "space")


def validate_value(key: str, raw: Any) -> Any:
    """Coerce one user-supplied value (CLI ``config set`` / setup) into what we store in config.yaml.

    Raises ``KeyError`` for unknown keys and ``ValueError`` (naming the key) for invalid values.
    """
    opt = SPEC[canonical_key(key)]
    try:
        value = _coerce(opt, raw)
        if opt.format == "project_channel":  # strict on write only: loading stays lenient (bad rows ignored)
            value = tuple(_project_channel(entry) for entry in value)
        elif opt.format == "meeting_route":
            from .routes import validate_entries

            value = tuple(validate_entries(value))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{key}: {exc}") from exc
    return list(value) if isinstance(value, tuple) else value


def _project_channel(entry: str) -> str:
    """``Project = 123`` / ``Project=<#123>`` → ``Project=123`` (one project per Discord channel id)."""
    name, sep, channel = entry.rpartition("=")
    name = name.strip()
    if not sep or not name:
        raise ValueError(f"expected 'Project = channel id', got {entry!r}")
    channel = _channel_value(channel)
    if not is_ascii_digits(channel):
        raise ValueError(f"{name}: expected a Discord channel id (or <#id>)")
    return f"{_name_or_id(name, 'a project name (max 100 characters, one line)')}={channel}"
