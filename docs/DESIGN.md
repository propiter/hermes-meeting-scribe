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
               audio_ffmpeg_path; decode, probe), archive.py (final packaging per
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
(`commands_aliases`, default `["meet", "rec"]`; e.g. a user can add `notes-rec`).
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
`autoleave_grace_seconds` (default 60), OR `limits_max_duration_minutes`
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
- Auto-join (`autojoin_enabled`, default **true**): `on_voice_state_update`
  listener; when a voice channel reaches `autojoin_min_humans` (default 2) for
  `autojoin_grace_seconds` (default 20) and the channel passes
  `autojoin_channels` allowlist / `autojoin_ignore_channels`, and the bot has no
  voice client in that guild → start. Only one recording per guild (Discord
  limit); multiple guilds concurrently.

## 5. Audio retention (one file per meeting)

`audio_retention`:
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
- Model own instance: `transcribe_model` (default `medium`), `device` auto
  (CPU int8 without CUDA), `cpu_threads` default `max(1, cores-2)`, language
  `transcribe_language` (default `auto`; the setup wizard asks and strongly
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
- Notes language: `analysis_language` (default = transcript language).
- **Project resolution** (`analyze/projects.py`): candidates = Hermes projects
  (`projects_db`, per profile) + kanban boards + Linear projects/teams (if
  enabled) + learned `channel_id→project` map; hints = guild/category/channel
  names. LLM chooses only from candidates or null; below
  `projects_min_confidence` (0.6) → unassigned. `/meeting project` and the 📁
  button teach the channel map.

## 8. Delivery (sinks)

All sinks implement `Sink.deliver(ctx, meeting, notes) -> SinkResult` and are
idempotent (keys: `mtg:<meeting_id>:<item_id>`).
- **files** (always): meeting folder artifacts + `notes.md` with YAML
  frontmatter (Obsidian-compatible).
- **discord** (`delivery_discord_enabled`, default true): summary + task index
  in `delivery_discord_channel` (default: the voice channel's text chat →
  Hermes home channel fallback); each task as its own message, with its own
  buttons, in a thread of its project's channel; assignee DMs. See §16 (the
  0.1 "all tasks, then all button rows" layout is gone).
- **Buttons** (persistent `discord.ui.DynamicItem`, survive restarts):
  ✅ Kanban (owners' own tasks) · 🟣 Linear (when Linear active) ·
  ❌ Dismiss · 📁 Move (select of channels) · 📋 My tasks (ephemeral panel).
  Authorization is per task (§16).
- **kanban** (`kanban_mode`: `approve` default | `auto` | `off`): creates
  tasks for OWNER items via `hermes_cli.kanban_db.create_task(triage=True,
  idempotency_key=..., project_id=resolved)`; body links meeting folder +
  quote + timestamp. Owner = `owners` config (default: Discord home channel
  user_id + DISCORD_ALLOWED_USERS first entry).
- **linear** (`linear_mode`: `approve` default | `auto` | `off`; active only if
  connected): backend A = GraphQL API with `LINEAR_API_KEY` (secret scope);
  backend B = Hermes MCP server named `linear` via `ctx.call_mcp` if
  allowlisted. Issue per action item: team/project from resolution, assignee
  from person mapping (learned links → email/name fuzzy match of Linear users
  vs Discord display name), description with quote + meeting ref. Not
  connected → silently skipped (doctor reports it).
- **obsidian** (`obsidian_vault_path`, off unless set): copies notes.md into
  `<vault>/<obsidian_folder>/`.

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
  `autoleave_grace_seconds`), not driven by the voice-state listener; auto-join uses
  one watcher task per channel re-checking every second (debounce).
  `ctx.on_unload` also cancels pending auto-join watchers.
- **Stop reasons**: `stopped`, `empty`, `max_duration` → complete;
  `disconnected` (lost voice / `/voice leave` / gateway `disconnect()`), `shutdown`,
  `error` → `partial=True`.
- **Notes layout**: header message (title, partial warning, TL;DR, decisions, open
  questions; split at 2000 chars, mentions never cut) in the target channel; the rest
  in a thread started from it when `delivery_discord_thread` and the channel supports
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

## 15. Review fixes (two adversarial reviews, 22 findings + notes)

Design changes that came out of the reviews. Each has a regression test named in the commit.

### Capture (`capture/`)
- **Silence is a marker, and the writer queue is bounded (C1).** The aligner queues `("gap", n_bytes)`
  instead of zero buffers. The writer thread expands the gap from one reused 20 ms zero frame, so a
  long silence costs O(1) memory. The loop-side `write()` never blocks: it does `put_nowait` into a
  bounded `queue.Queue`. When that queue is full, chunks go to an ordered overflow deque that the
  writer drains first, which keeps order and never drops audio. Past a hard backlog cap (~60 s of
  audio) the track fails loudly (`error` set, recorded as partial) instead of growing without bound.
  Drop-oldest was rejected because it silently corrupts the timeline. ffmpeg stderr goes to a temp
  file, not a pipe.
- **Lost connection (W1).** The session is only ended when discord.py's connection state is
  disconnected, or when it has not become healthy within `vc.timeout` (the reconnect grace).
  Teardown always calls `disconnect(force=True)`. `busy()` also counts `guild.voice_client`.
- **Autojoin (W2).** After a manual stop or `max_duration`, the channel is on cooldown until humans
  drop below `autojoin_min_humans`. Voice-state events from the bot itself are ignored.
- **Lifecycle (W4-W7).** ffmpeg is resolved before connecting. A per-user writer failure is
  isolated: that speaker is skipped and the meeting continues. `_finished` is set in a `finally`,
  and teardown runs as a shielded task, so cancelling `stop()` cannot abort finalization. The run
  task is created before any post-lock await, and failures (including `BaseException`) tear down.
  The session is registered before `start()` and popped on failure. `live_meeting_ids()` keeps a
  meeting until teardown has *finished*, so `recover()` can never close a starting or stopping
  meeting.
- **Consent (W9).** A leftover `[REC] ` prefix is stripped from the base nickname and restored.
  Consent helpers moved to `capture/consent.py`.
- **Timeline and DAVE (notes).** Timestamps come from a `time.monotonic()` timeline anchored to the
  wall-clock `t0`, so NTP steps cannot shift tracks. `dave_session` is re-read on every drain tick.
  While DAVE is active, frames decoded before their SSRC is mapped are dropped, because their
  decryption is not trustworthy.
- **Hermes TTS (note).** Hermes plays voice replies only through `play_in_voice_channel` for a guild
  that has a `_voice_text_channels` entry. We register the client in `_voice_clients` without
  adding that mapping, and the recording client's `play()` is also stubbed to refuse, so no reply
  audio can reach the recording.

### Discord UI (`discord_ui/`)
- **Reload safety (W3).** Each install gets a unique factory qualname. `ctx.on_unload` removes the
  listener, calls `remove_dynamic_items`, closes the runtime and drops the `RUNTIMES` entry. All bot
  work is marshalled to the loop (`call_soon_threadsafe` / `run_coroutine_threadsafe`).
- **Notes pointer (W8).** The notes pointer is persisted after every message sent, and edits/sends
  are retried. A retry after a partial post edits instead of duplicating.
- **Replies (S1/S2).** The project picker defers before the catalog lookup and answers through a
  followup. Replies are truncated to 1900 characters.

### Core
- **Per-sink decisions (1).** A new `item_sinks(meeting_id, item_id, sink, status)` table. Approve mode
  delivers only items approved for that sink. `action_items.status` is now just a display summary.
  Schema v2 back-fills existing Kanban/Linear deliveries as `delivered`.
- **Leases and ownership (2).** `jobs.owner` and `jobs.heartbeat` form a lease, refreshed every 30 s
  by a helper thread. `recover()` requeues only leases older than 180 s and skips leased meetings.
  `meetings.capture_owner` records which process is capturing. Orphan recordings are closed only
  when the caller owns capture (the Discord-connected gateway) and that owner process is gone
  (checked with a same-host `kill(pid, 0)`). `ensure_pipeline()` is a no-op outside the gateway
  (`_HERMES_GATEWAY=1`), so CLI/TUI commands never start a worker or run recovery.
- **Unicode (3/4).** `domain/text.fold`: NFKC, casefold, strip combining marks, keep every letter.
  The hallucination filter, ids and name matching use it. A repetition only counts as a
  hallucination at low confidence. Covered by tests in es/ru/zh/ja/ar.
- **Routing (5).** The meeting stores the resolved `project_key`. Sinks see only candidates of their
  sources: Linear gets `linear`; Kanban gets `hermes` and `kanban`. A real catalog candidate replaces
  a learned stub.
- **Claim-first delivery (6).** `deliveries.state` is `pending` or `done`. A claim is an
  `INSERT … ON CONFLICT DO NOTHING`. Losers get `DeliveryInProgress`. A failed or abandoned claim
  stays pending, and the next claimer reconciles before creating: Linear looks up the issue by the
  `` `mtg:…` `` marker in its description (GraphQL `issues(filter: {description: {contains}})`).
  Kanban relies on its server-side idempotency key.
- **Profiles (7).** Runtime keeps a repository and service per database path and never closes one
  that another caller may still hold. `/meeting` resolves the service on every call.
- **LLM schema (8).** Only `title` is required. Numbers are nullable and additional properties are
  allowed. When Hermes rejects the output against the schema, the adapter retries in `json_mode`
  without a schema, and the normaliser absorbs the variants.
- **Timeout (9).** The worker timeout scales with the *sum* of track durations.
- **Flat settings (10).** Keys are flat (`kanban_mode`, …), because Hermes' Desktop form reads
  `settings[key]` flat while dotted keys are stored nested. Old nested values are still read as a
  fallback, and the CLI accepts the dotted spelling. `plugin.yaml` is regenerated by
  `scripts/gen_manifest.py`.
- **Pagination (11).** Linear users, projects and teams follow `pageInfo`, capped at 40 pages of 250.


## 16. Task delivery redesign (0.2.0)

Live feedback from the first real Discord test on the author's server (capture, transcription and the
summary worked) found three problems with the 0.1 notes message: the buttons were not under their
task (all tasks, then all button rows, and the rows shifted as soon as one task was handled); the
project was per MEETING although one meeting covered several; and anyone Hermes authorized could
press Linear/Dismiss/Project on anybody's task. The plugin is public and runs on arbitrary servers,
so nothing below depends on any server's channel names.

### Layout
- **Meeting chat** (`delivery_discord_channel` → voice text chat → home): the summary parts
  (`notes` pointer) and, last, the **task index** (`index` pointer): counts per project with a link to
  the thread holding them (⚠️ when a group has uncertain tasks, ⛔ when a channel lacked permissions),
  counts per person, closed DMs, and ONE `📋 My tasks` button (`mscribe:mine:<meeting>:all`).
- **Project channel**: one anchor message + a thread per meeting (`thread:<channel>` pointer;
  in the channel itself when `delivery_project_threads` is off or the thread cannot be created), then
  ONE message per task (`task:<item>` pointer with the routed `target`) with that task's buttons
  directly under it. A handled task shows ✅ Kanban `t_x` / 🟣 Linear ENG-1 / ❌ Dismissed and has no
  buttons, so it can never shift another task's buttons. Tasks without a channel go to a thread under
  the summary (or the meeting chat itself).
- **Ephemeral panel** (`mine`, `pg:<scope><page>`): components v2 `LayoutView`: header, then per task a
  `TextDisplay` followed by ITS `ActionRow`. 4 tasks + 1 nav row per page (Discord: 5 rows per classic
  message, 40 components and 4000 display characters per v2 view). Owners get a 👥 switch to all tasks
  (scope `a`); non-owners asking for `a` get their own tasks.
- **DMs** (`delivery_dm_assignees`, default true): the same panel for each assignee (`dm:<user>`
  pointer), refreshed in place after actions. A closed DM (50007) or unknown user is logged, listed in
  the index, and never fails the delivery.

### Per-task projects
- The extraction schema has `project`, `project_key`, `project_confidence` per action item (lenient:
  missing or unknown values are fine). A name that is not a candidate survives as `project_hint`
  (the raw spoken name) so channel routing can still fuzzy-match it.
- Candidates gain a `discord` source: the meeting guild's text channels and categories
  (`DiscordChannelCatalog`, snapshotted on the gateway loop).
- **Name cleanup is generic** (`domain/names.clean_channel_name`): strip by Unicode category only
  (So/Sk/Cs, variation selectors, ZWJ, keycaps; box drawing and separators such as `┃ │ ・ | •`;
  Ps/Pe/Pi/Pf brackets such as `『』【】「」[]()`), trim leftover separators, keep the letters and case.
  Decorative leading WORDS are server-specific and therefore config: `channel_name_ignore_prefixes`
  (default empty). No prefix is hard-coded.
- **Matching** (`similarity`, `match_name`): both sides NFKC + casefold + accents stripped for
  comparison only, split on `-_`, spaces and punctuation. Score = max(full-string ratio, best token
  ratio, containment when the spoken term equals a whole channel token or token sequence). A
  single-token hit needs a token of ≥ 4 characters (short/common words like `app` never match alone);
  exact multi-token sequences always count. `project_match_min_score` (0.8) is the threshold; a
  runner-up within a small margin makes the match **uncertain**. difflib only, no new dependency.
- **Channel precedence** (`discord_ui/routing.route_item`): LLM picked a `discord:<id>` candidate →
  `project_channels` config (`Name=channel_id`) → learned map (`project_channels` table, schema v3) →
  best fuzzy channel/category (a category resolves to its first postable text channel) → the meeting's
  project, flagged → meeting chat. Weak (≥ min − 0.2) or ambiguous matches are still posted in the
  most probable channel and flagged "⚠️ project not certain — confirm with 📁".
- **Permissions**: posting needs View Channel + Send Messages (+ Create Public Threads when threads are
  on). A matched channel without them routes to the meeting chat and the index names it.
- **📁 Move** (`prj:<item>` → select `tsel:<item>` of postable channels, most likely first): the item
  gets `project_key=discord:<id>` (confidence 1.0), notes.json is rewritten, the old name, the hint and
  the channel name are learned, and the task is re-posted in the new channel's thread while the old
  message is deleted (pointer updated).

### Authorization (per task, `discord_ui/auth.py`)
- The task's **assignee** and the **owners** may act on it; anyone else gets an ephemeral "This task
  belongs to @X" and nothing happens, even users Hermes authorizes.
- Unassigned tasks: owners only. Unknown/stale item ids: fail closed.
- ✅ Kanban: owners only, and only on tasks assigned to an owner (the owner's personal board);
  everyone else uses Linear, assigned to them.
- `mine`/`pg` are open: the panel only ever contains the clicker's tasks (or all, for owners).
- 0.1 meeting-wide buttons still work on old messages with their 0.1 rule (`allk` owners;
  `alll`/`psel`/`prj:all` owners or Hermes-authorized users).

### Idempotency and refresh
- Every posted message has a `deliveries` pointer (upsert) saved right after it exists; reprocess edits
  in place, re-posts only what was deleted or moved, and deletes messages of tasks that disappeared.
- After a button action only that task's message, its assignee's DM panel and the index counts are
  edited; a click from an ephemeral panel re-renders the panel too. The per-(item, sink) status, claims
  and leases of §15 are unchanged.
- custom_ids stay `mscribe:<action>:<meeting>:<item>` (< 100 chars) and are routed by DynamicItem
  templates, so every button keeps working across restarts.

### Review fixes (fresh-context review before release)
- **Moves are per task; only owners teach routing.** An assignee's 📁 pins THEIR task (new
  `item_overrides` table, schema v3) and does not change the shared project → channel map, so it
  cannot re-route other people's tasks. An owner's move also learns the names.
- **Overrides survive re-analysis.** Items are read joined with `item_overrides`; a re-analysis that
  picks the old candidate again cannot move the task back.
- **Panel clicks defer as an update** (`response.defer()`), so `edit_original_response` edits the
  clicked panel; clicks on public task messages defer ephemerally with "thinking".
- **Only a confirmed missing message is re-posted** (404 / codes 10003/10008). Transient failures
  propagate and the job retries, so nothing is duplicated.
- **Delete fails → disarm.** A message that cannot be deleted (no Manage Messages) is edited to a
  "moved to #x" notice without buttons.
- **One publication per meeting at a time** (per-sink `asyncio.Lock`): delivery, refresh, refresh_item
  and move cannot race each other into duplicate anchors, threads or DMs.
- **DM cleanup.** An assignee who lost every task gets their DM panel edited to an empty panel.
- **0.1 pointers** (no `"v": 2`): the old task rows (in the old notes thread) are deleted or disarmed
  once; the header is edited in place.
- **Short names match only exactly.** A single spoken token under 4 characters scores 0 unless it
  equals the channel name, in every scoring path, including the weak-guess floor.

### Needs a live check
Components v2 in ephemeral follow-ups and DMs, thread creation from an anchor message in channels with
slow mode or restricted thread permissions, and DM delivery rate on large meetings.

## 17. Google Meet import and transcript attachment (unreleased)

Implemented; **not yet exercised against the live Google API** (no credentials in CI). Every field,
filter and state below was checked against the official reference
(<https://developers.google.com/workspace/meet/api/reference/rest/v2>, the *Work with artifacts*
guide and <https://developers.google.com/identity/protocols/oauth2/native-app>).

### 17.1 Architecture

```
google/http.py       stdlib urllib transport seam (HTTP errors returned, never raised; https only)
google/oauth.py      client JSON import, PKCE + loopback receiver, exchange/refresh/revoke, 0600 files
google/meet_api.py   conferenceRecords / transcripts / entries / participants / spaces, pagination
google/convert.py    entries → Utterance, participants → Speaker, language, title (pure)
google/importer.py   MeetImporter.sync (one pass) + MeetPoller (thread + SQLite lease)
cli_google.py        google connect | status | sync | disconnect
discord_ui/transcript_file.py   full transcript attachment (all sources)
```

`Runtime.start_pipeline` (gateway only: Discord connect or `ensure_pipeline`) starts the poller next
to the worker; `stop_pipeline`/`close` (unload, reload, profile switch) stops and joins it and
releases the lease. The poller never runs on the Discord asyncio loop.

### 17.2 Decisions

- **One scope**, `meetings.space.readonly` (sensitive, not restricted). No Drive (restricted), no
  userinfo — so `status` can only say "connected". Each user brings their own "Desktop app" client;
  consent screen *Internal* avoids Google verification in Workspace.
- **No new dependency**: urllib, http.server, secrets, hashlib, base64, json.
- **Files**: `<plugin data>/google/{client,token}.json`, created 0600 atomically (dir 0700), under
  `plugin_data_dir` of the active profile (never a hard-coded home). `connected_at` lives in the
  token. `invalid_grant`/`invalid_client` mark the token `disconnected` (no refresh storm; the
  poller backs off to hourly) until `connect` runs again.
- **Errors**: 401 → forced refresh + one retry; 403 → "forbidden" status (API disabled, scope, admin
  policy); 429/5xx/network → "temporary", next cycle. Messages carry Google's error *status* and
  message, never tokens.
- **Readiness**: only transcripts in `FILE_GENERATED`. `ENDED` means the file is not generated yet
  and entries may be incomplete; the record is simply retried on the next poll. A record with no
  transcript (transcription off) or no entries is counted and skipped, never an error.
- **Idempotency**: schema v4 adds `meetings.source` / `meetings.external_id` with a UNIQUE index;
  `Repository.create_imported_meeting` inserts row + speakers + utterances in one `BEGIN IMMEDIATE`
  transaction and returns False on conflict. Two pollers, a manual `google sync` and a restart can
  race freely: exactly one insert wins, losers delete the files they wrote.
- **Single poller**: `leases` table (`google-meet-poll`, owner = the runner's process owner id,
  TTL = 3 × interval), renewed every tick; released on stop.
- **Window**: automatic polls import only conferences whose `end_time >= connected_at` (never floods
  Discord with history); `google sync --since/--days` backfills explicitly; always clamped to the
  30-day retention.
- **Entry into the pipeline**: `MeetingService.import_transcript` writes `transcript.jsonl/.md` with
  the existing writers, persists the meeting in `transcribed` and enqueues ANALYZE. TRANSCRIBE is
  never run; ARCHIVE finds no `tracks/` and returns None (unchanged `make_archiver`).
- **Meeting fields**: `guild_id=""`, `channel_id="gmeet:<space id>"` (keeps learned
  channel→project rows separate per Meet space), `channel_name` = meeting code or "Google Meet",
  title `Google Meet · <UTC date time> · <meeting code>` (the LLM title replaces it after analysis;
  Meet has no event title without Calendar scope). `language` = majority `languageCode`, shortened
  to ISO 639-1.
- **Utterances**: one per entry (no merging: the pipeline's only merge utility works on whisper
  segments); `t0/t1` relative to the conference `startTime`; `speaker_id = gmeet:<participant id>`;
  `speaker` = signed-in/anonymous/phone `displayName` (else "Participant N"); `confidence = 1.0`.
- **Imported speakers in Discord**: `is_discord_user_id` (digits only) guards mentions, DMs and the
  "belongs to" message; imported names are shown in bold instead. Owners are Discord ids, so Meet
  tasks are never owner tasks (no Kanban auto-approve for them).
- **Notes channel**: Meet meetings use `google_meet_discord_channel` → `delivery_discord_channel` →
  home channel; non-numeric ids are ignored. With none, the Discord sink returns *ok + skipped*
  (the job does not loop). The guild for project channels is taken from the notes channel.

### 17.3 Transcript attachment (all sources)

`delivery_discord_transcript` (default true): after the summary, in the meeting chat (not the
thread: it is part of the notes, and voice-chat channels often cannot host threads). One file
`transcript-<YYYY-MM-DD>-<slug>.md`, rendered from `transcript.jsonl` with the current title.
Pointer `transcript` in `deliveries` stores sha256 + message ids: same hash → nothing is posted;
different hash (reprocess) → old messages deleted and the new file posted. Above 8 MB (conservative
Discord limit) the text is split at line ends into `…-partNofM.md` (UTF-8 safe); more than 20 parts
→ a notice pointing to `export`. Missing Attach Files (403/50013/50001) → one notice, remembered.
Transient failures keep already-posted parts and retry on the next publish. None of this can fail
the delivery.

### 17.4 Risks / needs a live check

- Real Google behaviour: time between `ENDED` and `FILE_GENERATED`, whether `participants.list`
  includes everyone who spoke, pagination sizes, and error bodies for admin-blocked tenants.
- Meetings owned by another organisation: the API may return 403/404 or omit them.
- The transcript attachment makes transcripts visible to everyone in the notes channel; it is on by
  default by design (documented in README and the catalog disclosure).
- Discord's per-file limit can be lower on some servers than the 8 MB we assume only if Discord
  changes it again; the split size is a single constant (`transcript_file.MAX_BYTES`).
