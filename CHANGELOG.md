# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Notes per voice channel, category or Meet meeting, and private meetings** (`meeting_routes`,
  e.g. `Leadership = #leadership-notes:private, category:Design = design-meetings, meet:abc-* =
  #meet-notes`). A matching rule decides the notes channel (it wins over
  `google_meet_discord_channel`, `delivery_discord_channel`, the voice chat and the automatic choice).
  If its channel cannot be used, the meeting waits and is never posted to a more public place. A
  normal rule only moves the notes. A **private** rule keeps the summary, the transcript and every
  task in its channel: nothing goes to project channels, the fallback channel, DMs or Kanban/Linear
  on its own. Members who can see that channel press **Send to <assignee>**, **Publish in
  #<project>** or **Share all tasks** (with confirmation), and only the task text leaves. Privacy
  is sticky (removing the rule never publishes a private meeting elsewhere), idempotent and survives
  reprocessing. The agent's tools and `/meeting` only read a private meeting from its own channel.
  `doctor` and `config list` show each rule resolved and warn about a private rule on a channel
  @everyone can see (and the opposite). Desktop marks private meetings. A published private meeting
  is anchored to its channel: editing the rule or renaming the channel never moves it (it waits, and
  `hermes meeting-scribe private-move <id> <channel>` moves it). A meeting that becomes private is
  withdrawn from every public place — summary, transcript, task messages in project, fallback and
  voice channels, threads and forum posts, assignee DMs — even while it waits; what Discord refuses to
  delete is emptied, renamed "Content withdrawn", archived, locked and retried, and `doctor` names
  the missing **Manage Threads**/**Manage Messages**. Cron jobs and any context other than the
  CLI/TUI, Desktop or the meeting's own Discord channel never read a private meeting. An unreadable
  rule that says "private" holds every meeting of the space instead of being ignored. Imported
  meetings are private from creation, and 📁 Move in a private meeting learns no routing. `scripts/gen_manifest.py
  --check` verifies the manifest without writing.

- **Forum and media channels** as destinations for notes (`delivery_discord_channel`,
  `google_meet_discord_channel`, automatic by name), for `delivery_fallback_channel` and for project
  channels. Each meeting is one post `<date> · <title>`: the summary is its first message; the
  transcript, the tasks with their buttons (Kanban, Linear, Dismiss, Move) and the task index go
  inside. A project forum gets one post per meeting with its tasks. Pointers keep the post and its
  first message, so re-deliveries and clicks edit in place; a deleted post is created again (with its
  transcript), a new title renames it and an archived post is unarchived to edit it. Forum tags that
  match the project or the new `delivery_forum_tags` are applied (max 5); forums that require a tag
  use the new `delivery_forum_default_tag`, and a post Discord still refuses leaves the delivery
  waiting with the reason (never another channel, never a DM). `doctor` and `config list` show forum
  channels and missing permissions (Send Messages in Threads, Attach Files); assignee DMs link to the
  meeting's notes (the post). 📁 Move offers forums and refuses one that would reject the post.

- **Meetings from any profile**: one installation, one owner profile. `owner_profile` (under
  `plugins.entries.meeting-scribe` in the default profile's `config.yaml`) names the profile that
  runs the bot and keeps the data; without it, the owner is the profile that holds the real copy.
  Other profiles link that copy and turn it on: their gateways start no runtime, register no tools or
  chat commands, and their `hermes meeting-scribe` only points to the owner; their Desktop backend
  serves the owner's meetings, so the page works whichever profile Desktop was opened with. New
  `owner` doctor check; the REST API answers 503 with the reason when `owner_profile` cannot be used.

- **Spaces**: one bot serves several teams or clients, each with its own Discord servers, meetings,
  settings overrides, people links and Google Meet connection. `hermes meeting-scribe space …`
  manages them; `--space` on `status`, `list`, `show`, `export`, `reprocess`, `config` and `google`;
  `space=<id>` for `/meeting` in a DM; a `spaces` doctor check (unassigned servers, baseline backups,
  one voice connection per server); Desktop REST endpoints with `?space=` plus `/v1/spaces` and
  `/v1/guilds` (the page has no selector yet). A pre-spaces database is backed up and started fresh.

- **Google Meet import** (opt-in, `google_meet_enabled`): transcripts that Meet already generated are
  imported and processed like Discord meetings (analysis, tasks per project, Kanban/Linear, Discord).
  Each user connects their own Google OAuth client with `hermes meeting-scribe google connect`
  (PKCE, loopback or `--no-browser` paste, read-only scope `meetings.space.readonly`, tokens 0600).
  New `google status|sync|disconnect` commands, an optional `setup` step and a `google_meet` doctor
  check. A leased poller (`google_meet_poll_minutes`) runs in the gateway only; conferences are
  imported once ever and only those ending after `connect` unless you backfill with `sync --days`.
  Notes go to `google_meet_discord_channel` (then `delivery_discord_channel`, then an automatic
  channel of the server).
- **Full transcript in Discord** (`delivery_discord_transcript`, default on): every meeting's
  transcript is attached as `transcript-<date>-<slug>.md` after the summary, once, split into parts
  above 8 MB, with a notice when the bot cannot attach files.
- Schema v4: meeting `source`/`external_id` (unique), a key/value status table and named leases.
- **Where notes are posted**: channel settings accept an id, `<#id>` or a name (`#meeting-notes`),
  resolved in the server at delivery time; ambiguous or unknown names are reported, never guessed.
  Without a channel, notes go to the server's system channel or the first channel named like
  `delivery_auto_channel_names` (`general`, `meetings`, `meeting-notes`, `notes`, `reuniones`,
  `notas`). New `delivery_discord_guild` picks the server for Meet meetings when the bot is in
  several. With nothing usable the meeting waits (no attempts used, no time limit), `status` and
  `doctor` show the command to run, and `config set` of a channel posts it.
- **Tasks without a project** go to `delivery_fallback_channel` (id or name) when set, for Discord
  and Meet meetings; Meet tasks with a project are routed to that project's channel in the chosen
  server.
- **Models and fallbacks from the plugin**: `hermes meeting-scribe llm show|set|fallback
  add|remove|clear|set|test` read and write Hermes' `auxiliary.meeting_scribe` block (the one its
  auxiliary router uses); `doctor` shows the chain and warns without a fallback. The auxiliary task
  is registered with neutral defaults (Hermes' main model).
- **Analysis robustness**: `analysis_timeout_seconds` (default 600) is a wall clock of the plugin's
  own — a call that never returns fails the attempt into the normal backoff; `analysis_max_tokens`
  (default 8192) is always sent; a non-JSON reply is retried once with a stricter instruction.
- **Configuration schema for UIs**: settings have a group and localized label/help (en/es);
  `config list` shows value, origin and the channel a name resolved to; `config schema --json` is a
  versioned form description including the LLM settings. New settings `delivery_transcript_max_mb`
  and `pipeline_max_attempts`. `plugin.yaml` and the README tables are generated from the settings.

- **Meetings page for Hermes Desktop** (`desktop/plugin.js`, off by default in Capabilities →
  Plugins): library with search, source/status/date filters and cursor pages; meeting detail with
  notes, tasks and their per-destination state and links, paged searchable transcript, recording
  when a mixed track exists, and **Reprocess** (confirmation dialog, queued for the gateway worker,
  followed by polling); status (worker heartbeat, queue, meetings waiting for a channel, Google Meet
  and diagnostics); settings generated from the config schema (groups, en/es labels, validation,
  per-field errors, value origin) and a **Models** editor (main model and ordered fallbacks). Backed
  by a versioned REST API under `/api/plugins/meeting-scribe/v1` (`dashboard/plugin_api.py`) that
  reads SQLite/files and never starts capture or a pipeline. Covered by Node render tests
  (`tests/desktop`) and `plugins validate`; **not yet exercised in a running Desktop**.

### Changed

- **Notes are never posted to Hermes' home channel any more**: it is often a DM with the owner, where
  nobody else sees them and tasks cannot be routed. A channel given by id that turns out to be a DM
  is skipped too.
- Channel settings no longer reject names (they are resolved at runtime); user and role mentions
  are still rejected.
- A Meet meeting without a channel now waits for one instead of being skipped in Discord.
- Speakers that are not Discord users (imported from Meet) are never mentioned or DMed; their name
  is shown instead.
- The worker and the Meet poller start when the gateway loads the plugin, not only when Discord
  connects or a `/meeting` command runs.
- Re-running `google connect` keeps the original connection time, so meetings that ended while
  access was broken are still imported (within Meet's 30 days).
- **Discord messages speak the user's language, not the plugin's**: errors from `/meeting`, buttons
  and `/meeting start` no longer quote the exception (it is logged, and `doctor`/`status` show the
  technical detail); expected problems get their own sentence (task dismissed, notes not ready,
  Kanban/Linear not connected, channel unavailable, not found). Meeting states and stages are
  labelled in plain words in `list`, `status`, `show` and `reprocess` ("✅ Ready", "⏳ Preparing
  notes…", "posting the notes again"); a failed meeting says which step could not finish instead of
  the raw error. `/meeting config` shows setting names instead of keys, `approve all` counts failed
  tasks instead of listing errors, the project picker describes each source ("Kanban board"), the
  DM-notes hint in `reprocess` no longer contains admin commands, and the slash command description
  is plainer.

### Fixed

- **Imported meetings no longer turn into Discord meetings after an older gateway rewrote them.**
  A gateway running pre-v4 code saved a Google Meet import's JSON without `source`/`external_id`;
  the columns kept `google_meet` and the record, but every read used the JSON, so `reprocess`
  would try to transcribe audio that never existed and the Desktop page showed "audio not kept"
  and offered "Transcription". Reads now take `source`/`external_id` from the columns, a save never
  changes them on an existing row (a stale copy cannot degrade it; the JSON and `meta.json` follow
  the columns), and schema v8 repairs stored JSON that disagrees with its columns (idempotent,
  data only). An affected `meta.json` is rewritten the next time the meeting is saved.

- **A recording in which nobody spoke is discarded, not failed.** When someone joined and left
  before speaking (auto-join), the pipeline raised `no audio tracks`, retried three times and left
  the meeting `failed`, cluttering `status`, `/meeting list`, doctor and the Desktop page. Now the
  meeting ends in a new terminal state `empty` ("No audio: discarded" / «Sin audio: descartada»):
  decided by the capture when no person's audio ever arrived, and by the pipeline when there are no
  tracks, no utterances or an empty transcript. No retries, no LLM call, nothing published; the
  job ends `done` without error. The stop message says "No audio was captured; there are no notes
  to prepare." instead of promising notes. `reprocess` of an `empty` meeting is refused (chat,
  CLI exit 1, Desktop). Desktop gets its own `empty` filter/facet and label, and hides the
  reprocess button for it. Tracks, scratch and utterances are removed; the folder with `meta.json`
  stays. Schema v7 reclassifies existing `failed` rows whose job failed at transcribe with
  `TranscriptionError: no audio tracks in …` (idempotent, data only; other failures untouched).

- **Notes left in a DM by an older version are moved only on request and safely**: nothing moves on
  button clicks, 📁 moves or retries; `reprocess <id> --from deliver` moves the meeting to an
  explicitly configured channel (never the automatic one), publishing there first and deleting the
  DM messages last. A channel that cannot be used leaves the DM intact with a clear error; an
  interrupted move resumes without duplicates. A meeting that was not meant to attach its transcript
  (or predates attachments) does not attach it when moving. Without a configured channel the notes
  stay in the DM and `status`, `doctor` and the reprocess reply say what to set.
- **Moving a DM meeting no longer deletes its transcript without re-posting it**: a summary pointer
  written before the `attach` key existed was treated as "never attach", so the transcript that had
  really been delivered in the DM was deleted and not re-posted. The intent is now the explicit
  `attach` when present, else whether a transcript was actually delivered (the legacy marker, or an
  explicit refusal, still means no). The DM copy of each part (summary, transcript, tasks, index) is
  deleted only once its replacement is confirmed; anything unconfirmed stays in the DM, with the
  reason in `status`/`doctor`, and a later `reprocess --from deliver` cleans it once replaced. A
  failed transcript upload during a move fails the delivery (the DM is untouched and the job
  retries) instead of being logged and skipped. A move saved by the faulty build re-derives the
  intent from the DM pointers it holds.
- Notes are never posted in another server: a Discord meeting whose server is not loaded, or a
  `delivery_discord_guild` that does not match, now waits instead of looking a channel name up in
  every server; configured channel ids of another server or a DM are ignored and reported.
- The automatic channel is never NSFW nor hidden from `@everyone` (a private channel is used only if
  configured, and `doctor` says so), and nothing is chosen while the server is still loading.
- Non-ASCII digits (`²`, `١٢٣`) in ids no longer crash the delivery; they are treated as names.
- `llm fallback add/set` require a model (Hermes skips fallbacks without one) and `llm show`/`doctor`
  flag existing ones; editing the chain keeps extra keys of hand-written entries (`key_env`,
  `api_key`, `api_mode`, `transport`, …); URLs are shown without credentials or query string; `llm
  set` with a default value says it uses the default.
- At most two timed-out analysis calls are left running; the next one fails fast with a clear error.
- While a delivery waits for a Discord channel, sinks that already delivered (files, Obsidian,
  Kanban, Linear) are not re-run every two minutes.
- Folder and transcript file names are cut on word boundaries, so a Meet code is kept whole or left
  out (never `…-gmj-bcgo-bq`); the slug limit went from 40 to 60 characters.
- Meet import: one failing conference (403/404/5xx, malformed data) no longer blocks the others; it
  is given up after 5 permanent failures (shown by `doctor`). No exception escapes a sync, so the
  status is always updated and `google sync` never prints a traceback. Conferences without a
  transcript an hour after they end are no longer polled, and a 429 `Retry-After` is honoured.
- `reprocess` of an imported meeting starts at analysis (there is no audio to re-transcribe), and
  recovery never rewinds one to transcription.
- The full transcript is attached only by the delivery step: button clicks never attach it, meetings
  delivered before the attachment existed never get it later, a new title alone does not re-post
  it, and a lost pointer write after an upload no longer causes a duplicate.
- Concurrent opening of an older database no longer fails with "duplicate column name" (migrations
  run under one write lock).
- OAuth: the loopback receiver is not blocked by idle browser connections and ignores redirects
  with a wrong `state`; token refreshes are serialised across processes and never undo a
  `disconnect` or overwrite a newer `connect`; a failed revoke on `disconnect` is reported with the
  link to remove access by hand.
- Meet display names are escaped in Discord (no mentions, no Markdown injection).
- Import crash leftovers are cleaned up and rows without a job are re-queued while running; the
  poller stops promptly and releases its lease from its own thread.
- `setup`, `config set` and `doctor` warn when the Meet import has no notes channel.
- Docs: the Meet API only lists conferences organised by the connected account.

## [0.2.0] - 2026-09-27

### Changed

- **Tasks are posted where the work lives.** Each action item is its own message, with its own
  buttons directly under it, in a thread of its project's Discord channel. The meeting chat keeps the
  summary plus a compact task index (per project with thread links, per person) and one
  **📋 My tasks** button. Handling a task edits only that task's message and the index counts.
- **A project per task** instead of per meeting. The guild's text channels and categories are project
  candidates. Channel names are cleaned generically (emoji, symbols, separators and decorative
  brackets are removed by Unicode category), and matching is token-aware and tolerant of
  transcription errors.
- **Per-task authorization.** Only a task's assignee and the owners may act on it; anyone else is told
  whose task it is. Kanban appears only on the owners' own tasks. Unassigned tasks are owners-only.

### Added

- **📋 My tasks:** an ephemeral, paginated panel with the clicker's tasks and their buttons. Owners
  can switch to all tasks.
- **Assignee DMs** (`delivery_dm_assignees`, default on). Closed DMs are noted in the index and never
  fail the delivery.
- **📁 Move** re-posts a task in another channel's thread, deletes the old message and learns the
  mapping. Uncertain matches are posted in the most probable channel with a ⚠️ warning.
- A 📁 move by an assignee pins only that task; an owner's move also teaches routing. Moves survive
  re-analysis.
- New settings: `delivery_project_threads`, `delivery_dm_assignees`, `project_channels`,
  `project_match_min_score`, `channel_name_ignore_prefixes`.
- Schema v3: learned project → channel map and per-task overrides.

### Fixed

- Buttons were listed after all tasks and lost their alignment as soon as one task was handled.
- Reprocessing a meeting whose analysis dropped a task left the old task message behind.

## [0.1.0] - 2026-09-27

First public release.

### Added

- Per-speaker Discord voice capture through Hermes' Discord adapter (DAVE-aware). Each participant is
  written as an Ogg/Opus track aligned to the meeting start. Supports auto join/leave, a duration
  cap, a consent announcement and a `[REC] ` nickname prefix.
- Durable SQLite job queue with the stages `transcribe → analyze → deliver → archive`: retries with
  backoff, resume after restart, and `reprocess` from any stage.
- Local transcription with faster-whisper in a low-priority subprocess (CPU or CUDA), with
  hallucination filters and word timestamps.
- Analysis through the user's Hermes LLM (`ctx.llm`, auxiliary task `meeting_scribe`): JSON-schema
  output, map-reduce for long meetings, prompt-injection guard.
- Automatic project resolution across Hermes projects, Kanban boards and Linear projects, with a
  learned channel → project map.
- Delivery sinks: meeting folder (Markdown + JSON), Discord thread with approval buttons, Hermes
  Kanban, Linear (GraphQL API key or an allowlisted MCP server) and Obsidian. All sinks are
  idempotent.
- `recording.mka` archive: a mixed stream plus one stream per speaker.
- `/meeting` slash command (aliases `/meet`, `/rec`), the `hermes meeting-scribe` CLI (`setup`,
  `doctor`, `status`, `list`, `show`, `reprocess`, `export`, `config`), the agent tools
  `meeting_search` / `meeting_get` and a bundled skill.
- English and Spanish UI.

### Fixed (found during the real end-to-end run before release)

- Whisper segments that spanned another speaker's turn put sentences out of order in the transcript.
  They are now split on word gaps.
- The LLM sometimes omitted decisions, open questions and due dates. The instructions now spell out
  the full output shape.
- Re-analysis created duplicate Kanban/Linear tasks when the LLM rephrased a title. Ids are now
  reconciled with the previous analysis.

### Known limitations

- Not yet exercised in a live Discord call. Capture is covered by unit tests and by integration tests
  against the real Hermes Discord adapter, and the processing pipeline was verified end to end with
  real speech, faster-whisper, the Hermes LLM and Kanban.
- The first ~100 ms of a new speaker can be dropped (Discord/DAVE stream mapping).

[0.2.0]: https://github.com/propiter/hermes-meeting-scribe/releases/tag/v0.2.0
[0.1.0]: https://github.com/propiter/hermes-meeting-scribe/releases/tag/v0.1.0
