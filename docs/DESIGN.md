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
4. **Owned by one profile**: one installation has one OWNER profile, which runs capture, the worker
   and the pollers and holds the data, settings and secrets. Other profiles may turn the plugin on
   only to serve the Desktop page with the owner's data. `meeting_scribe.home` is the only resolver
   (see §1.5). Nothing follows a request's profile scope. Secrets via
   `agent.secret_scope.get_secret`; threads via `agent.memory_provider.spawn_context_thread`.
5. **Local-first**: audio never leaves the machine. Only transcript text goes to
   the user's own configured LLM.
6. **Strict TDD**: tests first, fakes for Discord/RTP/LLM/Linear.

### 1.5 One installation, one owner profile, any Desktop profile

**Problem.** Hermes Desktop runs ONE backend (`hermes serve`) launched with the profile Desktop was
opened with, and scopes plugin REST calls to the page's active profile with `?profile=<name>`.
Hermes serves a plugin's API only when that LAUNCH profile has it in `plugins.enabled`
(`hermes_cli/web_server.py::_plugin_api_runtime_gate`, `web_server_dashboard.py::_mount_plugin_api_routes`),
otherwise every call is `404 Plugin not found`. The page therefore worked only when Desktop was opened
with the one profile that had the plugin.

**What the host allows (hermes-agent `6e69a8933`, read-only):**

- The Desktop backend finds dashboard halves in `<launch home>/plugins` and ALSO `<root>/plugins`
  (`web_server_dashboard.py::_dashboard_plugin_search_dirs`); the gate still needs `plugins.enabled`
  of the launch profile.
- A gateway's agent loader scans ONLY `<its home>/plugins`
  (`plugins_discovery.py::collect_directory_manifests`, `user_dir = get_hermes_home() / "plugins"`), and
  so do `hermes plugins enable/update/remove` (`plugins_cmd.py::_plugins_dir`, `_discover_all_plugins`).
  A copy only in the root is invisible to a named profile's gateway.
- Hermes keeps ONE dependency environment for every live profile (`pm/plugins_state.py::dependency_homes`,
  `enabled_plugins_ordered`). Each enabled plugin directory becomes a uv workspace member keyed by its
  RESOLVED path (`pm/workspace.py::member_sources`, `_member_key`). Two real copies are two members;
  this project has a build backend, so both keep the package name `hermes-meeting-scribe` and `uv lock`
  refuses them ("Two workspace members are both named…", comment in `_workspace_member`). A symlink to
  the copy resolves to the same identity: one member however many profiles enable it.
- Every enabled profile's gateway (its own systemd unit) and every Desktop backend calls `register()`.
  Module names are per home (`plugins_loader.py::_directory_module_name`), so each gets its own
  runtime unless the plugin refuses.

**Decision.**

- ONE real copy. Profiles that need the plugin but do not hold the copy get a SYMLINK
  `<home>/plugins/meeting-scribe → <copy>` and `hermes -p <profile> plugins enable meeting-scribe`.
  Both layouts are supported and covered by `tests/integration/test_any_profile_hermes.py`: the copy in
  the owner profile (what `hermes plugins install` produces; recommended in the README) or in the root.
- ONE owner, resolved by `meeting_scribe.home.owner()`:
  1. `plugins.entries.meeting-scribe.owner_profile` in the ROOT `config.yaml` (the default profile's).
     The root is the one file every profile can reach, the same place Hermes itself looks for shared
     plugins. A per-profile key could disagree between profiles; the root one cannot.
  2. Otherwise the home holding the REAL files (`plugin_root().resolve()`, symlinks followed), so a
     single-profile install needs no configuration and a linked profile is never an owner by accident.
  A checkout outside `plugins/` (tests) uses the process home. "The profile with the Discord adapter"
  was rejected as the rule: several profiles may have Discord (with different bots), and it would tie
  Google Meet-only installs to Discord.
- A declaration that cannot be used (not a profile id, missing profile, unreadable root config) is an
  `OwnerError`: no profile is the owner (nothing records), the REST API answers `503` with the reason,
  and doctor says it. Never a fallback to another profile's data.
- `register()` asks `home.role(get_hermes_home())` (Hermes binds discovery to the profile's home,
  `plugins_loader.py::_plugin_home_scope`):
  - owner: everything as before (runtime, tools, chat commands, skill, Phase B capture, worker, Meet
    pollers). The runtime keeps the role sentence for doctor's first check, `owner`.
  - any other profile: no runtime, no worker, no capture, no tools, chat commands or skill (their
    reads would bootstrap a database in the wrong home). Only `hermes meeting-scribe` stays, and it
    prints which profile to use (`hermes -p <owner> meeting-scribe …`); `doctor` exits 0, any other
    subcommand 2.
- Every process resolves data through `home.data_dir()` → `<owner home>/plugin-data/meeting-scribe`.
  The REST API enters Hermes' own request scope for the owner on every request (`owner_scope()` →
  `web_server_profiles._config_profile_scope`), so settings and secrets are the owner's; `?profile=`
  is ignored. Commands from the page are rows in the owner's `desktop_commands`, executed by the
  owner's worker (§21).
- The root config is parsed once per change (mtime+size cache): the worker's pulse asks often.

**Operational rules (README «Use Meetings from any profile»).** Update the copy through its owner
only (`hermes -p <owner> plugins update meeting-scribe`); links follow. Put the new code in place
BEFORE linking or enabling other profiles: `hermes plugins enable` reloads that profile's running
gateway immediately (`plugins_activation.activate_plugin_now`), and a release without this section
would start a second runtime there. Entering the owner's scope turns a Desktop backend launched
with another profile into a multi-profile host (`tui_gateway.launch_profile_policy.activate_multi_profile_hosting`),
the same switch Hermes flips for any `?profile=` request.

**Still needs a live check.** The four-profile setup in a real Desktop (open it with each profile),
and a gateway reload of a linked profile while the owner records.

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
| `status` | recording / processing state, queue. The first line names the meetings in state `recording` read from the database (never the calling process: the CLI captures nothing), live when their `capture_owner` is this process or a live one; a dead owner is flagged as an orphan the gateway closes at start. The operator reads it before restarting |
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

### 4.1 Voices without SPEAKING, and audio that never arrived

Discord maps an RTP SSRC to a user only through SPEAKING (voice op 5). In
production it often sends none for people **already in the call when the bot
connects** (they re-join → SPEAKING within seconds; people who join after the
bot always get one). Hermes' `_on_packet` then skips DAVE for that SSRC (no
user id) and feeds the still end-to-end-encrypted payload to Opus: noise or
decode errors, and the voice is lost silently. The plugin fixes this without
touching Hermes:

- **Retain, don't decode.** `_Decoders` hands Hermes a `_Retainer` for an
  unmapped SSRC: it keeps the payload after the transport layer (NaCl) with its
  arrival time, **from the first packet and for the whole meeting if needed**,
  so a voice identified late gets its complete track, not only what follows
  the mapping. Caps: `RETAIN_SECONDS` 4 h (the default
  `limits_max_duration_minutes`) and `RETAIN_SSRC_MAX_BYTES` 256 MiB per SSRC,
  `RETAIN_MAX_BYTES` 1 GiB overall; each frame is counted as its bytes plus
  `FRAME_OVERHEAD` (120 B of Python objects). Encrypted Opus is ~6-8 KB/s per
  speaker (≈100 MB in 4 h with overhead), so a normal meeting never hits them;
  past a cap the oldest frame goes first and is counted.
- **Candidates.** People in the voice channel (voice states, every tick),
  CLIENTS_CONNECT (op 11: the `user_ids` already in the call, sent during the
  voice handshake) and CLIENT_CONNECT/DISCONNECT (ops 12/13), plus
  `DaveSession.get_user_ids()` (the MLS group), minus the bot and users already
  mapped. Hermes hooks the websocket only after `connect()` returns — too late
  for op 11 — so the scribe connects with `channel.connect(cls=ScribeVoiceClient)`,
  a `discord.VoiceClient` subclass whose `create_connection_state()` passes a
  hook to `VoiceConnectionState` from the start and keeps ops 11/12/13 (a
  bounded backlog replayed when the receiver starts, then a listener). These
  ops carry user ids, never SSRCs.
- **With DAVE: proof by key.** davey exposes `decrypt(user_id, MediaType.audio,
  frame)`, `get_user_ids()`, `can_passthrough()`; no SSRC→user mapping. Each
  user's decryptor only opens frames sealed with that user's key, so a frame
  (magic `0xFAFA`) is tried with every candidate; the SSRC belongs to the
  **only** key that opens *and* Opus-decodes `CONFIRM_PACKETS` (3) frames. Two
  keys opening it (never expected) = ambiguous → not attributed, WARNING.
  libdave rejects a repeated nonce, so the Opus opened while testing is cached
  and claim + map + replay happen atomically under the receiver lock.
- **Retries.** Retained DAVE frames are tried again with every key not yet
  tried as soon as the candidate set grows (a voice-state join, op 11/12) and,
  with every unproven candidate, every `RETRY_SECONDS` (5 s) — a key can start
  working without a new candidate (the MLS commit adding its decryptor lands
  late). Only the newest `RETRY_SCAN_FRAMES` (250, 5 s of audio) are scanned — any
  frame of the SSRC proves its owner, `CONFIRM_PACKETS` suffice, and the loop
  stays cheap; the replay opens the rest. Voice states of the channel
  alone are enough candidates when op 11 never arrives.
- **Without DAVE (or DAVE passthrough): no proof, so never a person's track.**
  Plain Opus carries nothing that says whose it is. Without SPEAKING it is
  never written to a person's own track: after `UNIDENTIFIED_AFTER` (10 s), or
  at the close, it gets its own track `unidentified-N`, speaker "Unidentified
  participant" (transcribed). **One `unidentified-N` = one SSRC = one Discord
  voice connection = one person.**
- **Inferred owner.** Its owner is inferred as metadata (`VoiceReport.inferred`,
  recomputed on every drain) when exactly one person can own it: everyone in
  the call while it sent audio (voice states every tick, ops 11/12; bots
  included, so another bot blocks it), minus people whose SSRC is already known
  (mapped, proven or inferred), people absent at two consecutive presence
  snapshots while it talked, and people whose join (a voice state, a later op
  11, op 12) was seen more than `JOIN_LAG` (2 s) after its first packet — a
  connection that did not exist yet sent nothing. Two SSRCs that could both be
  the same only person: neither is named. **Mute flags are no evidence**: the
  voice-state cache lags (see below). At the close the session gives an
  inferred (or late-SPEAKING) track to its owner: `tracks/unidentified-N.ogg`
  becomes `tracks/<user id>.ogg` and the label speaker disappears; if they
  already have a track the label keeps its audio, named after them.
- **Replay.** Once an owner is PROVEN (key or a late SPEAKING — always
  authoritative), the whole retained audio is decoded and
  written at its original arrival times, `REPLAY_BATCH` (1500 frames = 30 s)
  per drain so a long backlog never stalls the loop; live packets queue behind
  it and the SSRC is handed to Hermes' live path only when the backlog is
  empty, so the track is in time order. The final drain replays everything.
  A SPEAKING for an SSRC already streaming as `unidentified-N` names that
  track; one that contradicts an inference drops it with a WARNING
  (`contradicted`) — the audio never was in the inferred person's track.
  Re-joins get a new SSRC: a mapping older than the user's last join is not
  reused.
- **Log.** Labelling an SSRC logs a WARNING with how many of its frames were
  DAVE, whether a DAVE session was active and ready, the candidates left and
  why it was not decided; the close logs every label with its owner.
- **Why (a real meeting).** The bot joined a call with A and B (op 11 listed
  both). A's SSRC sent audio with no SPEAKING; B's SPEAKING came 2.7 s later.
  The voice-state cache still flagged A muted, so the old rule "the sole
  *unmuted* person without SSRC" found nobody and A's 20 minutes became
  `unidentified-1` with no owner. A minute later C joined flagged muted and her
  client sent a little audio on a new SSRC 3 s after the join; A's flag had
  caught up, so A was now the "sole unmuted person without SSRC" and C's
  packets went to A's track until C's SPEAKING arrived 8 s later. Both came
  from treating mute flags as evidence and from a guess that did not count
  A's own unnamed SSRC nor C's fresh join. `tests/unit/capture/test_voice_identity.py`
  replays that sequence (receiver and session).
- **Assigning after the meeting.** Any `unidentified-N` left can be given to a
  participant later: `hermes meeting-scribe speaker list <meeting>` (interval
  and lines of each track) and `speaker assign <meeting> unidentified-N
  <name|id|@id>`; Desktop (Summary and Transcript: "Assign to…", a queued
  `assign_speaker` command run by the gateway); Discord, a "Who is
  <Unidentified participant>?" button on the notes' first message (a
  participant of the meeting or an owner; private meetings only in their
  channel, direct-message meetings only in the clicker's own copy). Assigning
  renames the lines (`transcript.jsonl`, `transcript.md`, the index), the tasks
  the track owned, the speaker list and `missing_audio`, keeps the mapping
  (`speakers.assigned.<meeting>`, applied again by `reprocess
  from=transcribe`), and re-delivers a published meeting: every message is
  edited in place, nothing is duplicated and an edit pings nobody. Assigning
  the same track to the same person again changes nothing; to someone else, it
  is refused.
- **Missing audio.** Each tick adds elapsed time to every unmuted human in the
  channel (here the flag only estimates who could have spoken). At the end,
  anyone unmuted there > 60 s whose voice reached no track — nor an
  `unidentified-N` given to them —
  is stored on the meeting (`missing_audio`) and logged as a WARNING with the
  SSRCs never identified; the stop announcement, the Discord notes and
  `notes.md` say "Could not capture the audio of: X, Y" / "No se pudo capturar
  el audio de: X, Y" (plain text, names inert), Desktop shows it in Summary and
  Processing, `status` lists it, and `doctor` (`missing_audio`) warns about the
  last 20 meetings.
- **Nobody heard, people present.** A recording where no voice reached a track
  ends `empty` without processing; if `missing_audio` names people, that is
  its visible cause, never a silent discard: `status` (chat and CLI) adds the
  names to its line, Desktop shows "No audio: voices not captured" and why, and
  `doctor` warns. The Discord sink (session-end listener) posts one notice,
  pointer `unheard`, where the notes would have gone ("could not capture the
  audio of X, Y… no transcript or notes"), following the meeting's rules: a
  private meeting only in its private channel, a `:dm` meeting nowhere (no
  channel is ever used for it), a held destination nowhere; skipped when that
  channel is the voice chat and the stop announcement already said it there.
- **Task owners.** Every person in the channel is a speaker of the meeting,
  heard or not, with their Discord nickname, global name and username as
  `aliases`; the analysis resolves owners against all of them (§7).
- **Compat probe** also checks `_on_packet`'s decoder seam
  (`ssrc not in self._decoders`, `self._decoders[ssrc].decode(`), davey's
  `decrypt`/`get_user_ids`/`MediaType.audio`,
  `VoiceClient.create_connection_state` and `VoiceConnectionState(…, hook=)`.
- Not verifiable offline: whether Discord sends op 11 to a bot on every
  connect (discord.py declares it; our integration test drives discord.py's
  real websocket with it), and davey's real per-user key rejection (simulated
  by the fakes; the error strings come from the installed library).

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
- **Owners.** The prompt carries `<participants>`: every human of the meeting
  (heard or not; Discord nickname, global name and username as aliases) with
  its id, a closed list the LLM must pick `owner_speaker_id` from (or null).
  An unknown id falls back to `owner_name`, resolved by
  `domain.names.match_person` (accents, emoji and case ignored), in tiers
  that must each give ONE person: the exact name; a whole word of it (given
  name, surname); a phonetic/edit match ≥ 0.85 for Spanish/English spellings
  (`h` silent, `ll`/`y`/`i`, `j`/`y`, `c`/`k`/`qu`, `v`/`b`, `z`/`s`,
  `ph`/`f`, doubled letters: `Yoana`~`Johanna`, `Cristofer`~`Christopher`). Gendered pairs
  (`Luis`/`Luisa`) never match; two candidates in a tier leave the task
  unassigned with the name said.
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
  an automatic channel of the server, never a DM, §19); each task as its own message, with its own
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

`empty` (terminal, not an error): nobody's voice was captured. The capture decides it when it
closes (no frame from any person ever reached a track writer: the row goes straight from
`recording` to `empty`, no job is queued); the pipeline decides it too (defence in depth) when the
folder has no tracks, the transcription yields no utterances or `analyze` finds an empty
transcript. The job ends `done` with no error and no attempt used; nothing is analyzed or
published. The folder and `meta.json` stay (the row points at them); `tracks/`, `.work/` and any
utterances are removed whatever `audio.retention` says. `reprocess` of an `empty` meeting is
refused. Schema v7 reclassifies rows an older version parked as `failed` with
`TranscriptionError: no audio tracks in …` at the transcribe stage (idempotent, data only).

`meetings.source` / `external_id` are written once, at insert, and are authoritative: every read
takes them from the columns (not the `data` JSON), `save_meeting` never updates them, and the JSON
and `meta.json` are kept coherent with them. Schema v8 repairs JSON an older (pre-v4) gateway
rewrote without those fields.

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
- **Meeting chat** (`delivery_discord_channel` → voice text chat → automatic channel, §19): the summary parts
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

`Runtime.start_pipeline` (gateway only) starts the poller next to the worker. `register` calls
`ensure_pipeline` itself in the gateway, so neither depends on Discord connecting or a /meeting
command; the Discord connect later only adds capture ownership to the running worker (one extra
`recover(owns_capture=True)` to close orphan recordings), never a second thread; `stop_pipeline`/`close` (unload, reload, profile switch) stops and joins it and
releases the lease. The poller never runs on the Discord asyncio loop.

### 17.2 Decisions

- **One scope**, `meetings.space.readonly` (sensitive, not restricted). No Drive (restricted), no
  userinfo — so `status` can only say "connected". Each user brings their own "Desktop app" client;
  consent screen *Internal* avoids Google verification in Workspace.
- **No new dependency**: urllib, http.server, secrets, hashlib, base64, json.
- **Files**: `<plugin data>/google/{client,token}.json`, created 0600 atomically (dir 0700), under
  `plugin_data_dir` of the active profile (never a hard-coded home). `connected_at` lives in the
  token and survives a re-`connect` (a revoked/expired token is replaced, the window start is kept,
  `reconnected_at` is added): no never-imported gap. `google disconnect` deletes the token, so the
  next `connect` starts a fresh window. Token writes, deletes and the read-refresh-write cycle hold
  `token.json.lock` (`fcntl.flock`, re-entrant per thread): the file is re-read inside the lock and
  again before writing; a vanished file (disconnect) is never recreated and a changed refresh
  token (a new connect) is adopted, never overwritten nor marked disconnected. `invalid_grant`/`invalid_client` mark the token `disconnected` (no refresh storm; the
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
  TTL = 3 × interval), renewed every tick. `stop()` sets a flag checked between records and before
  every page request (`SyncStopped`: nothing half-imported, no status written); the poller THREAD
  releases the lease on exit, on the repository it leased (never through the runtime's factory,
  whose lock `close()` holds), so a stop that times out never leaves a thread using a closed repo.
- **Crash leftovers**: `import_transcript` writes `meta.json` first, then the transcript, then the
  row. Each tick removes folders with a non-discord `meta.json`, no row and older than 1 h
  (`MeetingService.clean_import_leftovers`). A committed row whose job was never enqueued is
  re-queued by the worker's periodic sweep (every `RECLAIM_SECONDS`, rows in
  captured/transcribed/analyzed with no job row and untouched for 10 min), not only at start-up.
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
  automatic channel (superseded by §19: ids or names, no home channel, waits instead of skipping).
  The guild for project channels is the one chosen for the notes (§19).

### 17.3 Transcript attachment (all sources)

`delivery_discord_transcript` (default true): after the summary, in the meeting chat (not the
thread: it is part of the notes, and voice-chat channels often cannot host threads). One file
`transcript-<YYYY-MM-DD>-<slug>.md`, rendered from `transcript.jsonl` with the current title.
Only `DiscordNotesSink.publish` (the DELIVER stage) attaches; `refresh`/`refresh_item`/moves (button
clicks) re-render with `attach_transcript=False`. The `notes` pointer records `attach` when the
summary is first posted with the setting on; a summary without it (pre-attachment version, or the
setting was off) is marked `skipped=legacy` and never gets the file later (privacy: no surprise
publication of old transcripts). Pointer `transcript` in `deliveries` stores sha256 of the
transcript LINES (not the title heading) + message ids: same hash → nothing is posted; different
hash (reprocess) → old messages deleted and the new file posted. Before each upload the pointer
records `sending=<file name>`; a retry after a lost pointer write first searches the channel's last
50 messages for one of ours with that attachment and adopts it (no duplicate). Above 8 MB (conservative
Discord limit) the text is split at line ends into `…-partNofM.md` (UTF-8 safe); more than 20 parts
→ a notice pointing to `export`. Missing Attach Files (403/50013/50001) → one notice, remembered.
Transient failures keep already-posted parts and retry on the next publish. None of this can fail
the delivery.

### 17.4 Risks / needs a live check

- Real Google behaviour: whether `participants.list`
  includes everyone who spoke, pagination sizes, and error bodies for admin-blocked tenants.
- Visibility: `conferenceRecords.list` returns only conferences ORGANISED by the connected user
  (documented: "filtered to the conference organizer"). Meetings the user merely attended are never
  listed; each organiser has to connect their own account. A record that still answers 403/404 on
  its sub-resources is isolated (per-record errors, given up after 5 permanent failures).
- Verified live: a transcript goes from `ENDED` to `FILE_GENERATED` about 5 minutes after the
  meeting ends.
- The transcript attachment makes transcripts visible to everyone in the notes channel; it is on by
  default by design (documented in README and the catalog disclosure).
- Discord's per-file limit can be lower on some servers than the 8 MB we assume only if Discord
  changes it again; the split size is `delivery_transcript_max_mb` (default 8).

## 18. Models, fallbacks and analysis robustness (unreleased)

Production showed three failures in one analysis: the provider ran out of credit (`402 … requested up
to 131072 tokens, can only afford N`), Hermes retried and the call hung ~30 minutes although the plugin
passed `timeout=600`, and the eventual answer was truncated (`LLM response is not JSON`).

- **One source of truth for models**: Hermes' `auxiliary.meeting_scribe` block, the same one its
  auxiliary router reads per call (`_get_auxiliary_task_config`: plugin defaults layered under the
  user's values). `llm_config` never copies it: `llm show` reads it (with the origin of each value:
  `hermes-config` or `plugin-default`, and what `auto` resolves to — Hermes' main model); `llm set`
  and `llm fallback add|remove|clear|set` write it with Hermes' own writer (`save_config(...,
  merge_existing=True)` under the plugin-state lock, after a fail-closed raw read; managed installs
  and administrator-managed keys are refused); `llm test` probes each link with a 5-token call
  through `resolve_provider_client` and redacts the output. `hermes config set auxiliary.…` also
  works but warns "not a recognized config key" because Hermes' `DEFAULT_CONFIG` does not list plugin
  tasks; the plugin's writer avoids that confusion. Alternative discarded: storing models in the
  plugin's own settings and passing `provider=`/`model=` overrides — that needs
  `plugins.entries.meeting-scribe.llm.allow_*_override` trust flags and bypasses Hermes' fallback
  chain, so there would be two places to configure one thing.
- **Defaults**: `register_auxiliary_task(defaults={"provider": "auto", "model": "", "timeout": 600})`
  (bare registration on hosts without `defaults=`). No provider is imposed.
- **Fallbacks** jump on 402 / rate limit / connection errors only (Hermes' behaviour); a HANG is not
  an error for Hermes. Hence:
- **Wall clock** (`analysis_timeout_seconds`, default 600): the call runs in a daemon thread
  (`run_with_deadline`, contextvars copied); when it does not return in time the attempt fails with
  `LlmTimeout` and enters the normal backoff. The thread cannot be killed: it is abandoned and its
  result discarded (it may still finish and cost tokens). No retry inside the same attempt after a
  timeout. Abandoned threads are tracked per task name: with `MAX_ABANDONED` (2) still alive the next
  call fails at once with `TooManyHungCalls` (logged) instead of piling up threads and provider
  connections; a finished thread frees its slot.
- **`analysis_max_tokens`** (default 8192) is always sent so providers do not reserve the whole
  window (the cause of the 402 with a low balance).
- **Non-JSON reply**: one immediate retry within the attempt, with an appended instruction to
  answer with one complete JSON object; the second failure counts as a failed attempt. Cost: that
  chunk is sent and billed twice and the attempt may take up to 2 × the timeout. The existing
  schema-rejection → `json_mode` retry is unchanged (different failure).
- **Chain edits keep hand-written keys**: `fallback add|remove|set` work on the stored entries, not on
  the parsed `Link`s, so `key_env`, `api_key`, `api_mode`, `transport` and any other key survive
  (`set` reuses the stored entry of a link it keeps). A fallback needs a model: Hermes'
  `_resolve_fallback_entry` returns nothing without one, so `add`/`set` refuse it and `view().problems`
  (hence `llm show` and `doctor`) flags existing model-less entries.
- **No credentials in output**: `Link.label()`, `to_dict()` (JSON) and probe errors show a
  `base_url` without userinfo, query string or fragment.
- **Defaults**: `llm set` with a value equal to the plugin default (e.g. `--provider auto`) says
  "using the default value" rather than "saved" — Hermes' writer strips values equal to defaults.
- `doctor` shows `primary → fallback 1 → …` and warns when there is no fallback.

## 19. Where notes are posted (unreleased)

Production: a Meet meeting with no channel configured was posted to the gateway's home channel, which
was a DM with the owner — no server, so no project candidates, tasks were not routed ("This message
does not have guild info attached") and nobody else saw it.

- **Order** (`discord_ui/destination.py`, resolved on the gateway loop per delivery):
  Meet: `google_meet_discord_channel` → `delivery_discord_channel` → AUTOMATIC → PENDING.
  Discord: `delivery_discord_channel` → voice text chat → AUTOMATIC → PENDING.
- **Id or name**: channel settings accept `123`, `<#123>`, `#name` or `name` (`config set` stores
  the id or the bare name; user/role mentions are rejected). Names are compared after removing
  decoration (emoji, `#`, case, `-`/`_`/spaces). Text, announcement, forum and media channels are
  candidates (§19.1). A name matching several of them, another kind of channel (voice, stage,
  category) or nothing is reported (`doctor`, `config list`) and skipped — never guessed.
- **Server**: the meeting's own (Discord); for imported meetings the server of a configured channel
  id, else `delivery_discord_guild` (id or name), else the bot's only server; with several servers
  and nothing configured nothing is guessed. A name unique across all servers also fixes the server —
  but ONLY in that last case: a Discord meeting whose server is not in the cache (not loaded, bot
  removed) and a `delivery_discord_guild` that does not resolve are PENDING with that reason, never a
  global name search (it would post one server's meeting in another server).
- **Ids are checked too**: a configured channel id that the cache shows as a DM (`not_in_server`) or
  as a channel of another server than the chosen one (`other_guild`) is ignored and reported, for
  the notes channel and `delivery_fallback_channel`. The publisher repeats the check on the channel it
  actually opens (ids not in the cache are fetched from the API).
- **Ids are ASCII digits** (`domain.text.is_ascii_digits`): `str.isdigit()` also accepts `²` or
  Arabic-Indic digits, which `int()` rejects or Discord never issues; such values are names.
- **AUTOMATIC**: the server's `system_channel` when the bot can send AND attach files there (the
  transcript), else the first text or forum channel whose clean name is in `delivery_auto_channel_names`
  (list order, then position) where it can send. Default names: general, meetings, meeting-notes,
  notes, reuniones, notas. Never an NSFW channel nor one `@everyone` cannot view (`default_role`
  permissions): notes are for the team, and a private channel is used only when configured
  explicitly (then `doctor` warns that it is not visible to `@everyone`). Permissions must be KNOWN:
  with `guild.me` not cached (server still loading) nothing is chosen automatically and the delivery
  waits with "server … is not loaded yet"; `can_send` treats unknown permissions as "no". "No
  automatic channel the bot can post in" is also PENDING (with the list of names and the skipped
  private/NSFW channels), not a failure.
- **Never a DM**: the home-channel fallback is REMOVED, not just filtered. It was only useful when
  it was a server channel, and then the automatic channel or an explicit setting covers it; keeping
  it would keep the surprising "posted where only I see it" failure and a second implicit rule.
  As defence in depth, a resolved channel without a `guild` is skipped when posting.
- **PENDING**: no resolvable channel → the sink returns `deferred + waiting`; the stage raises
  `StageDeferred(waiting=True)`; the runner re-queues every 2 minutes WITHOUT using attempts and
  WITHOUT the 6 h cap of the Discord-connecting deferral (a missing channel is a configuration
  problem: dropping the notes would be worse than waiting). The reason (with the exact
  `config set` command) is stored under `pipeline.waiting_destination.<id>` and shown by `status`
  and `doctor`; `config set` of a destination key re-queues waiting deliveries immediately. A
  meeting already posted keeps being edited where it is. While a delivery waits, the other sinks are
  not re-run on every 2-minute retry: `pipeline.sinks_done.<id>` remembers the sinks that delivered
  the current `notes.json` (sha256) during this job; it is dropped when the job ends, on an explicit
  `reprocess` and whenever the notes change.
- **Notes an older version posted in a DM** (the removed home-channel fallback) are moved ONLY
  explicitly and safely. Rejected: moving them on any delivery (the first attempt did that) — a
  button refresh or a 📁 move triggered it, it deleted the DM before knowing the server channel
  worked (a deleted channel lost the notes and spent attempts), and it bypassed the legacy
  transcript marker, posting a private DM transcript in `#general`.
  - Trigger: `reprocess <id> --from deliver` (CLI or `/meeting reprocess <id> from=deliver`) sets
    `discord.move_from_dm.<id>`; only the DELIVER stage reads it (`sink.publish`). Button refreshes,
    `refresh_item` and `_move_item` never move. The flag is cleared after that delivery, when the job
    fails for good, and by any other reprocess.
  - Target: only a channel from `google_meet_discord_channel` / `delivery_discord_channel` (never
    the automatic one). None configured → the notes stay in the DM and `discord.dm_notes.<id>`
    stores the exact `config set` + `reprocess` commands (shown by `status`, `doctor` and the
    reprocess reply).
  - Order: open and check the channel (server, same guild) → save a `dm_move` pointer holding the old
    DM pointers, then drop them → publish header, transcript, tasks, index in the channel (each
    pointer saved as it is posted) → only then delete the DM messages and the `dm_move` pointer. A
    failure before the delete leaves the DM untouched with a clear `DmMoveError`; a retry resumes
    from the saved pointers without duplicates; if nothing new was posted yet and the channel went
    away (or a button refresh runs meanwhile), the move is rolled back to the DM pointers.
  - The transcript keeps its intent: the `{"skipped": "legacy"}` marker is not in the DM (it has no
    channel) and stays; the new summary keeps the old intent, so a meeting that was not meant to
    attach its transcript never does after the move. The intent is `attach` when the summary pointer
    has it; pointers written before that key decide from the transcript pointer (delivered = `done`
    with messages → attach; the legacy marker or nothing → no). Rejected: `bool(ptr.get("attach"))`
    — it read "key absent" as "no", deleted a genuinely delivered DM transcript and never re-posted
    it. A resumed `dm_move` re-derives the intent from the DM pointers it holds.
  - Delete only what is replaced: `_finish_move` deletes a DM message only when the new pointer of
    the same kind (notes, transcript, `task:<id>`, index) exists outside the DM with messages (the
    transcript also `done` and not `skipped`); a task that no longer exists needs no replacement.
    Anything else stays, recorded in a `dm_leftover` pointer and explained in
    `discord.dm_notes.<id>` (`status`, `doctor`); the next DELIVER retries the cleanup.
  - During a move that carries a DM transcript, a failed upload (exception or unfinished pointer)
    raises: the delivery fails, the DM is untouched and the job retries. Outside a move the
    attachment still never fails a delivery (§17.3).
  - Until moved, a DM meeting works where it is (buttons, 📋 My tasks, edits in place).
- **Tasks**: with a project → the project channel (routing of §16, candidates from the server chosen
  above, so Meet tasks route too); without → `delivery_fallback_channel` (id or name; anchor +
  thread like a project channel), else the notes chat as before.
- The last resolution per source is stored (`discord.destination_report.<source>`) so `doctor` and
  `config list` — which run in other processes without a Discord connection — can show which
  server/channel a name resolved to, its kind (`forum`/`media`) and the warnings below.

### 19.1 Forum and media channels

Production: the team created a forum (type 15) for meeting notes. `TEXT_KINDS` rejected it by name
and automatically, and a configured id failed on `channel.send` (a forum has none) and on
`message.create_thread`. Media channels (type 16) behave the same.

- **Where forums are accepted**: notes (`delivery_discord_channel`, `google_meet_discord_channel`,
  automatic by name), `delivery_fallback_channel` and project channels (`project_channels`, fuzzy
  routing, 📁 Move; `guild.snapshot_channels` reports them as kind `forum`).
- **Layout**: one POST per meeting, named `<date> · <title>` (≤ 100 chars), created with
  `ForumChannel.create_thread(name, content, view, applied_tags)` → `(thread, message)`. The first
  message is the summary's first part; the other parts, the transcript, the tasks without project
  (the post already is the meeting's thread) and the index go inside the post. A project forum gets
  one post per meeting whose first message is the anchor, the tasks inside — instead of anchor +
  thread. A fallback or project channel that IS the notes forum puts its tasks in the meeting's post
  (never a second post with the same name).
- **Pointers**: `notes` = `{channel: post id, forum, messages: [first, …], url: post url, name,
  tags, attach}`; `thread:<project channel>` = `{channel/thread: post id, message: first, forum,
  url, name, tags}`. Everything else keeps its own pointer with `channel` = post id, so edits,
  buttons and 📁 moves work unchanged.
- **Idempotency**: a stored post is opened and its first message edited; a missing post (404) is
  created again and the transcript pointer that lived in it is dropped, so DELIVER re-attaches it
  (tasks and index re-post through their own pointers). A deleted first message is re-sent INSIDE the
  post (it cannot be recreated as the opening message without a new post). A shorter summary
  deletes surplus parts only — the first is never deleted (that would delete the post). A changed
  title renames the post; a re-posted summary (text or forum) keeps the transcript intent (`attach`)
  of the one it replaces; an archived post (code 50083) is unarchived and the edit retried. A
  rename/tag edit that fails for another reason (e.g. no Manage Threads on a post someone else made)
  is logged and retried on the next publish.
- **Tags**: forum tags whose normalized name (§19 `norm_name`: emoji, case, separators ignored)
  matches the meeting's project (notes post) or the task's project (project post), or a name in
  `delivery_forum_tags`, in the forum's order, max 5. Tags added by hand are kept on a re-tag. If
  the forum has `REQUIRE_TAG` and nothing matched, the first `delivery_forum_default_tag` name the
  forum has. Names are never invented.
- **Refused post** (400 / 40067, tag required): `DestinationPending` with the forum, its tags and the
  exact `config set delivery_forum_default_tag` command; the delivery waits (§19 PENDING, no
  attempts). For a project/fallback forum the rest of the meeting is published first and only those
  tasks wait; they are never re-routed to the notes or another channel, and never DMed. Setting
  either tag key re-queues waiting deliveries (`config.DESTINATION_KEYS`).
- **Permissions**: a forum needs View Channel + Send Messages (create the post) + Send Messages in
  Threads (everything inside), + Attach Files for the transcript. The automatic choice requires them;
  configured channels (notes, fallback, forum project channels) are checked and reported as
  warnings (`doctor`, `config list`), as is a `REQUIRE_TAG` forum without a usable default tag.
- **📁 Move** lists forums; moving a task to a forum that requires a tag and has none for it is
  refused before anything changes (`ForumTagRequired`, plain reply).
- **DMs**: the assignee panel header links to the notes (`notes.url`: the post's URL in a forum);
  a recreated post updates the link on the next publish.
- Not verifiable without real Discord: the exact error codes for archived/locked posts, the
  permission Discord actually enforces to edit `applied_tags` of the bot's own post, and media
  channels' requirement of an attachment on the first message (Discord currently accepts text-only
  posts via the API; if it did not, the post would fail and be retried as a normal error).

### 19.2 Meeting routes and private meetings

Request: some voice channels hold conversations that the rest of the server must not see (a
leadership group, a team's own room). The team wants to choose where those notes go and, for private
rooms, keep everything there and decide what leaves by pressing buttons. Group meetings can also post
their notes in the group's own channel or forum. Tasks still go to their project channels.

**Setting** `meeting_routes` (list, group `delivery`, space-scoped like every setting, format
`meeting_route`; module `routes.py`, pure). There is one rule per entry, `origin = channel[:private]`:

| origin | covers |
|---|---|
| `<voice channel id>` / `<name>` / `#name` | that voice channel (names normalized as in §19) |
| `category:<id or name>` | every voice channel of that Discord category (`Meeting.category_id`, recorded at capture) |
| `meet:<pattern>` | Google Meet: meeting code, Meet room id or title; `*`/`?`, case ignored |

- **channel**: a text, announcement, forum or media channel, given as an id, `<#id>` or name, and resolved like the §19 settings (inside the meeting's space servers only).
- **`:private`**: `:privado`/`:privada` are accepted too and stored canonically as `:private`.
- **CLI**: `config set meeting_routes "Leadership = #leadership-notes:private, category:Design = design-meetings"`, or one rule at a time with `route add` (§19.3).
- **Desktop**: the rule editor of §19.3. The schema has `format: meeting_route`.
- **`:dm`**: direct messages only, no channel (§19.3).
- **Validation on write is strict**: a clear `ValueError` for a missing `=`, an empty channel, an unknown option, a mention used as the origin, a private mark (`private`/`privado`/`privada`) anywhere but as the `:private` option, or the same origin twice (`routes.validate_entries`).
- **Loading is lenient but fails closed**:
  - An entry whose origin is readable but whose rest is not (hand-edited YAML) becomes a *broken* rule. It still matches and its meetings wait with the reason.
  - An entry whose origin is NOT readable but that carries a private mark anywhere (`Leadership:private`, `Leadership -> 700:private`, `<#200> = 700:private`) becomes a broken rule of kind `any`: it matches EVERY meeting of the space, ahead of every other rule, so all of them wait until it is fixed; `doctor` says so. It never marks a meeting private for good (fixing it releases them).
  - An unreadable entry without a private mark is dropped with a warning.
- **Precedence**:
  - A voice channel rule beats a category rule.
  - Within the same kind, the first in the list wins.
  - Meet rules only see Meet meetings, and voice/category rules never do.

**Notes channel order.** A matching rule comes first. If it matches, its channel is the ONLY candidate: there is no Meet channel, no `delivery_discord_channel`, no voice chat and no automatic choice. If the channel is missing, ambiguous, unreachable or in another server, the delivery is PENDING (`DestinationPending`, no attempts used) with the rule named, and the meeting is never posted to a more public channel. Without a match, the order is §19: `google_meet_discord_channel` (Meet) → `delivery_discord_channel` → voice chat → automatic.

**Normal rule.** Only the notes channel changes. Project routing, the fallback channel, assignee DMs and Kanban/Linear stay as they are today.

**Private rule.**
- **Where it is posted**:
  - The summary, the transcript, EVERY task and the index are posted only in the rule's channel: its thread, or its post in a forum.
  - Nothing goes to project channels or the fallback channel, and no assignee panel is sent.
  - Kanban/Linear `auto` behaves as `approve` (`ItemSink.deliver`).
- **Buttons on each task** (second row): `✉️ Send to <assignee>` (`shd`) and `📣 Publish in #<project channel>` (`shp`, the task's normal routing), next to the usual Kanban/Linear/Dismiss/Move.
- **Index**:
  - The index shows "Private meeting…" and "Shared: n of m".
  - Its `📤 Share all tasks` (`sha`) opens an ephemeral confirmation (`shc`) that does the normal distribution: each open task goes to its assignee (DM) and to its project channel.
  - Tasks without a project stay private.
  - Failures (closed DMs, a forum that requires a tag) are listed.
- **What leaves**: only the task (`render_tasks.shared_task_text`):
  - Included: status, title, due date, assignee, project, description and a date footer.
  - Never included: the summary, the quote, the meeting title or a link to the private channel.
  - The Kanban/Linear body of a private meeting is also only the description and the date (`sink.private_item_body`).
- **Shared copies**:
  - Copies have no buttons: decisions stay in the room.
  - A project forum gets one post per shared task, named after the task and tagged like §19.1.
- **State** (`private_share.py`) is kept in pointers:
  - `share:<item>` holds `{dm, channel}`.
  - `stask:<item>` and `sdm:<item>` point to the copies. They are saved before the decision, so a retry edits instead of reposting.
  - A decision already made makes the button a no-op ("That was already done").
- **Reprocess**:
  - Shared copies are edited in place with the new task text.
  - Copies of tasks that disappeared are deleted.
  - The private channel is re-rendered with the same buttons and state.
- **Authorization** (`auth.check_private`, before every button of a private meeting, share or not):
  - The click must come from the private channel, its thread or forum post (`interaction.channel` or its parent) inside a server, never a DM.
  - The clicker must have `view_channel` there (`interaction.permissions`). If permissions are unknown, the click is refused.
  - Any member of the room may share: deciding what leaves is the room's decision.
  - The usual owner/assignee rules still apply to Kanban/Linear/Dismiss/Move.
  - Share buttons on a meeting that is not private answer "no longer private".
- **Sticky privacy** (`privacy.py`):
  - The first `Stages.persist` of a meeting that a private rule matches records `privacy.private.<id>` = `{rule, channel}`; an import records it right after its row is committed, before its job exists (`MeetingService.import_transcript`). The channel is filled in after the first publish.
  - **Anchor.** Once recorded, the channel never changes by itself (`privacy.remember` keeps the first one): the meeting is anchored there. `DiscordNotesSink.destination` always targets the anchor; when the rule was removed, edited to another channel or its channel name now resolves to another channel (a rename), the destination is `held`: refreshes and buttons keep working in the anchor, and DELIVER waits (`DestinationPending`) with the reason and the command that moves it, `hermes meeting-scribe private-move <id> <channel id>` (`privacy.anchor` + a DELIVER reprocess, which withdraws the old copy). Button authorization (`private_place`, `privacy.allowed_places`) uses the anchor, never the rule's current channel. If no channel was ever recorded and no private rule matches, it waits.
  - **Withdrawal** (`private_share.withdraw_public`, `withdraw.Withdrawal`). A meeting that becomes private after it was published normally (a rule added, then a reprocess) is withdrawn from every place outside its private channel, also while its delivery waits for an unusable channel: the summary, transcript and index; the notes thread; task messages in project channels, the fallback channel and the voice chat; assignee DM panels; project anchors and their threads or posts. The private channel (the anchor or, before one is recorded, the rule's channel) is never touched.
    - Everything is deleted when Discord allows it. A thread or forum post needs **Manage Threads** to be deleted, even the bot's own: when refused, each of the bot's messages inside is deleted (or edited to "🔒 Content withdrawn." without buttons or files), and the thread is renamed "Content withdrawn", archived and locked. A message that cannot be deleted (the bot lost access, or Discord fails) is edited to the same neutral text; if even that fails, the step is reported as missing **Manage Messages**. A DM panel is emptied the same way.
    - Each unfinished step is a `withdraw:<kind>:<channel>:<message>` pointer, retried on every later publish of the meeting; `privacy.withdraw_pending.<id>` holds the count and the missing permissions, and `doctor` (check `delivery`) names them with the reprocess command.
  - Its tasks are posted again in the private channel.
- **Chat reads** (agent tools `meeting_search`/`meeting_get`, `/meeting list|show|search|status|reprocess|project`): a private meeting exists only from its place, which is `privacy.allowed_places`:
  - the recorded channel and the rule's channel id;
  - once the notes are there, the notes channel, its forum and its thread.
  - The caller's chat id, thread id and `HERMES_SESSION_PARENT_CHAT_ID` are matched against that set (`privacy.Reader`).
  - Outside it, search hits of the meeting are dropped and `get`/`show` answer exactly like an unknown id.
  - Other platforms never read it. Full access only for the operator: the `hermes meeting-scribe` CLI (`show`, `export`, `list`), the agent in a session whose `HERMES_SESSION_SOURCE` is `cli`, `tui` or `desktop` and that is not a cron job, and Desktop's own API; Desktop marks it 🔒 "Private" (`Library.private`). Any other context fails closed: a cron job (Hermes binds an empty platform and `HERMES_CRON_SESSION=1`, and delivers its output to a chat), webhooks, the API server, unknown or future surfaces, or no session at all (`privacy.Reader.local`).
- **Not affected**:
  - Local files: the meeting folder and Obsidian (local by definition).
  - The recording announcement in the voice channel's chat (it already existed, and it says nothing about the content).
  - `task_moves`: 📁 Move re-routes a task's project, but in a private meeting the task message stays in the private channel and nothing is learned for the space (no project→channel mapping). Only `shp` publishes it.

**Diagnostics.**
- **Report**: every delivery stores `discord.routes_report.<space>`: each rule resolved against the discord.py cache (`destination.resolve_routes`), with its channel id and name, its kind (text/forum/media), whether @everyone can see it, and the problem if any.
- **Where it is shown**: `doctor` (check `delivery`) and `config list` (`routes` in `--json`) print each rule as `origin (kind) → [forum ]#channel (id), private|normal`. A rule not checked yet says so.
- **Warnings**:
  - a private rule on a channel @everyone can see ("everyone there sees the whole meeting");
  - a normal rule on a channel @everyone cannot see;
  - broken or unresolvable rules ("its meetings wait").
- Changing `meeting_routes` re-queues waiting deliveries (`DESTINATION_KEYS`).

**Not verifiable without real Discord.**
- Whether `interaction.permissions` is always populated for component clicks in threads and forum posts. If it is not, the code falls back to `channel.permissions_for(user)`, and without either the click is refused.
- That `Thread.delete` needs Manage Threads even on the bot's own threads and posts (assumed: the withdrawal empties, renames, archives and locks them when refused). Renaming and locking a thread also needs Manage Threads unless the bot created it; archiving its own thread does not.
- The exact error Discord returns for a deleted DM channel while a shared DM copy is being edited. It is treated as "gone" through `is_missing`.

### 19.3 Direct-messages-only meetings and easy rule editing

Request: some meetings (a 1:1, a small private conversation) should not live in any channel at all;
each participant should get the whole meeting privately. And anyone installing the plugin should be
able to write rules without knowing ids or the rule syntax.

**Syntax.** `origin = :dm` (also `origin = dm`, `:directo`/`directo`, `:mensajes`/`mensajes`; stored
canonically as `origin=:dm`). The right side carries NO channel: `:dm` together with a channel or
another option (`#x:dm`, `:dm:private`) is refused on write with the reason. A channel literally
called `dm` is written `#dm`; names that merely contain the word (`dm-notes`, `mensajes-equipo`) are
ordinary channels. `MeetingRoute.dm` is true and `MeetingRoute.private` is true too: a DM meeting is
private for every purpose of §19.2. An unreadable entry mentioning `dm` fails closed exactly like one
mentioning `private` (a broken rule of kind `any`).

**Recipients** (`privacy.people`, also who is mentioned in §19.4). The humans of the meeting
(`Meeting.human_speakers`: who spoke or was in the call, as captured). Only a verifiable identity
becomes a Discord member:
- A Discord speaker: its user id, captured by the bot itself.
- A Google Meet attendee: only its **Google account** — the Meet API's `signedinUser.user`
  (`users/<id>`, stored as `Speaker.google_user`) — linked to exactly one member of the space by a
  plugin owner: `/meeting link @member google=users/<id>` (`links.google_user`, schema 101; linking
  an account to a member takes it away from anyone else, so it never resolves to two people).
- Never the name shown in Meet: the attendee types it (a guest too), so a guest named like a member
  or like a member's email is nobody. A `/meeting link @member <name or email>` is a **Linear**
  link only; it never identifies anyone in Meet. The Meet API gives no email for an attendee, so
  there is no email match either.
- Who may link: anyone may link THEMSELVES to Linear; linking another member, and every Google
  link, is for the plugin's owners (`Runtime.owners`, the same admins as §16) — a Google link decides
  who receives a direct-messages meeting.
- Anyone else (guests, phone callers, an account nobody linked) is unmapped: named, never sent
  anything. `status`/`doctor` list them with their account (`Name (Google users/<id>)`) so an owner
  can link it (`privacy.participants`).

**Delivery** (`discord_ui/dm_delivery.DmDelivery`, reached from `DiscordNotesSink.publish` when the
destination is `dm`; the destination has no channel keys, so it never waits for one):
- Each recipient gets in their DM: the notes (summary, decisions, questions — the normal render), the
  transcript `.md` when `delivery_discord_transcript` is on, an index of every task with its
  assignee (`render_dm_index`, no buttons) and one message per task **assigned to that recipient**,
  with the usual Kanban / Linear (as configured) / Dismiss buttons and `📣 Publish in #<project>`
  (`shp`, the §19.2 share: only the task text leaves).
- Pointers `pdm:<user>:notes|transcript|index|task:<item>` are saved as soon as each message exists;
  a reprocess or a refresh edits them in place, a task reassigned to someone else is deleted from
  the old assignee's DM, and a refresh never creates a copy that was not delivered.
- **Anchor.** The first delivery that reaches AT LEAST ONE recipient records `privacy.private.<id>` =
  `{rule, channel:"", mode:"dm", recipients:[…]}` (`privacy.anchor_dm`). From then on the meeting is a
  DM meeting whatever the rules say (removing or editing the rule never publishes it in a channel)
  and its recipient list is fixed. Until then nothing is anchored and the list is worked out again
  at every attempt (someone linked or with their DMs opened later is included). A meeting already
  anchored to a private channel is never turned into a DM meeting.
- **Closed DMs.** A recipient whose DMs are closed (`Forbidden` 50007 / 403) or who is unknown to
  Discord (`NotFound` 10013 Unknown User, a deleted account) is skipped; the others get their copy. `privacy.dm_unreachable.<id>` lists who and why; `status`, `doctor` (check
  `delivery`) and Desktop's status show it. Nothing is ever posted to a channel instead.
- **Nobody reachable.** No recipient mapped, or every DM closed: the delivery is PENDING
  (`DestinationPending`, no attempts used) with the reason (link people with `/meeting link`, or open
  DMs), and nothing is anchored until someone got it.
- **Withdrawal.** A meeting that was published before the `:dm` rule existed is withdrawn from every
  public place first (`private_share.withdraw_public`, §19.2), then delivered by DM.
- **Everything else of §19.2 applies:** no project channels, no fallback channel, no assignee panel
  (everyone already has the whole meeting), Kanban/Linear `auto` behaves as `approve`, no
  project→channel learning.

**Buttons** (`auth.check_dm`, `ButtonActions._dm_gate`): a click counts only in the DM the bot sent
to the clicker (`interaction.channel` is the clicker's recorded DM channel, never a server) and only
on a task assigned to the clicker. Meeting-wide buttons (share all) and 📁 Move are refused, and a
DM task message carries no 📁 Move button: moving would re-route the task to a server channel the
participant may not see (and publish it there with 📣). Decision: a
participant can NOT send another participant's task to them — every participant already received
their own copy with their own tasks, so there is nothing to forward, and letting one DM act on
someone else's task would let a participant decide what leaves for another.

**Reads** (`privacy.allowed_places`): a DM meeting is readable by the agent and `/meeting` only from
the DM channels of its recipients' copies; before any copy exists, from nowhere. Operator surfaces
(CLI/TUI, Desktop) as in §19.2.

**Channel catalog.** The gateway (the process connected to Discord) writes, per server, a snapshot of
the bot's channels in `kv` `discord.channels.<guild>` = `[{id, name, type: voice|text|forum|media|category, parent_id,
parent_name, public}]` (`public`: @everyone has View Channel; the row's `updated_at` is `seen_at`).
A corrupt value reads as no catalog for that server (rules stay `not_checked`, never silently ok). It is written at every connect (next to `set_bot_guilds`) and, debounced 5 s per server,
on `on_guild_channel_create|delete|update` and `on_guild_join` (`discord_ui/channel_watch.py`).
Threads are not listed. Every other process only reads it (`channel_catalog.Catalog`); nothing
outside the gateway talks to Discord.

**Using it.** `doctor.route_rows` resolves each rule against the catalog of its space's servers
(`doctor.space_guilds`, §23): an install with one space owns every server; with several, a space
sees only its assigned servers and **a space without servers sees no channel** (the catalog is
empty, every rule side is refused with "assign a server to this space", REST
`/v1/discord/channels` answers `no_servers: true` and Desktop says so). An id of a channel
catalogued in another space's server is refused ("belongs to a server outside this space") even
before the space's own servers were catalogued. A rule becomes `ok` (with ids, names,
kinds and visibility), `problem` (missing, ambiguous, wrong kind) or stays `not_checked` without a
catalog; a private rule on a channel @everyone sees gets the warning. The delivery report of §19.2
still refines it when present. `config list`, `doctor`, `route list` and Desktop print
`Leadership (voice channel, 300) → forum #leadership-notes (700, private channel), private`.

**Editing rules** (`route_editor.py`, shared by CLI, REST and Desktop): a rule is three choices —
origin kind (`voice`, `category`, `meet`) and value, destination (text/forum/media channel; none
for `dm`) and mode (`normal`, `private`, `dm`). With a catalog, names become ids (renaming a channel
never breaks the rule) and wrong names are refused with the reason; without one, the rule is stored
as written and checked later. The list is validated as a whole (`validate_entries`: same origin
twice, …); removing never needs the rest to be valid. It is written to the space's override when
the install has several spaces or the space already has its own list, else to the global value.
- **CLI** (`cli_routes.py`): `route list [--json]`, `route add --voice|--category|--meet … [--to …]
  [--private|--dm] [--position N]`, `route remove <n|origin>`, `route move <n|origin> <position>`,
  all with `--space`. A change re-queues deliveries waiting for a rule.
- **Setup** asks, optionally, for rules one at a time (with several spaces it points to `route add
  --space`).
- **REST**: Appendix A.
- **Desktop** (Settings → Delivery, `RoutesEditor`): the rules as sentences («Voice channel
  "Leadership" → forum "notes" · Private») with their status, ↑/↓ and remove; «Add rule» opens a
  form with a SegmentedControl for the origin kind, a Select of voice channels or categories (an
  Input for Meet patterns or when there is no catalog), a SegmentedControl Normal / Private / Direct
  messages only with one plain sentence per mode, and a Select of text channels and forums grouped
  by category with a lock on private ones (hidden for DM). A private rule to a channel everyone sees
  shows the warning before saving. With several spaces a Select picks the space. The raw
  `meeting_routes` list field is no longer shown there (the editor replaces it).

**Not verifiable without real Discord.**
- That `on_guild_channel_update` fires for permission-overwrite changes (a channel made private)
  as documented; if not, the catalog catches up at the next connect or channel event.
- `DMChannel` ids stay stable across bot restarts for the same user (assumed; pointers and reads use
  them).
- The exact error for a user with closed DMs (`Forbidden` 50007) — treated as unreachable.

### 19.4 Participants mentioned in the notes

Request: wherever the notes of a meeting are published (a text channel, its thread, a forum post, a
private rule's channel), the first message @mentions the humans of the meeting so they know the notes
are there.

- **Setting** `delivery_mention_participants` (bool, default `true`, group `delivery`, space-scoped;
  Desktop shows it as a switch in Delivery).
- **Who** (`privacy.people`, shared with §19.3): `Meeting.human_speakers` — Discord speakers by id
  (who spoke or was in the call); Google Meet attendees only through their Google account linked by
  an owner (§19.3: never by the name shown in Meet); anyone else by name only, as inert text
  (`safe_name`: a typed `@everyone` never pings). The bot (client user / guild `me`) is never listed.
- **Where in the message**: one line `-# 👥 Participants: <@a>, <@b>, Name` under the title of the
  FIRST part of the summary (`render_header(participants=…)`); at most 40 mentions, the rest counted.
- **Pings** (`MessageSpec.mentions`, `Messages._mentions`, `DiscordViews.mention_kwargs`): the first
  part is sent with `AllowedMentions(users=[exactly those ids], roles=False, everyone=False,
  replied_user=False)`; the other parts ping nobody. Only the very first publication pings (no
  `notes` pointer yet): the line is saved in the pointer (`people`) and reused verbatim; every edit
  and any re-post (a deleted summary, a reprocess) carries the same text with `users=[]`.
- **Who may be pinged**: a member the bot can check (`guild.get_member` +
  `channel.permissions_for(member).view_channel`, a thread/post judged by its parent) is mentioned
  only if they can view the channel. A member the bot cannot check is mentioned only in a channel
  @everyone can see and not under a private rule; otherwise they appear by name (fail closed: no
  forced notification for someone without access).
- **Not applied** to direct-messages-only meetings (§19.3): each participant already gets their copy.
- **Nothing else pings.** Every message the plugin sends names the users it may notify
  (`MessageSpec.mentions`, empty by default) and is sent with exactly those: panels, index, summary
  parts, transcript labels, shared copies. Text the plugin does not write — the model's summary,
  decisions, questions, task titles, a Meet name — may contain `<@id>`, `<@&role>` or `@everyone`:
  it shows as written but notifies nobody.
- **Task messages** follow the same rule (`mentions.may_mention`): the assignee is shown as `<@id>`
  (and pinged when the message is first posted) only if they can view the channel where it goes;
  otherwise — a private channel they cannot open, or a member the bot cannot check there — by name
  (`**Name**`). The tasks index and the "Sent to" line of a private task do the same.

**Not verifiable without real Discord.** That `guild.get_member` is populated (members intent and
cache) for everyone in a private channel; without it those people are named, not mentioned.

## 20. Configuration schema for UIs (unreleased)

`config.SPEC` stays the single source of truth; each `Opt` now has a `group` (capture,
transcription, analysis, llm, delivery, projects, google_meet, integrations, privacy, pipeline, ui)
and a `format` (`discord_channel`, `discord_guild`). Labels and help live in the i18n catalogs
(`cfg.<key>.label/help`, `cfg.group.<group>`), en and es; tests require both for every key.
`plugin.yaml` (`label`, `description`, `group`, `format`, `minimum`, `maximum`; unknown keys are
ignored by Hermes' form) and the README tables are generated by `scripts/gen_manifest.py`; tests fail
on drift.

- `config list [--json] [--group G]`: effective value + origin (`default` | `configured` |
  `invalid`) + `resolved` (the channel/server a name resolved to, from §19's report), plus the LLM
  view.
- `config schema --json [--lang]`: `{"version": 1, "plugin", "language", "groups": [{key, label}],
  "fields": [{key, type, group, label, help, default, storage, path, choices?, minimum?, maximum?,
  format?}]}`. `storage` is `plugin` (`plugins.entries.meeting-scribe.settings.<key>`) or `hermes`
  (the virtual `llm` group: `llm_provider`, `llm_model`, `llm_base_url`, `llm_fallback_chain` at
  `auxiliary.meeting_scribe.*`, each with the `cli` command that edits it). Adding fields is
  compatible; `version` changes only for incompatible shape changes.

Fixed on purpose (not settings): Discord limits (2000/4096 chars, 25 select options, 100-char
names), button id templates, lease/heartbeat/reclaim timings, the retry backoff curve
(60/300/900 s; only the attempt count is a setting), Meet's 30-day retention and its 5-failure
give-up, whisper hallucination filters and merge gaps, status emojis and thread names (i18n text,
not configuration), fuzzy-matching internals (the user-facing thresholds are settings). Each is
either imposed by an external system or an internal safety margin a user cannot tune meaningfully.


## 21. Hermes Desktop Meetings page (unreleased)

**Frontend.** `desktop/plugin.js` is one ESM file the runtime loads without a build. The UI is
written as `jsx()` calls and imports only `@hermes/plugin-sdk`, `react` and `react/jsx-runtime`: the
loader refuses anything else, and `plugins validate` checks the desktop surface. The file registers
one exact route (`/meeting-scribe`), a sidebar row and a palette command. The router has no params,
so the tab and the selected meeting live in module atoms. Styling uses only the host's CSS variables
(`--ui-*`), injected as a `<style>` that is removed on dispose; no host Tailwind class is assumed.
The page's texts are its own `ctx.i18n.register({en, es})` bundles, separate from the bot's
`meeting_scribe/i18n` catalogs. Field labels come from the schema (`/v1/settings?lang=`).

**Data.** Every call goes through `ctx.rest` (namespace-scoped) and the SDK's React Query. The data is
the owner profile's (§1.5) whichever profile Desktop was opened with, so a profile switch changes
nothing on this page. A launch profile without the plugin gets Hermes' `404 Plugin not found`; the
page then says to open Desktop with a profile where Meetings is on, or to turn it on there; query keys carry
the connection (another Hermes install), and a connection switch closes the open meeting.
Updates come from polling, not events (`broadcast_plugin_event` is process-local): the library every
30 s, status every 15 s, an unfinished meeting every 10 s, and a pending command every 2 s until it
is `done`, `failed` or `unknown`. Reprocess sends a client-generated `request_id` with
`confirm: true` after the `ConfirmDialog`, so a retried POST has no effect.

**Transcript.** The first page (200 lines) comes through the cache. `Load more` and
`Load everything` then follow the cursor, 500 lines per request. The search box filters the loaded
lines and says when the result is partial.

**Audio.** Shown only when `audio.available` (`recording.ogg`). The `<audio>` element tries
`hermes-media://stream/<path>` (local backend) first, then
`hermes-media://remote/<path>?connectionId&profile` (remote gateway, proxied to
`/api/files/stream`). If both fail, it explains that the file stays on the Hermes machine.
Multitrack `.mka`, imported Meet meetings and `audio_retention: none` show a plain reason instead.
`/v1/meetings/{id}/audio` (Range) exists for a future direct fetch, but the page does not use it:
`<audio src>` cannot carry Desktop's auth headers.

**Settings.** The form is drawn from `schema.fields`, plugin storage only; the `llm` group becomes
the Models editor. Each type maps to a control:

- bool: a switch, saved on toggle
- choices: a select, saved on change
- int/float: a number input, with the schema bounds checked client-side
- list: a textarea, one item per line
- str: a text input

Each field saves on its own (`PUT /v1/settings/{key}`) and shows the server's validation message
under it. The origin badge comes from `values[key].origin`. Models saves with `PUT /v1/llm`, sending
the primary and the whole ordered chain.

**Tests.** `tests/desktop/run.mjs` (node:test) loads the real module with a stub SDK and renders each
view with react-dom/server. React comes from `MS_REACT_DIR` or the Hermes checkout's
`node_modules`; without React only the structural tests run. `tests/unit/test_desktop_plugin_js.py`
runs `node --check` and the Node suite, and checks that every SDK import exists in the Hermes SDK
index.

**Needs a live check.**

- Rendering inside Desktop: theme, focus rings, ConfirmDialog, SearchField.
- The Capabilities toggle.
- hermes-media playback and seek of `recording.ogg`, locally and on a remote gateway.
- Reprocess end to end with a running gateway.

## 22. Parallel processing

**Workers.** `pipeline_workers` (default 2, 1–8) threads run jobs; each is spawned through the host
spawner, so contextvars follow it. The count is read when the worker starts: a change applies after a
gateway restart.

**One claim statement.** `claim_next_job` selects AND leases the next ready job in one
`UPDATE … WHERE id=(SELECT …) RETURNING *`. Two workers can never take the same job, whether they
are threads of this process or other processes on the same database. The lease, heartbeat and
`requeue_stale`/`owner_dead` recovery are unchanged, so a job abandoned by a dead worker is taken back
exactly as before. Only one worker at a time runs the periodic reclaim sweep.

**Transcription cap.** Transcription is the CPU/GPU-heavy step. At most `pipeline_max_transcriptions`
(default 1) jobs of this process are in the transcribe stage at once. While the cap is reached, the
claim skips jobs queued at `transcribe`, so the other workers keep analysing and delivering. The slot
is freed as soon as the job leaves the transcribe stage (its analysis does not hold it). The cap is
per process: a second process on the same database (a CLI `reprocess --now`) has its own.

**Shared connection.** Every worker, heartbeat and command handler of one process shares the
repository's connection. `Repository._x` therefore reads a statement's rows while it holds the lock
and returns a `Result` (rows + `rowcount`), never a live cursor. A cursor fetched after the lock was
released could interleave with another thread's statement.

**Reprocess vs. a running worker.** `reprocess` first `hold_job`s the meeting's job: in one
transaction it refuses when the job is `running`, or else parks it as `held`, which no claim takes.
Then it rewinds the meeting and re-enqueues it. A failure in between restores the previous job state.
A worker can no longer claim the job between the "is it running?" check and the rewind.

**Desktop commands.** `desktop.control.execute_one` already moves a command `queued → running` with a
conditional `UPDATE`, so only one worker executes it. Its `_reconcile` of dead executors is unchanged.

## 23. Spaces (unreleased)

One installation and one bot serve several teams or clients. A **space** is one of them: its own
settings (overrides on top of the global ones), Discord servers, meetings, jobs views, owners, person
links, learned channel/project maps and Google connection. Nothing crosses from one space to another.

**Storage baseline.** Schema `user_version` 100 is the baseline of record; later changes are
ordinary migrations on top of it (101: `links.google_user`, §19.3). A database written before
spaces (any version 1–99) is not migrated: under a cross-process file lock it is copied with SQLite's
backup API into `backup-<UTC>/` together with its `meetings/` folders, and a fresh baseline is
created. Meetings recorded before spaces were disposable; configuration (Hermes settings) and the
Google OAuth files are kept. The same lock covers creating a new database, because processes that
open a new file at once otherwise race on `PRAGMA journal_mode=WAL` (`database is locked`: SQLite's
busy handler does not cover that pragma). Opening a current WAL database takes no lock.

**Bootstrap.** Every open runs `spaces.bootstrap`. It is idempotent. The first time, it creates `main`
(named in `ui_language`, marked `adopt_guilds`) and moves the loose files of `<data>/google/` into
`google/main/` through a staging folder, so the move completes even after a crash. On its first
Discord connect, `main` adopts the bot's servers once, and only while it is the only space.

**Resolution.**
- Server → space: `space_guilds.guild_id` is a primary key, so a server belongs to at most one space.
  With one space, an unowned server joins it automatically (`claim_guild`), which is the behaviour
  before spaces. With several spaces, an unowned server is never recorded, never shown and never
  published to.
- Chat commands take the space of `HERMES_SESSION_SCOPE_ID` (the guild id on Discord). In a DM or on
  another platform they use the only space; with several spaces they explain that a choice is needed.
  The space is held in a `ContextVar`, so concurrent commands never see each other's space.
- Agent tools (`meeting_search`, `meeting_get`) follow the same rule.
- Capture resolves the space after marking the server as starting (review W7). Auto-join reads the
  channel's space settings, and the meeting carries its space from creation.
- Button clicks look up the space of the clicked meeting. Its language and owners apply.
- Publishing: `resolve(..., allowed_guilds=)` never picks a server or channel id outside the meeting
  space's servers. The restriction applies only with several spaces; with one space every server of
  the bot is allowed, as before. With several spaces, a channel id that is not cached cannot be
  checked, so it is refused.
- Channel catalog (§19.3, `doctor.space_guilds`): a space's editors, `doctor`, `config list` and REST
  `/v1/discord/channels` see only the channels of the space's servers. With several spaces, a space
  without servers sees no channel at all — never another space's — and its rule editor refuses
  every channel until a server is assigned; a channel id of another space's server is refused.
- Per-space settings reach the sinks, the transcriber (`transcribe_language`), the analyzer, render
  options, the channel catalog (ignored prefixes) and the Meet pollers. Machine-wide keys
  (`pipeline_*`, `audio_bitrate_kbps`, `audio_ffmpeg_path`…) cannot be overridden per space.
- The job queue is shared. `status`, `list_jobs` and `pending_job_count` filter by space.
  `space=None` is reserved for machine-wide operator views.
- Google Meet: one importer and one poller per space, with keyed status, per-record memory and lease
  (`google-meet-poll:<space>`). The worker pulse reconciles pollers at most every
  `POLLER_RECONCILE_SECONDS` (60 s), so a space created elsewhere gets its poller without a restart
  and without a database read on every tick.

**Choosing a space.**
- CLI: `--space <slug>` on `status`, `list`, `show`, `export`, `reprocess`, `config get|set|list` and
  `google connect|status|sync|disconnect`. An unknown slug exits 2. Views (`status`, `list`, `show`,
  `export`) without `--space` read every space and add a space column; actions (`reprocess`,
  `config set`, `google *`) without `--space` exit 2 when there are several spaces. `export`/`reprocess`
  of an id that belongs to another space than `--space` exit 1. `hermes meeting-scribe space
  list|show|create|rename|delete|add-guild|remove-guild|set|unset` manages spaces, their servers and
  overrides.
- Chat, in a server: the server's space. An unowned server (several spaces) gets a plain answer
  naming the CLI command an administrator runs (`space add-guild <space> <server id>`); `space=` in a
  server must name that server's own space.
- Chat, in a DM with several spaces: the spaces whose servers the caller is a member of, read from the
  connected bot's member cache (`discord_ui.membership_for`). One → used. Several → `space=<slug>`
  picks one of them; without it the reply lists them. None, or Discord not connected → the reply asks
  to run the command inside the team's server. A slug the caller is not in is never used.

**Concurrency (Discord).** Capture sessions are keyed by server, so different servers record in
parallel, each in its own space. Hermes' adapter keeps ONE voice connection per bot and server
(`_voice_clients[guild_id]`), so a server records one channel at a time: auto-join never leaves a
live recording for another channel, and `/meeting start` from another channel of the same server
answers with the channel being recorded (`capture.other_channel`). Stopping one server leaves the
others recording.

**Google Meet.** Overlapping meetings are imported independently (records are keyed by conference
record name and space; the same record name in two accounts gives two meetings). Status,
`Retry-After` pause, per-record memory and the poll lease are per space: one space paused by Google
does not delay another.

**Doctor.** `spaces`: each space with its servers; servers the bot is in that no space owns (warn,
with the `add-guild` command); baseline backups kept; the voice limit above. `google_meet`: one line
per space (`[slug] …`), worst status wins; the fix commands carry `--space`.

**Desktop REST.** Every data endpoint takes `?space=`; the contract is in Appendix A. The page itself
has no selector yet: with several spaces it gets 409 and shows the error.

**Still pending.**
- Desktop UI: space selector, spaces/servers screens and the per-space settings form (the REST
  contract exists; `desktop/plugin.js` is unchanged).
- Delivery report per space: `discord.destination.<source>` is still keyed per source only.
- Chat has no admin command to assign a server; it is done from the CLI or the REST API.

## Appendix A. REST contract (Desktop, `/api/plugins/meeting-scribe`)

Authentication is the host's (session token / OAuth gate). All bodies are JSON. Errors are
`{"detail": "<plain sentence>"}` with:

| Code | When |
|---|---|
| 400 | invalid input: bad value, bad cursor/date, unknown setting, machine-wide key with `?space=`, missing `confirm: true`, bad request id, non-numeric server id, empty name |
| 403 | the setting is managed by the administrator / managed install |
| 404 | unknown meeting, command, space (`there is no space called 'x'`) or server-of-space; a meeting of another space is "not found" |
| 409 | several spaces and no `?space=` on a data endpoint (`this installation has several spaces: say which one with ?space=<id> (the list is at /v1/spaces)`); slug taken; server owned by another space; deleting a non-empty or the last space |
| 422 | FastAPI parameter validation (e.g. `limit` out of range) |
| 503 | `owner_profile` cannot be used (§1.5); the detail says why |

`space` (query, ≤ 40 chars): optional with one space; required (409) with several on
`/v1/meetings*`, `/v1/commands/*` and `POST …/commands`. Optional everywhere else as described.

### Data (space-scoped)

| Method, path | Params / body | Response |
|---|---|---|
| `GET /v1/meetings` | `space, q, source, state, since, until, channel, project, cursor, limit(1–100, 30)` | `{items:[meeting row], next_cursor, total, facets:{total, states, sources, channels:[{id,name,count}], projects:[{name,count}]}}` |
| `GET /v1/meetings/{id}` | `space` | `{meeting, notes, tasks, transcript_total, audio:{available, reason?, path?, stream_path?, can_prepare?}, history, job, command, projects, waiting_destination, dm_notes, destinations:{discord, kanban, linear, kanban_board}}` (destinations use the space's settings) |
| `GET /v1/meetings/{id}/transcript` | `space, cursor, limit(1–500, 200)` | `{items:[{id,t0,t1,speaker,text,…}], total, next_cursor}` |
| `GET\|HEAD /v1/meetings/{id}/audio` | `space`, `Range` | the listening copy (`playback.ogg`) or `recording.ogg`, inline, 206 with Range; 404 without audio |
| `POST /v1/meetings/{id}/commands` | `space`; `{request_id:[A-Za-z0-9_-]{1,100}, action:"reprocess", stage:"transcribe"\|"analyze"\|"deliver", confirm:true}` or `{request_id, action:"prepare_audio", confirm:true}` | `{id, action, stage, state:"queued", …}`; the same `request_id` again returns the existing command |
| `GET /v1/commands/{rid}` | `space` | `{id, action, stage, state, error, created_at, updated_at, …}` |
| `POST /v1/commands/{rid}/acknowledge` | `space`; `{confirm:true}` | the command, `state:"acknowledged"` |

### Status and doctor

| Method, path | Params | Response |
|---|---|---|
| `GET /v1/status` | `space` (optional) | `{worker:{state:"recent"\|"stale"\|"unknown", last_seen}, queue:{running,queued,failed} (machine), space, counts, jobs, waiting_destination, dm_notes, dm_unreachable, commands, google, settings_warnings, recording:[{meeting_id, title, started_at, live}]}` (`recording`: meetings in state `recording` from the database, `live` when their capturing process is alive; Desktop's Bot card lists them). With a space: `counts/jobs/…` are that space's and `google` is its connection (`{enabled, client_stored, connected, revoked, connected_at, commands:{connect,status,enable}, last_poll_at?, last_poll_ok?, last_error?, last_import_at?, last_import_meeting?, records_given_up?, records_given_up_last?, retry_after_until?}`). Several spaces and no `space`: `space:null, google:null`, lists empty, `counts` = `queue`. |
| `GET /v1/doctor` | – | `{exit_code, checks:[{name, status:"ok"\|"warn"\|"fail", detail}]}`; walks every space; the first check, `owner`, names the owner profile whose data is served |

### Spaces and servers

| Method, path | Body | Response |
|---|---|---|
| `GET /v1/spaces` | – | `{items:[space]}` with `space = {slug, name, guilds:[{id,name}], google:{enabled, connected, last_check, last_import, error}, counts:{meetings, by_state:{<state>:n}}}` |
| `POST /v1/spaces` | `{name, slug?}` (slug `^[a-z0-9][a-z0-9-]{0,31}$`, default derived from the name) | `space`; 409 slug taken; 400 bad name/slug |
| `PATCH /v1/spaces/{slug}` | `{name}` | `space` (the slug never changes) |
| `DELETE /v1/spaces/{slug}` | – | `{deleted: slug}`; 409 when it has meetings or is the last one |
| `PUT /v1/spaces/{slug}/guilds/{guild_id}` | – | `space`; the name comes from the bot's server list; 409 owned by another space |
| `DELETE /v1/spaces/{slug}/guilds/{guild_id}` | – | `space`; 404 when that space does not own it |
| `GET /v1/guilds` | – | `{items:[{id, name, space: slug\|null, bot_present}], seen_at}` (servers the bot reported at its last connect, plus servers assigned by id it has not reported: `bot_present:false`; `seen_at:null` before the gateway ever connected) |

### Settings

| Method, path | Params / body | Response |
|---|---|---|
| `GET /v1/settings` | `lang`, `space` (optional) | `{schema:{language, groups, fields:[{key, group, type, scope:"global"\|"space", storage, label, …}]}, values:{key:{value, origin:"default"\|"configured"\|"space"\|"invalid"}}, warnings, global:[keys], space:[keys], space_slug, overrides:{key:value}, llm}`. Without `space`: global values. With it: what that space sees. |
| `PUT /v1/settings/{key}` | `space` (optional); `{value}` | without `space`: `{key, value, scope:"global", requeued}` (Hermes config). With it: the space's override, `{key, value, scope:"space", space, requeued}`; `value:null` removes it; a `global` key → 400 |
| `PUT /v1/llm` | `{provider?, model?, base_url?, timeout?, fallback_chain?}` | the LLM view (machine-wide) |

### Channels and meeting rules (§19.3)

| Method, path | Params / body | Response |
|---|---|---|
| `GET /v1/discord/channels` | `space` | `{items:[{id, name, type:"voice"\|"text"\|"forum"\|"media"\|"category", parent_id, parent_name, public, guild_id, guild_name}], seen_at, no_servers}` from the gateway's catalog of the space's servers only; `items:[]`, `seen_at:null` before it reported; a space without servers: `items:[]`, `no_servers:true` |
| `GET /v1/routes` | `space`, `lang` | `{space, scope:"global"\|"space", catalog_seen_at, items:[rule]}` with `rule = {position, text, origin, kind, channel, private, mode:"normal"\|"private"\|"dm", status:"ok"\|"not_checked"\|"problem"\|"invalid", detail, warning?, sentence, origin_check, target_check, channel_id?, channel_name?, target_kind?, public?}` |
| `POST /v1/routes` | `space`, `lang`; `{origin_kind:"voice"\|"category"\|"meet", origin, target? (not with dm), mode, position?}` | the list plus `{added, warning}`; 400 with the reason (unknown or ambiguous name, wrong kind, a channel with `dm`, same origin twice) |
| `PUT /v1/routes/{position}` | same body | the list plus `{warning}` |
| `DELETE /v1/routes/{position}` | `space`, `lang` | the list |
| `POST /v1/routes/{position}/move` | `{to}` (1 = first) | the list |

With several spaces `space` is required (409) on these endpoints. Writes go to the space's override
when the install has several spaces or the space already has its own list, else to the global value.
