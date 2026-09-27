# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Google Meet import** (opt-in, `google_meet_enabled`): transcripts that Meet already generated are
  imported and processed like Discord meetings (analysis, tasks per project, Kanban/Linear, Discord).
  Each user connects their own Google OAuth client with `hermes meeting-scribe google connect`
  (PKCE, loopback or `--no-browser` paste, read-only scope `meetings.space.readonly`, tokens 0600).
  New `google status|sync|disconnect` commands, an optional `setup` step and a `google_meet` doctor
  check. A leased poller (`google_meet_poll_minutes`) runs in the gateway only; conferences are
  imported once ever and only those ending after `connect` unless you backfill with `sync --days`.
  Notes go to `google_meet_discord_channel` (then `delivery_discord_channel`, then home).
- **Full transcript in Discord** (`delivery_discord_transcript`, default on): every meeting's
  transcript is attached as `transcript-<date>-<slug>.md` after the summary, once, split into parts
  above 8 MB, with a notice when the bot cannot attach files.
- Schema v4: meeting `source`/`external_id` (unique), a key/value status table and named leases.

### Changed

- Speakers that are not Discord users (imported from Meet) are never mentioned or DMed; their name
  is shown instead.
- The worker and the Meet poller start when the gateway loads the plugin, not only when Discord
  connects or a `/meeting` command runs.
- Re-running `google connect` keeps the original connection time, so meetings that ended while
  access was broken are still imported (within Meet's 30 days).

### Fixed

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
- Discord channel ids are validated (`config set` rejects non-numeric ids, `doctor` reports them),
  and `setup`, `config set` and `doctor` warn when the Meet import has no notes channel.
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
