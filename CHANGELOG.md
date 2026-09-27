# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

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

[0.1.0]: https://github.com/propiter/hermes-meeting-scribe/releases/tag/v0.1.0
