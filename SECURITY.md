# Security policy

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through
[GitHub Security Advisories](https://github.com/propiter/hermes-meeting-scribe/security/advisories/new).
You should get an acknowledgement within 7 days.

## Supported versions

Only the latest release gets security fixes.

## What the plugin touches

- **Audio**: recorded and stored locally under `<HERMES_HOME>/plugin-data/meeting-scribe/`. It is
  never uploaded.
- **Transcript text**: sent to the LLM provider configured in Hermes (through `ctx.llm`).
  Transcripts are wrapped as untrusted data, and the model is told to ignore instructions inside
  them.
- **Linear**: when enabled, calls `api.linear.app` with `LINEAR_API_KEY`, or uses an MCP server that
  you explicitly allowlisted.
- **Hermes Kanban**: creates tasks in the profile's Kanban database.
- **Secrets**: read only from the Hermes environment (`.env`). They are never logged or written to
  meeting files.

Recording people can have legal consequences. See the *Privacy and consent* section of the README.
