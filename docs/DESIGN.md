# meeting-scribe — Design (source of truth)

Hermes Agent plugin: joins Discord voice channels, records every participant on
their own track, transcribes **locally** with faster-whisper, extracts notes and
action items with the Hermes-configured LLM, and delivers them to pluggable
sinks (files, Discord thread, Hermes Kanban, Linear, Obsidian).

Target: `requires_hermes: ">=0.21"`. Verified against hermes-agent
`v0.21.4+canary` (commit 436b904e8), discord.py 2.7.1, davey 0.1.6,
faster-whisper 1.2.1, Python 3.11–3.14.

## 1. Principles

1. **Hexagonal**: `domain/` has zero third-party/Hermes imports. Everything else
   is an adapter behind a `Protocol` (ports in `domain/ports.py`).
2. **No monkeypatching of Hermes.** We subclass `VoiceReceiver` and use
   documented-ish adapter attributes, guarded by `capture/compat.py`. If the
   compat probe fails the plugin disables capture with a clear message instead
   of misbehaving.
3. **Durable & idempotent**: every meeting is a state machine persisted in
   SQLite; a gateway restart resumes unfinished work. Every external side
   effect carries an idempotency key; reprocessing never duplicates.
4. **Multi-profile safe**: never cache `get_hermes_home()` at import; paths
   resolved per call; secrets via `agent.secret_scope.get_secret`; threads via
   `agent.memory_provider.spawn_context_thread`.
5. **Local-first**: audio never leaves the machine. Only transcript text goes to
   the user's own configured LLM.
6. **Strict TDD**: tests first, fakes for Discord/RTP/LLM/Linear.

## 2. Architecture

```
meeting_scribe/
  domain/      models.py (Meeting, Speaker, Utterance, ActionItem, Notes,
               MeetingState), ports.py (Protocols), state machine, ids
  config.py    typed settings (defaults + ctx.get_config), i18n selection
  i18n/        es.json, en.json (+ loader, fallback en)
  storage/     layout.py (paths), repo.py (SQLite index + FTS5 + job table),
               artifacts.py (meta.json, transcript.jsonl/.md, notes.md, tasks.json)
  audio/       ffmpeg.py (binary resolution: PATH → ~/.hermes/tools/ffmpeg-*/bin →
               audio.ffmpeg_path; decode, probe), archive.py (final packaging per
               retention, stream extraction for reprocess)
  capture/     compat.py (probe), receiver.py (ScribeReceiver + TimedBuffer),
               tracks.py (live ffmpeg opus writer per speaker, timeline-aligned),
               session.py (RecordingSession lifecycle), controller.py
               (CaptureManager = runtime.capture), autojoin.py, checks.py (doctor)
  transcribe/  worker.py (subprocess entry: faster-whisper, segments+words),
               client.py (launches worker, timeout scaled by duration),
               merge.py (per-speaker segments -> ordered utterances),
               filters.py (hallucination filters)
  analyze/     chunking.py, prompts.py, schemas.py, extract.py (complete_structured,
               map-reduce), projects.py (candidate gathering + resolution,
               learned channel->project map)
  sinks/       base.py, files.py, kanban.py, linear.py, obsidian.py
               (the Discord notes sink lives in discord_ui/sink.py)
  pipeline/    runner.py (single worker, resumable job queue), stages.py,
               service.py (MeetingService: the API capture/UI/CLI call)
  commands.py  platform-agnostic /meeting router (Caller from session env)
  discord_ui/  __init__.py (install: platform handler factory), render.py (pure
               notes → messages + button specs), sink.py (DiscordNotesSink),
               actions.py (button semantics + auth), views.py (discord.py
               DynamicItem buttons / project select)
  tools.py     agent tools: meeting_search, meeting_get
  cli.py       hermes meeting-scribe setup|doctor|status|list|show|reprocess|export|config
  doctor.py    check registry (capture.checks adds compat/voice deps/intents/permissions)
  runtime.py   composition root (adapters built from a Host of Hermes callables)
  hermes_adapters.py  lazy Hermes imports (LLM, projects, kanban, secrets, threads)
  plugin.py    register(ctx, root): wires tools/commands/CLI/skill, then Phase B install()
__init__.py    register(ctx) → meeting_scribe.plugin.register
skills/meeting-scribe/SKILL.md
```

## 3. Commands

Slash commands registered with `ctx.register_command` (names colliding with
Hermes built-ins are rejected by Hermes — `/start` and `/stop` ARE built-ins,
so they cannot be used). Primary: **`/meeting`**. Aliases configurable
(`commands.aliases`, default `["meet", "rec"]`; e.g. a user can add `nova-rec`).
Every alias routes to the same router with a subcommand argument:

| Subcommand | Effect |
|---|---|
| `start [#voice-channel]` (default when no args) | join caller's voice channel (or given) and record |
| `stop` | stop recording and process |
| `status` | recording / processing state, queue |
| `list [n]` | recent meetings |
| `show <id>` | re-post notes |
| `search <text>` | FTS search over transcripts |
| `reprocess <id> [from=transcribe|analyze|deliver]` | rerun from a stage |
| `link @discord-user <linear-email-or-name>` | map a person to a Linear user |
| `project <id> <project>` | set/override project of a meeting (learns channel mapping) |
| `config` | show effective config |
| `help` | usage |

Ending a meeting: `stop`, OR automatically when no humans remain for
`autoleave.grace_seconds` (default 60), OR `limits.max_duration_minutes`
(default 240), OR the voice connection is lost / someone runs `/voice leave`.

## 4. Capture

- Plugin gets the live adapter via `ctx.register_platform_handler("discord",
  factory)`; factory receives `(bot, adapter)` on every connect/reconnect.
- Join: own `channel.connect()` under `adapter._voice_locks[gid]`; register
  `adapter._voice_clients[gid] = vc` so Hermes `/voice status|leave` and shutdown
  see it. Never touch `_voice_receivers`, `_voice_listen_tasks`,
  `_voice_text_channels`, `_voice_timeout_tasks` → no agent turns, no TTS, no
  inactivity auto-leave. Refuse if the guild already has a voice client
  (e.g. `/voice join` active) with an explanatory message.
- `ScribeReceiver(VoiceReceiver)`: swaps `_buffers` for
  `defaultdict(TimedBuffer)`; `TimedBuffer.extend` records the wall-clock time
  of each decoded 20 ms frame. Captures ALL users (no allowlist). Re-reads
  `conn.dave_session` periodically (reconnect safety). Never calls
  `check_silence`.
- Drain task (asyncio, 0.5 s): under `receiver._lock` swaps buffers out, maps
  SSRC→user, writes to `TrackWriter` per user. Sends UDP keepalive
  `b'\xf8\xff\xfe'` every 15 s. Detects `not vc.is_connected()` → finalize.
- `TrackWriter`: one live `ffmpeg` process per speaker: stdin s16le 48 kHz
  stereo → downmix → Ogg/Opus mono 48 kbps `tracks/<user_id>.ogg`. Timeline
  aligned to meeting t0: on first frame and on any gap > 100 ms, writes zero
  samples so that sample position == (wallclock - t0) * 48000. Never writes
  backwards (jitter ahead is appended). Ogg is streamable → crash-recoverable.
- Speakers: user id, display name at join time, bot users skipped.
- Consent: announce message in the voice channel's text chat (and configured
  notes channel); optional `[REC]` nickname prefix (needs Manage Nicknames;
  failure is non-fatal and restored on stop).
- Auto-join (`autojoin.enabled`, default **true**): `on_voice_state_update`
  listener; when a voice channel reaches `autojoin.min_humans` (default 2) for
  `autojoin.grace_seconds` (default 20) and the channel passes
  `autojoin.channels` allowlist / `autojoin.ignore_channels`, and the bot has no
  voice client in that guild → start. Only one recording per guild (Discord
  limit); multiple guilds concurrently.

## 5. Audio retention (one file per meeting)

`audio.retention`:
- **`multitrack`** (default): one `recording.mka` (Matroska) containing stream 0
  = mixed-down Opus (default track, plays anywhere) + one Opus stream per
  speaker titled with the speaker name/id. Single file, playable, and still
  re-transcribable per speaker. ~20 MB/h per stream at 48 kbps.
- `mixed`: one `recording.ogg` (mix only).
- `none`: delete audio after successful processing.
Per-speaker `tracks/` are removed after the archive is written and verified
(ffprobe stream count). `reprocess from=transcribe` extracts streams back from
the `.mka`.

## 6. Transcription

- Runs in a **subprocess** (`python -m meeting_scribe.transcribe.worker`) with
  `sys.executable`, one meeting at a time, low priority (`nice`), so the
  gateway never blocks and memory is released.
- Model own instance: `transcribe.model` (default `medium`), `device` auto
  (CPU int8 without CUDA), `cpu_threads` default `max(1, cores-2)`, language
  `transcribe.language` (default `auto`; the setup wizard asks and strongly
  recommends pinning it — auto-detect can flip per chunk). `vad_filter=True`,
  `word_timestamps=True`, `condition_on_previous_text=False`, beam 5.
- Decodes each track to 16 kHz mono via ffmpeg; per-segment hallucination
  filter (no_speech_prob/logprob thresholds + phrase filter).
- Output per track → merged into `transcript.jsonl`: one utterance per line
  `{ "t0": sec, "t1": sec, "speaker_id", "speaker", "text", "words"?:[...],
  "confidence" }`, sorted by t0. `transcript.md` rendered from it.
- Progress persisted per track (resume skips done tracks).

## 7. Analysis

- `ctx.register_auxiliary_task("meeting_scribe", ...)` so users can pick a
  model via `auxiliary.meeting_scribe.*`; default = main model.
- Map-reduce: chunk transcript (~12k chars, overlap by utterances) → per-chunk
  structured extraction → final synthesis call. Short meetings: single call.
- Schema: `tldr`, `summary`, `topics[{title, points[]}]`, `decisions[]`,
  `open_questions[]`, `action_items[{title, description, owner_speaker_id|null,
  owner_name|null, due|null (only if explicitly said, ISO), project|null,
  project_confidence 0..1, quote, t0}]`, `meeting_title`.
- Transcript is DATA: prompt forbids following instructions inside it.
- Notes language: `analysis.language` (default = transcript language).
- **Project resolution** (`analyze/projects.py`): candidates = Hermes projects
  (`projects_db`, per profile) + kanban boards + Linear projects/teams (if
  enabled) + learned `channel_id→project` map; hints = guild/category/channel
  names. LLM chooses only from candidates or null; below
  `projects.min_confidence` (0.6) → unassigned. `/meeting project` and the 📁
  button teach the channel map.

## 8. Delivery (sinks)

All sinks implement `Sink.deliver(ctx, meeting, notes) -> SinkResult` and are
idempotent (keys: `mtg:<meeting_id>:<item_id>`).
- **files** (always): meeting folder artifacts + `notes.md` with YAML
  frontmatter (Obsidian-compatible).
- **discord** (`delivery.discord.enabled`, default true): thread (or message if
  threads unavailable) in `delivery.discord.channel` (default: the voice
  channel's text chat → Hermes home channel fallback) with TL;DR, decisions,
  open questions, action items **grouped by person with mentions**, and
  per-task buttons.
- **Buttons** (persistent `discord.ui.DynamicItem`, survive restarts):
  ✅ Kanban (owners only, for owner tasks) · 🟣 Linear (when Linear active) ·
  ❌ Dismiss · 📁 Project (select menu of candidates) · bulk "Approve all".
- **kanban** (`kanban.mode`: `approve` default | `auto` | `off`): creates
  tasks for OWNER items via `hermes_cli.kanban_db.create_task(triage=True,
  idempotency_key=..., project_id=resolved)`; body links meeting folder +
  quote + timestamp. Owner = `owners` config (default: Discord home channel
  user_id + DISCORD_ALLOWED_USERS first entry).
- **linear** (`linear.mode`: `approve` default | `auto` | `off`; active only if
  connected): backend A = GraphQL API with `LINEAR_API_KEY` (secret scope);
  backend B = Hermes MCP server named `linear` via `ctx.call_mcp` if
  allowlisted. Issue per action item: team/project from resolution, assignee
  from person mapping (learned links → email/name fuzzy match of Linear users
  vs Discord display name), description with quote + meeting ref. Not
  connected → silently skipped (doctor reports it).
- **obsidian** (`obsidian.vault_path`, off unless set): copies notes.md into
  `<vault>/<obsidian.folder>/`.

## 9. Pipeline & storage

`<HERMES_HOME>/plugin-data/meeting-scribe/`
```
index.sqlite              meetings, speakers, utterances FTS5, jobs, deliveries,
                          action_items, links (discord↔linear), channel_projects
meetings/YYYY/MM/<YYYY-MM-DD_HHMM>_<slug>_<shortid>/
  meta.json  transcript.jsonl  transcript.md  notes.md  notes.json  tasks.json
  recording.mka | recording.ogg      (per retention)
  tracks/<user_id>.ogg               (temporary)
```
States: `recording → captured → transcribing → transcribed → analyzing →
analyzed → delivering → done`; `failed` keeps `failed_stage` + error + attempts
(retry with backoff, max 3, then manual `reprocess`). On factory connect, jobs
in non-terminal states resume; `recording` rows with no live session become
`captured` (partial=true).

## 10. Configuration (`plugins.entries.meeting-scribe.settings`)

Declared in `plugin.yaml` `config_schema` (Desktop form for free). Wizard:
`hermes meeting-scribe setup` (language, whisper model with speed estimate,
notes channel, owners, autojoin, retention, kanban/linear modes, obsidian).
`hermes meeting-scribe doctor`: compat probe, ffmpeg/ffprobe + libopus,
faster-whisper import + model cache, disk space, Discord intents/permissions
hints, LLM reachability, Kanban, Linear connectivity, Obsidian path.

## 11. Agent surface

Tools (toolset `meeting_scribe`): `meeting_search(query, limit)`,
`meeting_get(meeting_id, part)` so the agent can answer "¿qué decidimos del
SMTP?". Plugin skill `meeting-scribe:meeting-scribe` explains usage.

## 12. Known limits (documented)

- Discord allows one voice connection per guild per bot.
- First ~100 ms of a new speaker can be lost until SPEAKING maps its SSRC
  (DAVE needs the mapping).
- CPU transcription of `medium` ≈ 1–3× real time on a 16-core CPU.

## 13. Phase B contract (implemented in Phase A, consumed by capture/UI)

- `meeting_scribe.capture` / `meeting_scribe.discord_ui` may expose
  `install(ctx, runtime)`; `plugin.register` calls it after the core is wired and
  logs (never raises) if it is missing or fails.
- Capture sets `runtime.capture` to a `commands.CaptureController`
  (`start(caller, target) -> str`, `stop(caller) -> str`, `live_meeting_ids() -> set[str]`).
  Until then `/meeting start|stop` answer `capture.unavailable`.
- Recording handoff (`runtime.service()`, a `MeetingService`):
  `begin_recording(meeting)` (state `recording`) → write each speaker to
  `track_path(meeting, user_id)` (`tracks/<user_id>.ogg`) →
  `finish_recording(meeting_id, speakers=..., partial=...)` enqueues the job.
- On gateway connect call `runtime.start_pipeline(capture.live_meeting_ids())`
  (commands also start it lazily via `runtime.ensure_pipeline()`).
- Discord notes sink: satisfy `sinks.base.DiscordNotesSink` and register with
  `runtime.add_sink(sink)`; approvals from buttons call
  `service.approve_item/approve_all/dismiss_item`.
- Doctor: `meeting_scribe.doctor.register_check(name, fn)`, `fn(env) -> Check`.
- Integration tests run under Hermes' interpreter via `scripts/test-integration.sh`
  (pytest in `~/.cache/meeting-scribe/hermes-test-deps`, outside the plugin root
  because `plugins validate` security-scans every file under it).

## 14. Phase B as implemented (deviations and details)

- **Wiring**: `capture.install` builds `CaptureManager` (`runtime.capture`), registers
  the doctor checks and an `on_unload` hook (finalize live recordings as partial).
  `discord_ui.install` owns the single `register_platform_handler("discord", …)`
  factory: on each connect it stores a weak adapter ref + the gateway loop, calls
  `capture.attach(bot, adapter)` (runs the compat probe against the adapter module
  actually loaded), moves the `on_voice_state_update` listener to the new bot,
  `add_dynamic_items`, and `runtime.start_pipeline(live_meeting_ids)`.
- **Compat probe** result gates capture: incompatible → `/meeting start` answers
  `capture.incompatible` with the problems; `doctor` reports `discord_compat: fail`.
- **Command dispatch**: `/meeting start|stop` handlers run on Hermes' executor; the
  controller runs the coroutine on the adapter loop with `run_coroutine_threadsafe`
  and waits (start 60 s, stop 90 s). If ever called on the loop thread it schedules
  the coroutine and answers "starting…/stopping…".
- **Auto-leave** is polled by the session each drain tick (no humans for
  `autoleave.grace_seconds`), not driven by the voice-state listener; auto-join uses
  one watcher task per channel re-checking every second (debounce).
  `ctx.on_unload` also cancels pending auto-join watchers.
- **Stop reasons**: `stopped`, `empty`, `max_duration` → complete;
  `disconnected` (lost voice / `/voice leave` / gateway `disconnect()`), `shutdown`,
  `error` → `partial=True`.
- **Notes layout**: header message (title, partial warning, TL;DR, decisions, open
  questions; split at 2000 chars, mentions never cut) in the target channel; the rest
  in a thread started from it when `delivery.discord.thread` and the channel supports
  threads (voice text chats do not → everything stays in the channel). Items grouped
  by owner (`<@id> (name)`, "Unassigned" last), ≤ 5 items per message (one button
  row per item; Discord max 5 rows), then a bulk row. Plain content, no embeds.
- **Buttons** (`custom_id` = `mscribe:<action>:<meeting>:<item>`, actions `ok`
  Kanban, `lin` Linear, `no` dismiss, `allk`/`alll` approve all, `prj` project
  picker; the picker is an ephemeral `psel` select of up to 25 candidates).
  Each install builds its own DynamicItem subclasses (multi-profile safe).
  Kanban buttons render only for owner items; `ok`/`allk` are owner-only; other
  actions: owners or `adapter._component_check_auth` (Hermes allowlists/roles/
  pairing). Without that helper → owners only (fail closed). After an action the
  notes messages are re-rendered in place (status icons, spent buttons removed).
- **Idempotency**: the sink stores a mutable pointer via the new
  `Repository.upsert_delivery` (`sink="discord", key="mtg:<id>:notes"`,
  `external_id` = JSON `{channel, thread, messages[]}`); reprocess edits those
  messages, sends extra parts, deletes surplus ones, re-posts if deleted.
  Adapter not connected yet → deliver fails softly and the job retries.
