# Roadmap

This document records planned work, not implemented capabilities or release promises.

## Future release — Native Hermes Desktop meeting library

**Status:** researched and deferred by the maintainer. No implementation or production deployment authorized by this roadmap. Version number to be chosen when scheduled.

### Goal

Ship a native **Meetings** page inside Hermes Desktop as part of this public plugin. Reuse the existing recording, transcription, analysis and task services. Keep the product generic: no server-specific names, channel prefixes, identities or routing assumptions.

### Proposed experience

- Sidebar entry **Meetings**, with a searchable meeting list and selected-meeting detail.
- Library filters: date, channel, project and processing state.
- Detail sections: summary, decisions, open questions, speaker-attributed timestamped transcript, tasks and processing status.
- Tasks show assignee, project, citations and independent Kanban/Linear delivery states. Link to the real destination; do not build a second Kanban.
- Explicit actions for approval, dismissal, project correction and reprocessing, reusing the domain services and delivery ledger.
- Visual settings for existing configuration, with a single source of truth rather than separate UI settings.
- Later: authenticated audio playback with timestamp seeking and original multitrack download, conditional on a successful compatibility spike.

### Verified extension mechanisms

Research inspected Hermes `28e6496a5` and meeting-scribe `a9376ad` (v0.2.0). These are research baselines, not supported-version guarantees; recheck contracts before implementation.

- **Kanban** is the reference for a page, sidebar navigation and data access: `apps/desktop/src/plugins/kanban/plugin.tsx`, `api.ts`.
- **Bots** is a bundled plugin in the inspected version: `apps/desktop/src/plugins/hermes-bots/plugin.tsx`. Borrow pane lifecycle patterns, not its bot/session management.
- **Radio** demonstrates runtime ESM distribution, SDK controls, i18n and disposal: `apps/desktop/src/plugins/radio/plugin.js`.
- Public frontend imports: `@hermes/plugin-sdk`, `react`, `react/jsx-runtime`. Do not import private Kanban components or Hermes internal stores.
- Public extension areas include `ROUTES_AREA`, `SIDEBAR_NAV_AREA`, panes and command palette. Use native list/detail components, controls, theme variables and plugin i18n.
- In the inspected router, contributed paths are exact matches. Start with `/meetings` and internal selection; do not assume `/meetings/:id` dynamic routing.
- Distributed `desktop/plugin.js` must be runtime-loadable JavaScript ESM. Bundle source modules if needed; raw TSX/JSX and arbitrary imports do not work. Do not assume new Tailwind classes are compiled by the host.
- Desktop and Web Dashboard have different frontend SDKs. Implement Desktop first; a web frontend is separate future scope.
- A catalog listing is **not required** for a visual plugin installed from GitHub. Catalog admission is an independent distribution task.

### Package and backend design

Keep `plugin.yaml` and `meeting_scribe/` as the existing manifest and domain/services/storage. Add these paths at repository root:

```text
desktop/plugin.js
dashboard/manifest.json         # declares api: plugin_api.py
dashboard/plugin_api.py         # exports FastAPI APIRouter
```

Hermes mounts the router under `/api/plugins/meeting-scribe/`. Proposed flow:

`SDK UI → authenticated API/policy → query DTOs or MeetingService → Repository/artifacts/sinks`

Proposed API (not implemented), under a versioned `/v1` namespace:

- `GET /meetings`: stable cursor pagination and filters.
- `GET /search`: transcript search with speaker/time context.
- `GET /meetings/{id}`: detail and available artifacts/actions.
- `GET /meetings/{id}/transcript`: paginated utterances, preserving timestamps.
- `GET /meetings/{id}/tasks`: current database and per-sink delivery state.
- `GET /status`: durable processing state and sanitized errors.
- Explicit validated POST actions for approvals, dismissal, moves and reprocessing.
- Audio/download endpoints by validated meeting ID, never arbitrary filesystem paths.

Reuse `Repository`, `MeetingService`, the existing FTS index and per-item/per-sink ledger. Do not use `MeetingTools.get` as an HTTP DTO: it truncates transcripts and exposes filesystem paths. Do not treat `tasks.json` as the current approval state.

### Constraints and gaps to resolve

1. **Process separation:** Desktop/serve and Discord gateway may run separately. A read request must not instantiate capture or start another pipeline. Resolve services by profile explicitly; the current `RUNTIMES` context identity is not an HTTP service registry.
2. **Live status:** local `worker_running` is not proof of gateway health. Design durable heartbeat/status semantics before presenting a live indicator.
3. **Updates:** `broadcast_plugin_event` is process-local; gateway events do not automatically reach Desktop. `ctx.socket` is a no-op on OAuth remote connections. Use host React Query polling/cache invalidation initially; evaluate a durable outbox later.
4. **Profile isolation:** UI is app-level, data is connection/profile-level. Cache keys and stale-response guards must include connection, profile and meeting ID. Verify active-profile routing; do not assume `ctx.rest` supports an explicit profile override.
5. **Authorization:** initial scope is the authenticated Hermes operator, not an external participant portal. `MeetingService` itself does not authorize callers and meetings lack ACLs. Add explicit HTTP policy; never trust a user ID supplied in request JSON. Sharing with Discord participants requires a separate identity/ACL design.
6. **Audio:** current multitrack `.mka` is not accepted by Hermes' generic stream extension allowlist. Browser decoding/seek is unproven. Preserve the original and investigate a private derived Ogg/WebM mix. Test MIME, Range/HEAD, authentication and revocation. Do not load long recordings as JSON/base64 or place credentials in URLs.
7. **Activation:** Python/backend enablement and Desktop visual enablement are distinct. The frontend is materialized locally; installing on a remote gateway does not necessarily install its visual half locally. Verify whether adding backend routes requires restarting the relevant serve process; visual hot reload does not prove backend hot reload.
8. **Install consent:** the inspected installer still has a non-TTY Python dependency consent limitation. A visual page does not fix that. Recheck upstream status before documenting the install path.
9. **Data scale:** current meeting-list API lacks full pagination/filtering. Add bounded queries and avoid loading entire long transcripts into the renderer.

### Delivery sequence and acceptance gates

1. **Library and read-only detail:** native page/sidebar, search, pagination, summary and transcript. Verify empty/loading/error states, long transcripts, theme/i18n, connection/profile switches and plugin disable/dispose.
2. **Tasks:** expose existing actions with authorization, conflict handling and idempotency. Verify per-destination approval independence and concurrent Discord/Desktop actions.
3. **Settings and processing:** reuse existing configuration, expose durable status and explicit reprocessing controls; prove opening the UI cannot start duplicate workers.
4. **Audio spike, then playback:** prove real authenticated playback and seek in the supported Desktop environment before including it in release scope.

Use strict TDD, isolated homes/databases, neutral fixtures, fresh review and a real Desktop smoke test. Do not deploy to a live bot without explicit maintainer approval. Existing Discord behavior must remain functional without installing/enabling the visual half.

### Sources and code navigation

Official references (recheck when implementation starts):

- https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk
- https://hermes-agent.nousresearch.com/docs/user-guide/features/extending-the-dashboard
- https://hermes-agent.nousresearch.com/docs/llms.txt

Hermes implementation anchors:

- `apps/desktop/src/sdk/index.ts` — public components and SDK.
- `apps/desktop/src/sdk/runtime-loader.ts` — runtime imports/loading (locate by symbol if moved).
- `apps/desktop/src/api/plugins.ts` — REST/socket transport and remote limits.
- `apps/desktop/electron/desktop-plugins-root.ts` — unified desktop-half materialization (locate `materializeDesktopHalf` if moved).
- `hermes_cli/web_server_dashboard.py::_mount_plugin_api_routes` — backend router mounting.
- `hermes_cli/plugin_events.py` — process-local event boundary.
- `web_routers/files.py` — streaming and media-extension restrictions.

Research-only findings; no frontend or HTTP endpoints were implemented as part of this planning work.
