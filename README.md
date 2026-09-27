# meeting-scribe

> A Hermes Agent plugin that records Discord voice meetings, transcribes them **on your machine**, and turns them into notes, decisions and tasks.

[Español](README.es.md) · [Design](docs/DESIGN.md) · [Changelog](CHANGELOG.md)

`meeting-scribe` joins a Discord voice channel and records **each participant on a separate track**,
so every line of the transcript is attributed to the right person. It transcribes locally with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper). It then sends the transcript text to
**the LLM you already configured in Hermes**, which extracts a summary, decisions, open questions and
action items. The results go to Markdown files, a Discord thread with approval buttons, the Hermes
Kanban board, Linear and your Obsidian vault.

Audio never leaves your machine. Only transcript text is sent, and only to your own LLM provider.

## Features

- **Per-speaker recording.** Each participant gets their own Ogg/Opus track, aligned to a common
  timeline, so there is no diarization guesswork.
- **Local transcription.** faster-whisper runs in a low-priority subprocess (the gateway never
  blocks), on CPU or CUDA, with hallucination filters.
- **Notes from your Hermes LLM:**
  - TL;DR, summary and topics
  - decisions and open questions
  - action items with owner, verbatim quote and timestamp
  - a due date, but only when one was said out loud
- **Automatic project detection.** The LLM picks from your Hermes projects, Kanban boards and Linear
  projects, and remembers which channel belongs to which project.
- **Delivery targets.** The meeting folder (always), a Discord thread with approval buttons, Hermes
  Kanban, Linear and Obsidian.
- **Durable and idempotent.** Every meeting is a resumable state machine stored in SQLite:
  - a restart resumes unfinished work
  - reprocessing never duplicates a task, an issue or a message
- **Auto join and leave.** The bot joins when people gather in a voice channel and stops when the
  channel empties.
- **Agent tools.** `meeting_search` and `meeting_get` let you ask Hermes things like *"what did we
  decide about the SMTP migration?"*.
- **One file per meeting.** `recording.mka` holds a playable mix plus one stream per speaker.
- **Languages.** The bot's messages are in English or Spanish (`ui_language`). Notes can be written
  in any language.

## How it works

```
 Discord voice channel
        │  RTP/Opus (decrypted, including DAVE, by Hermes' Discord adapter)
        ▼
 ┌──────────────┐   tracks/<user>.ogg (one per speaker, aligned to meeting t0)
 │   capture    │──────────────────────────────────────────────┐
 └──────────────┘                                              ▼
                                        ┌──────────────────────────────────────┐
 SQLite job queue (resumable)  ───────▶ │ transcribe  faster-whisper subprocess │
                                        └──────────────────┬───────────────────┘
                                                           ▼ transcript.jsonl / .md
                                        ┌──────────────────────────────────────┐
                                        │ analyze     Hermes LLM (ctx.llm)      │
                                        │             map-reduce, JSON schema   │
                                        └──────────────────┬───────────────────┘
                                                           ▼ notes.json / notes.md / tasks.json
                                        ┌──────────────────────────────────────┐
                                        │ deliver     files · Discord · Kanban  │
                                        │             Linear · Obsidian         │
                                        └──────────────────┬───────────────────┘
                                                           ▼
                                        ┌──────────────────────────────────────┐
                                        │ archive     recording.mka (mix + one  │
                                        │             stream per speaker)       │
                                        └──────────────────────────────────────┘
```

A meeting moves through these states: `recording → captured → transcribing → transcribed →
analyzing → analyzed → delivering → done`. A failed stage is retried automatically with backoff.
Once the retries are used up, `reprocess` resumes from that stage.

## Requirements

| | |
|---|---|
| Hermes Agent | **>= 0.21**, with the Discord gateway configured (`hermes gateway`) |
| Python | whatever your Hermes runs on (3.11 – 3.14) |
| ffmpeg + ffprobe | must be built **with libopus**. Searched on `PATH`, then `~/.hermes/tools/ffmpeg-*/bin`, then `audio_ffmpeg_path` |
| faster-whisper | `>=1.1,<2`. Hermes installs it when you grant dependency consent at install time |
| Disk | ~20 MB per speaker-hour at 48 kbps, plus the whisper model (from 75 MB for `tiny` to 3 GB for `large-v3`) |
| OS | Linux, macOS |

### Whisper model: speed vs accuracy

These numbers come from a real end-to-end run: a 114-second Spanish meeting with 3 speakers
(343 s of speech across the speaker tracks). It ran on **CPU only** (AMD Ryzen 9 270, 14 threads,
int8), with the language pinned to `es`. *Word match* is the share of words that match the script
the speakers read aloud.

| Model | Transcription time | ≈ per meeting minute | Word match | Suggested use |
|---|---|---|---|---|
| `tiny` | 7.5 s | 4 s | 94.7 % | quick drafts |
| `base` | 11.5 s | 6 s | 97.7 % | |
| `small` | 20.5 s | 11 s | 98.9 % | fast CPU choice |
| `medium` (default) | 49.5 s | 26 s | 99.2 % | best balance on CPU |
| `large-v3` | not measured | more than `medium` | | use with a CUDA GPU |

Transcription time grows with the **amount of speech on the speaker tracks**. Participants who stay
silent add almost nothing. With a CUDA GPU (`transcribe_device: cuda`,
`transcribe_compute_type: float16`), `large-v3` becomes practical. Pin `transcribe_language` if
you can, because auto-detection can pick the wrong language on short segments.

## Install

```bash
hermes plugins install propiter/hermes-meeting-scribe --enable
hermes meeting-scribe setup      # language, model, notes channel, owners, auto-join, Kanban/Linear, Obsidian
hermes meeting-scribe doctor     # checks ffmpeg/libopus, whisper, storage, LLM, Kanban, Linear, Discord
hermes gateway restart           # load the plugin into the running gateway
```

Hermes asks for consent before it installs the Python dependency (`faster-whisper`). If you install
from a non-interactive shell and the plugin is left disabled, run
`hermes plugins enable meeting-scribe`, which installs the dependency and enables the plugin.

`setup` can also run without prompts, for example:

```bash
hermes meeting-scribe setup --non-interactive --language es --model small --kanban-mode approve
```

### Discord bot

The plugin reuses the bot that Hermes' Discord adapter already runs. It needs no token of its own.

- **Intents.** Hermes already enables `voice_states` (not privileged). Hermes itself needs
  **Message Content** (privileged). The plugin needs nothing extra.
- **Permissions** in the channels you record:
  - required: **View Channel, Connect, Send Messages, Create Public Threads**
  - optional: **Manage Nicknames**, for the `[REC] ` nickname prefix
  - *Speak* is not needed
- **`doctor` checks** the capture compatibility probe (`discord_compat`), voice dependencies
  (PyNaCl, davey, libopus), intents and permissions.

## Usage

### Slash commands

The main command is **`/meeting`**. Its aliases **`/meet`** and **`/rec`** can be changed with
`commands_aliases`. The plugin cannot use `/start` or `/stop`, because Hermes reserves them as
built-ins.

| Subcommand | What it does |
|---|---|
| `start [#voice-channel]` (the default when run with no arguments) | Joins your voice channel, or the one given, and starts recording |
| `stop` | Stops recording and starts processing |
| `status` | Shows recording and processing state, plus the queue |
| `list [n]` | Lists recent meetings |
| `show <id>` | Shows a meeting's notes |
| `search <text>` | Full-text search across transcripts |
| `reprocess <id> [from=transcribe\|analyze\|deliver]` | Re-runs a meeting from a stage |
| `link @user <linear-email-or-name>` | Maps a Discord user to a Linear user |
| `project <id> <project>` | Sets or corrects a meeting's project, and teaches the channel → project map |
| `config` | Shows the effective configuration |
| `help` | Shows usage |

**Auto join and leave.** When `autojoin_enabled` is on, the bot joins a voice channel once
`autojoin_min_humans` people have stayed there for `autojoin_grace_seconds`. It stops recording in
any of these cases:

- nobody human has been in the channel for `autoleave_grace_seconds`
- the recording reaches `limits_max_duration_minutes`
- someone runs `/meeting stop`

After a manual stop or a duration-limit stop, auto-join does not rejoin that channel until it has
emptied.

### CLI

```text
hermes meeting-scribe setup [--non-interactive] [--language CODE] [--model NAME] [--notes-channel ID]
                            [--owners ID,ID] [--autojoin | --no-autojoin]
                            [--retention multitrack|mixed|none] [--kanban-mode approve|auto|off]
                            [--linear-mode approve|auto|off] [--linear-team KEY] [--obsidian-vault PATH]
hermes meeting-scribe doctor [--json]
hermes meeting-scribe status [--json]
hermes meeting-scribe list [-n 20]
hermes meeting-scribe show <id>
hermes meeting-scribe reprocess <id> [--from transcribe|analyze|deliver] [--now]
hermes meeting-scribe export <id> [--format md|json] [--out FILE]
hermes meeting-scribe config get [KEY]
hermes meeting-scribe config set KEY VALUE
```

By default, `reprocess` queues the work for the gateway's worker. Add `--now` to process it in the
CLI process instead. Meeting ids can be shortened to any unique prefix.

### Ask the agent

The `meeting_scribe` toolset provides two tools:

- `meeting_search(query, limit)`
- `meeting_get(meeting_id, part=notes|transcript|tasks|meta)`

The bundled skill `meeting-scribe` teaches the agent how to answer questions with them.

## Configuration

Settings live in the profile's `config.yaml`, under `plugins.entries.meeting-scribe.settings`. You
can change them in three ways:

- the plugin settings form in Hermes Desktop
- `hermes meeting-scribe config set KEY VALUE`
- `hermes meeting-scribe setup`

Settings are re-read on every operation, so changes need no restart (except `commands_aliases`). An
invalid value falls back to its default, and `doctor` reports it as a warning.

| Key | Type | Default | Description |
|---|---|---|---|
| `commands_aliases` | list | `[meet, rec]` | Extra slash-command names routed to /meeting (read when the gateway starts). |
| `autojoin_enabled` | bool | `true` | Join a voice channel automatically when people gather. |
| `autojoin_min_humans` | int | `2` | Humans required in a voice channel to auto-join. |
| `autojoin_grace_seconds` | int | `20` | Seconds the channel must stay populated before joining. |
| `autojoin_channels` | list | `[]` | Voice channel ids/names allowed for auto-join (empty = all). |
| `autojoin_ignore_channels` | list | `[]` | Voice channel ids/names never auto-joined. |
| `autoleave_grace_seconds` | int | `60` | Seconds with no humans before the recording stops. |
| `limits_max_duration_minutes` | int | `240` | Hard cap on a single recording. |
| `audio_retention` | str | `multitrack` | Audio kept after processing: `multitrack` / `mixed` / `none`. |
| `audio_bitrate_kbps` | int | `48` | Opus bitrate per speaker track. |
| `audio_ffmpeg_path` | str | `""` | ffmpeg binary used when none is found on `PATH` or in `~/.hermes/tools`. |
| `transcribe_model` | str | `medium` | faster-whisper model (tiny/base/small/medium/large-v3...). |
| `transcribe_device` | str | `auto` | Inference device: `auto` / `cpu` / `cuda`. |
| `transcribe_compute_type` | str | `auto` | CTranslate2 compute type: `auto` / `int8` / `int8_float16` / `float16` / `float32`. |
| `transcribe_cpu_threads` | int | `0` | CPU threads for whisper (0 = cores minus 2). |
| `transcribe_language` | str | `auto` | Spoken language code (`auto` = detect; pin it if you can). |
| `transcribe_beam_size` | int | `5` | Beam size for decoding. |
| `analysis_language` | str | `auto` | Notes language (`auto` = transcript language). |
| `analysis_chunk_chars` | int | `12000` | Transcript chunk size for map-reduce analysis. |
| `projects_min_confidence` | float | `0.6` | Minimum confidence to auto-assign a project. |
| `delivery_discord_enabled` | bool | `true` | Post notes to Discord. |
| `delivery_discord_channel` | str | `""` | Notes channel id (empty = voice text chat, then the Hermes home channel). |
| `delivery_discord_thread` | bool | `true` | Post notes in a thread when possible. |
| `delivery_project_threads` | bool | `true` | Post each task in a thread of its project's channel. |
| `delivery_dm_assignees` | bool | `true` | DM each assignee their tasks with buttons after delivery. |
| `project_channels` | list | `[]` | Explicit project → channel map, entries like `Project name=channel_id`. |
| `project_match_min_score` | float | `0.8` | Minimum fuzzy score to route a task to a channel by name. |
| `channel_name_ignore_prefixes` | list | `[]` | Decorative leading words ignored in channel names. |
| `owners` | list | `[]` | Discord user ids whose tasks may go to Kanban (empty = first `DISCORD_ALLOWED_USERS` entry). |
| `kanban_mode` | str | `approve` | Kanban delivery of owner tasks: `approve` / `auto` / `off`. |
| `kanban_board` | str | `""` | Kanban board slug (empty = default board). |
| `linear_mode` | str | `approve` | Linear issue creation: `approve` / `auto` / `off`. |
| `linear_default_team` | str | `""` | Linear team key/id used when no project resolves. |
| `obsidian_vault_path` | str | `""` | Obsidian vault path (empty = disabled). |
| `obsidian_folder` | str | `Meetings` | Folder inside the vault for notes. |
| `ui_language` | str | `en` | Language of bot messages: `en` / `es`. |
| `consent_announce` | bool | `true` | Announce the recording in the channel chat. |
| `consent_nickname_prefix` | str | `"[REC] "` | Nickname prefix while recording (empty = off). |

**Using a different model for analysis.** Meeting analysis runs as the Hermes auxiliary task
`meeting_scribe`. To send it to a different model than your chat model, configure
`auxiliary.meeting_scribe.*` in `config.yaml`, or pick a model for "Meeting Scribe" in Desktop's
auxiliary-model settings.

## Integrations

### Hermes Kanban

Only action items owned by one of the **owners** become Kanban tasks. The owners are the ids in
`owners`, or the first `DISCORD_ALLOWED_USERS` entry when `owners` is empty. Each task is created
in **triage**, and its body includes the meeting folder, the quote and the timestamp.

**Modes:**

- `kanban_mode: approve` (the default): approve tasks with ✅ or *Approve all → Kanban* under the
  notes in Discord.
- `kanban_mode: auto`: tasks are created as soon as the meeting is processed.

**Where each task goes:**

- If the meeting resolves to a **Hermes project**, the task goes to that project's board and gets
  its `project_id`.
- If it resolves to a **Kanban board**, that board is used directly.
- Otherwise, the task goes to `kanban_board`, or to the default board if that is empty.

**Idempotent.** Every task carries an idempotency key, `mtg:<meeting>:<item>`. If a re-analysis
rephrases an item, the plugin matches it back to its previous id, so no duplicate task appears.

### Linear

Linear turns on when either of these is available:

- `LINEAR_API_KEY` in the profile's `.env` (uses the GraphQL API)
- a Hermes MCP server named `linear` that you allowlist for this plugin:

  ```yaml
  plugins:
    entries:
      meeting-scribe:
        mcp_allowlist: [linear]
  ```

Each issue is filed under the resolved Linear project or team, falling back to
`linear_default_team`. The assignee comes from `/meeting link` mappings, or from matching Discord
display names against Linear users. The description includes the quote and a reference to the
meeting.

The modes work the same way as Kanban: in `approve` mode, use the 🟣 button. When Linear is not
connected, the plugin skips it silently and `doctor` reports that.

### Obsidian

Set `obsidian_vault_path`, and every `notes.md` is copied to
`<vault>/<obsidian_folder>/<meeting-folder>.md`. The notes carry YAML frontmatter with the title,
date, participants, project and tags.

## Storage layout

```
<HERMES_HOME>/plugin-data/meeting-scribe/
  index.sqlite                 meetings, speakers, utterances (FTS5), jobs, deliveries,
                               action items, links, channel→project map
  meetings/YYYY/MM/<YYYY-MM-DD_HHMM>_<channel>_<id>/
    meta.json                  meeting metadata
    transcript.jsonl           one utterance per line (t0, t1, speaker, text, words, confidence)
    transcript.md              readable transcript
    notes.json / notes.md      structured and Markdown notes (Obsidian-compatible frontmatter)
    tasks.json                 action items and their delivery status
    recording.mka              multitrack retention: stream 0 = mix, then one stream per speaker
    recording.ogg              mixed retention
```

Storage is separate for each Hermes profile. `reprocess --from transcribe` extracts the speaker
streams back out of `recording.mka`.

## Privacy and consent

> **Recording people without their knowledge may be illegal where you or they live.** Many
> jurisdictions require consent from **every** participant (all-party consent laws; GDPR in the EU).
> **You** are responsible for telling participants they are being recorded, and for getting their
> consent before you record.

The plugin helps you do this:

- `consent_announce` posts a notice in the voice channel's text chat when a recording starts.
- `consent_nickname_prefix` shows `[REC] ` in the bot's nickname while it records.
- Audio stays on your disk. Transcript **text** is sent to the LLM provider you configured in
  Hermes, so choose a provider whose data policy suits your meetings.
- To delete a meeting, remove its folder. `audio_retention: none` deletes the audio once processing
  finishes.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/meeting start` says capture is not compatible | Hermes' Discord adapter changed internals the plugin relies on. `doctor` lists the failing checks under `discord_compat`. Update the plugin, or run a Hermes version it supports. |
| "I'm already connected to a voice channel in this server" | Discord allows **one voice connection per server for each bot**. Run `/voice leave` (Hermes voice chat) first. |
| `/meeting start` fails from the CLI or TUI | Live capture only works from Discord, because it needs the gateway's Discord connection. |
| The first word from a new speaker is missing | Known Discord/DAVE limitation: a new speaker's audio is dropped until Discord maps their stream, which takes about 100 ms. |
| Wrong language or misheard words | Pin `transcribe_language`, and try a larger `transcribe_model`. |
| A meeting is stuck or failed | `hermes meeting-scribe status` shows the stage and the error. Then run `hermes meeting-scribe reprocess <id> --from <stage>`. |
| ffmpeg is found but reports no libopus | Install an ffmpeg build that includes libopus. `doctor` shows which binary it picked. |

## Limitations

- Live capture works on Discord only. Processing, search and the agent tools work on any Hermes
  surface.
- Each Discord server can have one recording at a time. Several servers can record at once.
- The first ~100 ms from a new speaker can be lost (see Troubleshooting).
- Transcription quality depends on the whisper model and on each speaker's microphone.
- CPU transcription with `medium` takes about 25–30 s per meeting minute with three active speakers
  on a modern 16-thread CPU. Plan for this with long meetings.
- This release has not yet been tested in a live Discord call. The capture path is covered by unit
  and integration tests against the real Hermes adapter (see [CHANGELOG](CHANGELOG.md)).

## Development

```bash
uv venv -p 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q                 # unit tests (no Hermes needed)
.venv/bin/python -m pytest -q -m slow         # real faster-whisper (downloads the tiny model)
scripts/test-integration.sh -q                # against a real Hermes checkout (HERMES_SRC, HERMES_PYTHON)
hermes plugins validate .                     # manifest, capability probe, security scan
.venv/bin/python scripts/gen_manifest.py      # regenerate plugin.yaml after changing config.py
```

The code follows a hexagonal architecture: `domain/` has no third-party imports. Development is
test-first. Read [docs/DESIGN.md](docs/DESIGN.md) before changing behaviour, and
[CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## Credits

The design is inspired by [Parley](https://github.com/SakethKanchi/parley) by Saketh Kanchi (ISC
license), which also records Discord per speaker and feeds local Whisper and an LLM summary. No code
was copied: `meeting-scribe` is a Hermes plugin written from scratch.

## License

[MIT](LICENSE) © Pedro Rodriguez (propiter)
