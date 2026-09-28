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
- **A project per task.** Each action item gets its own project. The LLM picks from your Hermes
  projects, Kanban boards, Linear projects and the Discord server's own channels and categories, and
  fuzzy matching absorbs transcription errors. A 📁 correction is remembered.
- **Tasks where the work lives.** Each task is posted in a thread of its project's channel, with its
  own buttons directly under it. The meeting chat gets the summary and a compact task index. Each
  assignee gets a DM with their tasks, and **📋 My tasks** opens a private panel.
- **Delivery targets.** The meeting folder (always), Discord, Hermes Kanban, Linear and Obsidian.
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
Once the retries are used up, `reprocess` resumes from that stage. A recording in which nobody
spoke ends as `empty` ("No audio: discarded"): not an error, no retries, nothing published, and
nothing to reprocess.

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

### Use Meetings from any profile

Hermes profiles are separate agents, and each one only loads the plugins in its own `plugins/`
folder. Meetings is still installed **once**: one profile is the **owner** and runs the bot
(recording, processing, Google Meet) and keeps the meetings, settings and Google connection. Any
other profile where you turn Meetings on only shows the Desktop page with the owner's meetings: it
starts no second bot and writes nothing of its own.

1. Keep one real copy (normally in the owner profile, where `hermes plugins install` put it) and
   say which profile owns it, in the default profile's `config.yaml` (`~/.hermes/config.yaml`):

   ```yaml
   plugins:
     entries:
       meeting-scribe:
         owner_profile: team        # the profile with the Discord bot ("default" for the default one)
   ```

   Without this line the owner is the profile that holds the real copy.

2. In every other profile that should show the page, link that copy and turn it on:

   ```bash
   ln -s ~/.hermes/profiles/team/plugins/meeting-scribe ~/.hermes/plugins/meeting-scribe          # default
   ln -s ~/.hermes/profiles/team/plugins/meeting-scribe ~/.hermes/profiles/work/plugins/meeting-scribe
   hermes plugins enable meeting-scribe
   hermes -p work plugins enable meeting-scribe
   ```

   A link, not a second install: two copies of the same plugin break Hermes' shared dependency
   environment ("two workspace members are both named hermes-meeting-scribe"); a link is the same
   plugin to it. Update with `hermes -p team plugins update meeting-scribe` only.

   Do this with a release that has this section. `plugins enable` loads the plugin into that
   profile's running gateway right away, and an older release there would start a second bot.

`hermes meeting-scribe doctor` starts with an `owner` line. In a profile that is not the owner, every
`hermes meeting-scribe` command just says which profile to use (`hermes -p team meeting-scribe …`).

### Discord bot

The plugin reuses the bot that Hermes' Discord adapter already runs. It needs no token of its own.

- **Intents.** Hermes already enables `voice_states` (not privileged). Hermes itself needs
  **Message Content** (privileged). The plugin needs nothing extra.
- **Permissions** in the channels you record:
  - required: **View Channel, Connect, Send Messages, Create Public Threads**
  - optional: **Manage Nicknames**, for the `[REC] ` nickname prefix
  - *Speak* is not needed
- **Permissions in a forum** used for notes or tasks: **View Channel, Send Messages** (creates the
  post), **Send Messages in Threads** (everything inside it) and **Attach Files** (the transcript).
  **Manage Threads** is only needed to rename or re-tag a post the bot did not create.
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
hermes meeting-scribe setup [--non-interactive] [--language CODE] [--model NAME] [--notes-channel ID|NAME]
                            [--owners ID,ID] [--autojoin | --no-autojoin]
                            [--retention multitrack|mixed|none] [--kanban-mode approve|auto|off]
                            [--linear-mode approve|auto|off] [--linear-team KEY] [--obsidian-vault PATH]
hermes meeting-scribe doctor [--json]
hermes meeting-scribe status [--json] [--space SLUG]
hermes meeting-scribe list [-n 20] [--space SLUG]
hermes meeting-scribe show <id> [--space SLUG]
hermes meeting-scribe reprocess <id> [--from transcribe|analyze|deliver] [--now] [--space SLUG]
hermes meeting-scribe export <id> [--format md|json] [--out FILE] [--space SLUG]
hermes meeting-scribe config get [KEY] [--space SLUG]
hermes meeting-scribe config set KEY VALUE [--space SLUG]    # with --space: that space's override
hermes meeting-scribe config list [--json] [--group GROUP] [--space SLUG]   # value + origin (+ resolved channel)
hermes meeting-scribe config schema --json [--lang en|es]    # machine-readable form description
hermes meeting-scribe llm show [--json]                      # see "Models and fallbacks"
hermes meeting-scribe llm set [--provider P] [--model M] [--base-url URL] [--timeout S]
hermes meeting-scribe llm fallback add|remove|clear|set ...
hermes meeting-scribe llm test [--json]
hermes meeting-scribe google connect|status|sync|disconnect [--space SLUG]   # see "Google Meet"
hermes meeting-scribe space list|show|create|rename|delete|add-guild|remove-guild|set|unset   # see "Spaces"
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

<!-- config-table:start -->
#### Capture

| Key | Type | Default | Description |
|---|---|---|---|
| `autojoin_enabled` | bool | `true` | Join a voice channel automatically when people gather. |
| `autojoin_min_humans` | int | `2` | Humans required in a voice channel before auto-joining. |
| `autojoin_grace_seconds` | int | `20` | Seconds the channel must stay populated before joining. |
| `autojoin_channels` | list | `[]` | Voice channel ids/names allowed for auto-join (empty = all). |
| `autojoin_ignore_channels` | list | `[]` | Voice channel ids/names never auto-joined. |
| `autoleave_grace_seconds` | int | `60` | Seconds with no humans before the recording stops. |
| `limits_max_duration_minutes` | int | `240` | Hard cap on a single recording. |
| `audio_bitrate_kbps` | int | `48` | Opus bitrate per speaker track. |
| `audio_ffmpeg_path` | str | `""` | Explicit ffmpeg binary (empty = auto-detect). |

#### Transcription

| Key | Type | Default | Description |
|---|---|---|---|
| `transcribe_model` | str | `medium` | faster-whisper model (tiny/base/small/medium/large-v3/turbo). |
| `transcribe_device` | str | `auto` | Where whisper runs. (`auto` / `cpu` / `cuda`) |
| `transcribe_compute_type` | str | `auto` | CTranslate2 compute type. (`auto` / `int8` / `int8_float16` / `float16` / `float32`) |
| `transcribe_cpu_threads` | int | `0` | CPU threads for whisper (0 = cores minus 2). |
| `transcribe_language` | str | `auto` | Language code (auto = detect; pin it if you can). |
| `transcribe_beam_size` | int | `5` | Beam size for decoding. |

#### Analysis

| Key | Type | Default | Description |
|---|---|---|---|
| `analysis_language` | str | `auto` | Language of the notes (auto = transcript language). |
| `analysis_chunk_chars` | int | `12000` | Transcript chunk size for map-reduce analysis of long meetings. |
| `analysis_timeout_seconds` | int | `600` | Wall-clock limit for one LLM call; a call that does not return fails the attempt and is retried with backoff. |
| `analysis_max_tokens` | int | `8192` | Output tokens requested per LLM call, so the provider does not reserve the whole context window (a common cause of 'insufficient credit' errors). |

#### Where notes are posted

| Key | Type | Default | Description |
|---|---|---|---|
| `delivery_discord_enabled` | bool | `true` | Post meeting notes to Discord. |
| `delivery_discord_guild` | str | `""` | Server (id or name) used when a meeting has no server of its own, e.g. Google Meet (empty = the bot's only server). |
| `delivery_discord_channel` | str | `""` | Channel id, <#id> or name for notes (empty = voice chat, then automatic). |
| `delivery_auto_channel_names` | list | `[general, meetings, meeting-notes, notes, reuniones, notas]` | When no channel is set: the server's system channel, else the first of these channel names the bot can post in. |
| `delivery_fallback_channel` | str | `""` | Channel id or name for tasks that match no project channel (empty = the notes channel). |
| `delivery_discord_thread` | bool | `true` | Post tasks without project in a thread under the summary when possible. |
| `delivery_project_threads` | bool | `true` | Post each task in a thread of its project's channel. |
| `delivery_dm_assignees` | bool | `true` | Send each assignee their tasks by DM after delivery. |
| `delivery_discord_transcript` | bool | `true` | Attach the full transcript (Markdown file) to the notes. |
| `delivery_mention_participants` | bool | `true` | The first message of the notes @mentions the people who were in the meeting (once, when the notes are first posted; in a private channel only those who can see it). Never @everyone, @here or roles. |
| `delivery_transcript_max_mb` | int | `8` | Largest file uploaded; longer transcripts are split. Raise it if your server allows bigger uploads. |
| `delivery_forum_tags` | list | `[]` | When notes or tasks go to a forum channel, also apply the forum tags with these names to each meeting post (the tag matching the meeting's project is applied anyway). Max 5 per post; names the forum does not have are ignored. |
| `delivery_forum_default_tag` | list | `[]` | For forums that require a tag on every post: the tag to use when no tag matches the project or the forum post tags (the first name that exists in that forum). Empty = none; the post then waits until a tag is set. |
| `meeting_routes` | list | `[]` | Where the notes of some meetings go, one rule per line: 'origin = #channel'. Origin: a voice channel (name or id), 'category:<name>' for every voice channel of a Discord category, or 'meet:<code>' for Google Meet (* and ? allowed). Add ':private' to keep everything in that channel: tasks are shared with people or project channels only when someone there presses a button. Example: 'Leadership = #leadership-notes:private'. |

#### Projects

| Key | Type | Default | Description |
|---|---|---|---|
| `projects_min_confidence` | float | `0.6` | Minimum confidence to assign a project automatically. |
| `project_channels` | list | `[]` | Explicit entries like 'Project name=channel id'. |
| `project_match_min_score` | float | `0.8` | Minimum fuzzy score to route a task to a channel by its name. |
| `channel_name_ignore_prefixes` | list | `[]` | Decorative leading words ignored in channel names. |

#### Google Meet

| Key | Type | Default | Description |
|---|---|---|---|
| `google_meet_enabled` | bool | `false` | Import Google Meet transcripts (needs `google connect`). |
| `google_meet_poll_minutes` | int | `5` | Minutes between Google Meet polls. |
| `google_meet_discord_channel` | str | `""` | Channel id, <#id> or name for Meet notes (empty = notes channel, then automatic). |

#### Integrations

| Key | Type | Default | Description |
|---|---|---|---|
| `owners` | list | `[]` | Discord user ids whose tasks may go to Kanban (empty = first allowed user). |
| `kanban_mode` | str | `approve` | Kanban delivery of owner tasks. (`approve` / `auto` / `off`) |
| `kanban_board` | str | `""` | Board slug (empty = default board). |
| `linear_mode` | str | `approve` | Linear issue creation. (`approve` / `auto` / `off`) |
| `linear_default_team` | str | `""` | Team key/id used when no project resolves. |
| `obsidian_vault_path` | str | `""` | Vault path (empty = disabled). |
| `obsidian_folder` | str | `Meetings` | Folder inside the vault for notes. |

#### Privacy

| Key | Type | Default | Description |
|---|---|---|---|
| `audio_retention` | str | `multitrack` | Audio kept after processing. (`multitrack` / `mixed` / `none`) |
| `consent_announce` | bool | `true` | Announce the recording in the channel chat. |
| `consent_nickname_prefix` | str | `"[REC] "` | Bot nickname prefix while recording (empty = off). |

#### Processing

| Key | Type | Default | Description |
|---|---|---|---|
| `pipeline_max_attempts` | int | `3` | Failed attempts before a meeting is marked failed (waiting for a destination never counts). |
| `pipeline_workers` | int | `2` | How many meetings are processed in parallel (transcription, summary, delivery). Applies after a gateway restart. |
| `pipeline_max_transcriptions` | int | `1` | Transcription is the heaviest step (CPU or GPU): at most this many run at the same time; the other workers keep summarising and delivering meanwhile. |

#### Interface

| Key | Type | Default | Description |
|---|---|---|---|
| `ui_language` | str | `en` | Language of bot messages. (`en` / `es`) |
| `commands_aliases` | list | `[meet, rec]` | Extra slash-command names routed to /meeting. |
<!-- config-table:end -->

`hermes meeting-scribe config list` shows every setting with its effective value and where it comes
from (`default` / `configured`), plus the channel a name resolved to. `config schema --json` prints
a versioned description of every setting (group, type, bounds, choices, localized label and help)
that a settings screen can render without plugin-specific code.

### Where notes are posted

Notes (summary, task index and transcript) go to the first of these that works:

| Meeting | Order |
|---|---|
| Discord voice meeting | `delivery_discord_channel` → the voice channel's text chat → automatic → waiting |
| Google Meet import | `google_meet_discord_channel` → `delivery_discord_channel` → automatic → waiting |

- **Id or name.** Channel settings accept a channel id, `<#id>` or a name (`#meeting-notes` or
  `meeting-notes`). Names are matched ignoring emoji, case and `-`/`_`/spaces. An ambiguous or
  unknown name is never guessed: `doctor` and `config list` report it.
- **Server.** Meet meetings have no server of their own. The plugin uses the server of a channel id
  you configured, else `delivery_discord_guild` (server id or name), else the bot's only server. If
  the bot is in several servers and none is set, it does not guess. If `delivery_discord_guild` is
  set but does not match, or a Discord meeting's own server is not available to the bot, the meeting
  waits — channel names are never looked up in other servers. A channel id from another server (or
  a DM) is ignored and reported by `doctor`.
- **Automatic.** The server's system channel (if the bot can post and attach files there), else the
  first channel named like `delivery_auto_channel_names` (`general`, `meetings`, `meeting-notes`,
  `notes`, `reuniones`, `notas`) where the bot can post. The automatic choice skips NSFW channels and
  channels `@everyone` cannot see (a private channel is used only if you configure it; `doctor` then
  notes that not everyone sees the notes), and chooses nothing until the server is fully loaded.
- **Never a DM.** The gateway's home channel is not used: it is often a DM, where nobody else sees
  the notes and tasks cannot be routed to project channels.
- **Waiting.** If nothing resolves (including "the bot cannot post in any automatic channel"), the
  meeting waits (no attempts are used, no time limit). `status` and `doctor` show the command to run;
  after `config set` of a channel it is posted on its own. Sinks that already delivered (files,
  Obsidian, Kanban, Linear) are not re-run on every retry while it waits.
- **Tasks.** A task with a project goes to a thread in that project's channel (see "Tasks in
  Discord"), for Meet too. A task without a project goes to `delivery_fallback_channel` if set, else
  under the notes.
- **Forum channels.** Any of these channels (notes, fallback, a project channel) may be a forum or a
  media channel. Each meeting is then **one post** named `<date> · <title>`: the summary (TL;DR,
  decisions, open questions) is its first message, and the transcript, the tasks with their buttons
  and the task index go inside the post. A project forum gets one post per meeting with that
  project's tasks. Re-deliveries and button clicks edit the post; a deleted post is created again.
  Each post gets the forum tags whose name matches the meeting's project or `delivery_forum_tags`
  (up to 5). If the forum requires a tag and none matches, `delivery_forum_default_tag` is used; with
  none, the delivery waits and `status`/`doctor` say which setting to change — it is never posted
  elsewhere. `doctor` and `config list` show which channels are forums and which permissions are
  missing. Assignee DMs link to the post.

```bash
hermes meeting-scribe config set google_meet_discord_channel "#meeting-notes"
hermes meeting-scribe config set delivery_fallback_channel "#backlog"
hermes meeting-scribe config set delivery_forum_default_tag "Minutes"  # only for forums that require a tag
hermes meeting-scribe config set delivery_discord_guild "My Team"    # only if the bot is in several servers
hermes meeting-scribe doctor
```

### Meeting rules: where each meeting goes

Rules decide, meeting by meeting, where the notes go. Each rule has three parts:

- **Which meetings**: a voice channel, every voice channel of a category, or Google Meet meetings
  whose code or title matches a pattern (`*` as wildcard).
- **Mode**:
  - **Normal**: the notes go to the channel you choose; tasks go to their project channels and the
    board as usual.
  - **Private**: everything (summary, transcript, every task) stays in that channel. Each task can be
    shared by a button.
  - **Direct messages only**: nothing is posted in any channel. Each participant gets the whole
    meeting in a direct message (summary, decisions, questions, the transcript file, the list of
    tasks) and their own tasks with buttons, including one to publish the task in its project
    channel.
- **Where**: a text channel or forum (not for direct messages).

The easiest way is Hermes Desktop → **Meetings** → **Settings** → **Delivery** → **Meeting rules**.
Each rule is shown as a sentence, for example *Voice channel «Team room» → forum «team-notes» ·
Normal*, with a ✓ when the bot has checked its channels. **Add rule** opens a small form: pick the
kind of meeting with the buttons at the top, choose the voice channel or category from a list (or
type a Meet pattern), choose the mode — a one-line explanation under it says what that mode does —
and pick the channel from a list grouped by category, where private channels carry a lock. If you
choose **Private** and a channel everyone in the server can see, a warning tells you so before you
save. The same is available from the terminal:

```bash
hermes meeting-scribe route list
hermes meeting-scribe route add --voice "Team room" --to "#team-notes"
hermes meeting-scribe route remove 2          # by its number in `route list`, or by its origin
hermes meeting-scribe route move 3 1          # make rule 3 the first one
```

Names are turned into ids when the bot has reported its channels (it does so when it connects and
whenever channels change), so renaming a channel later never breaks a rule. `hermes meeting-scribe
setup` offers to add rules too. With several spaces, add `--space <id>`.

**Three recipes**

1. **Each team's meetings in its own forum.** A team meets in the voice channel «Design room»:
   `route add --voice "Design room" --to "#design-notes"`. In Desktop: *Voice channel* → «Design
   room», *Normal*, → «design-notes». Tasks still go to their project channels.
2. **Management meetings in a private channel.** Everything said in the «Management» category stays
   in a channel only managers see: `route add --category "Management" --to "#management-notes"
   --private`. In Desktop: *Category* → «Management», *Private*, → «management-notes» (shown with a
   lock). Nothing reaches project channels, other people's DMs or the board unless someone in that
   channel presses a share button.
3. **One-to-ones only by direct message.** `route add --voice "1on1" --dm`. In Desktop: *Voice
   channel* → «1on1», *Direct messages only* (no channel to pick). Both people get the meeting in
   their DMs and nothing appears in the server.

**Direct messages only, in detail.** The participants are the people the capture saw (who spoke or
was in the call). For Google Meet, a participant counts when their name or email matches exactly one
person linked with `/meeting link`. Someone with closed DMs is skipped and `status`/`doctor` say
who; the others get it anyway, and it is never posted in a channel instead. If nobody can be reached
the meeting waits and says why. Reprocessing edits the same messages. Buttons only work in your own
copy and only on your own tasks (nobody can act on or forward someone else's task). The meeting
stays in direct messages even if the rule is removed later, and the agent only finds it from those
DMs. A meeting published before the rule existed is withdrawn from the channels first.

### Participants are mentioned

The first message of the notes (in a channel, its thread, a forum post or a private rule's channel)
@mentions the people who were in the meeting, so they know the notes are there: Discord participants
by their account, Google Meet attendees when they are linked with `/meeting link`, anyone else by
name. It pings only once, when the notes are first posted (reprocessing edits the text without
pinging again), never @everyone, @here, roles or the bot, and in a private channel only people who
can see it. Direct-messages-only meetings skip it. Turn it off with
`hermes meeting-scribe config set delivery_mention_participants false` (per space with `--space`) or
in Desktop → Settings → Delivery.

### Notes per voice channel and private meetings (rule syntax)

The rules above are stored in `meeting_routes`, one rule per entry, `origin = #channel`, with an
optional `:private`, or `origin = :dm` (also `origin = dm`) for direct messages only — no channel;
a channel literally called `dm` is written `#dm`:

- `Leadership = #leadership-notes:private`: the voice channel "Leadership" (by name or id).
- `category:Design = design-meetings`: every voice channel of the Discord category "Design".
- `meet:abc-* = #meet-notes`: Google Meet meetings whose code, room or title matches (`*`, `?`).
- `category:Board = :dm`: every meeting of the category "Board", only by direct message.

A voice channel rule wins over a category rule. Otherwise the first matching rule wins. A matching
rule's channel is the only place the notes may go: if the bot cannot use it, the meeting waits and
`status`/`doctor` explain why. It is never posted anywhere more public.

- **Normal rule**: only the notes move. Tasks still go to their project channels and to their
  assignees, as usual.
- **Private rule**: everything (summary, transcript, every task) stays in that channel. No task is
  sent to a project channel, to the fallback channel, to anyone's DMs, or to Kanban/Linear on its
  own. Each task gets buttons to **send it to its assignee** or **publish it in its project channel**.
  The index has **Share all tasks**, which asks for confirmation first. Only people who can see the
  private channel can press them. What is shared is just the task (title, description, assignee, due
  date): never the summary, the quotes or a link to the private channel. A meeting published as
  private stays private and stays **in its channel** even if the rule is later removed, edited or
  its channel renamed: when the rule points elsewhere the meeting waits (`status`/`doctor` say why)
  until you restore the rule or move it yourself with `hermes meeting-scribe private-move <id>
  <channel id>`. A meeting that becomes private after it was published normally is withdrawn from
  every public place (summary, tasks, threads, posts, assignee DMs); what the bot cannot delete is
  emptied and renamed "Content withdrawn", retried later, and `doctor` names the permission missing
  (**Manage Threads**, **Manage Messages**). The agent and `/meeting` only find it from its own
  channel; scheduled jobs (cron) and other platforms never see it. You see every meeting from the
  terminal (`hermes meeting-scribe show <id>`, `export`), from `hermes` chat in the CLI/TUI and from
  Desktop, which marks it 🔒. A rule you cannot read that says "private" anywhere holds every
  meeting of the space until it is fixed.

```bash
hermes meeting-scribe config set meeting_routes "Leadership = #leadership-notes:private, category:Design = design-meetings"
hermes meeting-scribe config list      # each rule, its channel, forum or not, private or not
hermes meeting-scribe doctor           # warns if a private rule points to a channel @everyone can see
hermes meeting-scribe private-move k3v7q2ab 123456789012   # move a private meeting to another channel
```

**Notes an older version posted in a DM.** Earlier versions could post a meeting to the gateway's
home channel when it was a DM. Those meetings keep working there (buttons, 📋 My tasks) and are
never moved by themselves; `status` and `doctor` list them. To move one to a server channel,
configure the channel explicitly and re-deliver it:

```bash
hermes meeting-scribe config set google_meet_discord_channel "#meeting-notes"   # or delivery_discord_channel
hermes meeting-scribe reprocess <id> --from deliver                           # or /meeting reprocess <id> from=deliver
```

The move posts everything in the channel first (summary, tasks, index; the transcript only if it was
meant to be attached) and deletes the DM messages last. If the channel cannot be used, the DM stays
as it is and the error says why. Without an explicitly configured channel the notes stay in the DM
(the automatic channel is not used for this) and the command explains what to set.

### Models and fallbacks

Meeting analysis runs as the Hermes auxiliary task `meeting_scribe`. Its model and fallback chain
live in Hermes' own config, under `auxiliary.meeting_scribe` — the plugin reads and writes that
block, so Hermes Desktop's auxiliary-model settings and these commands edit the same thing. By
default it uses Hermes' main model (`provider: auto`).

```bash
hermes meeting-scribe llm show                         # provider, model, fallbacks, timeout, origin
hermes meeting-scribe llm set --provider <provider> --model <model>
hermes meeting-scribe llm fallback add <provider>:<model>        # a fallback needs a model
hermes meeting-scribe llm fallback add <provider>:<model> --position 1
hermes meeting-scribe llm fallback remove 2            # by position, provider or provider:model
hermes meeting-scribe llm fallback clear
hermes meeting-scribe llm test                         # tiny call to every link; no secrets printed
```

This writes, for example:

```yaml
auxiliary:
  meeting_scribe:
    provider: <provider>
    model: <model>
    fallback_chain:
      - {provider: <provider-2>, model: <model-2>}
      - {provider: <provider-3>, model: <model-3>}
```

Hermes walks the chain on rate limits, connection errors and payment errors (402), not when a call
hangs. For that the plugin has its own limit, `analysis_timeout_seconds` (default 600): a call that
does not return fails the attempt, which is retried with backoff. The stuck call is abandoned in the
background (Python cannot stop it) and may still finish and use tokens. `analysis_max_tokens`
(default 8192) is sent with every call so the provider does not reserve the whole context window,
which is what turns a low balance into "402 … can only afford N". A reply that is not valid JSON
(usually cut off) is retried once right away with a stricter instruction (that chunk is sent, and
billed, twice; the attempt can take up to twice the timeout). If two timed-out calls are still
running, the next analysis fails fast with a clear error instead of starting a third. `doctor`
shows the chain and warns when there is no fallback or when an entry has no model (Hermes skips it).
Editing the chain keeps any extra keys you wrote by hand in an entry (`key_env`, `api_mode`, …), and
URLs are printed without credentials or query string.

## Tasks in Discord

After a meeting is processed, the plugin posts:

- **In the meeting chat:** the summary (TL;DR, decisions, open questions), then a **task index**:
  counts per project with a link to the thread that holds them, counts per person, and one
  **📋 My tasks** button.
- **In each project's channel:** a thread for the meeting, with **one message per task** and that
  task's buttons directly under it: ✅ Kanban · 🟣 Linear · ❌ Dismiss · 📁 Move. Once a task is
  handled, its message shows the result (``✅ Kanban `t_42` ``, `🟣 Linear ENG-7`, `❌ Dismissed`) and
  loses its buttons. The other tasks are not affected.
- **To each assignee:** a DM with their tasks and the same buttons (`delivery_dm_assignees`, on by
  default). If someone has DMs closed, that is noted in the index and nothing else fails.

**📋 My tasks** opens an *ephemeral* panel that only the person who clicked can see. It lists their
tasks, each with its own buttons, 4 per page. Owners also get a 👥 switch to see every task.

**Who can press what.** Buttons on a public message are visible to everyone, so every click is
checked against the task:

- The task's **assignee** and the **owners** may act on it. Anyone else gets a private "This task
  belongs to @X" and nothing happens.
- An **unassigned** task can only be handled by the owners.
- **✅ Kanban** is the owners' personal board, so it only appears on tasks assigned to an owner. Other
  people's tasks go to Linear.

**How a task finds its channel.** The first rule that matches wins:

1. An explicit `project_channels` entry, e.g. `["Website=123456789012345678"]`.
2. A mapping learned from a 📁 correction.
3. The best fuzzy match among the server's text channels and categories. A category resolves to its
   first channel the bot can post in.
4. Otherwise the meeting chat.

Channel names are compared after removing decoration: emoji, symbols, box-drawing separators such as
`┃` or `・`, and brackets such as `『』` or `【】`. So `『🚀』website`, `🟢┃website` and `【Website】` all
read as `website`. The matcher tolerates transcription errors: *Nebulla* finds `#nebula`. Short or
common words never match on their own; a single-word match needs at least 4 letters.

If your server puts decorative *words* in front of channel names, list them in
`channel_name_ignore_prefixes`. For example, `["team", "proj"]` makes `team-website` and
`proj-website` read as `website`.

When the match is weak, or two channels score about the same, the task is still posted in the most
likely channel, marked **⚠️ project not certain — confirm with 📁**. Pressing 📁 moves the task: it is
re-posted in the right channel's thread and the old message is deleted. When an owner moves a task,
the correction is also remembered for future meetings; an assignee's move only affects their task. If the bot lacks View Channel, Send Messages or Create Public Threads in
the matched channel, the task stays in the meeting chat and the index says why.

Reprocessing a meeting edits these messages in place instead of posting new ones.

## Google Meet

meeting-scribe can also import **transcripts that Google Meet already generated** and run them
through the same analysis and delivery as Discord meetings (summary, decisions, tasks per project,
Kanban/Linear, Discord threads). No audio is downloaded and no bot joins the call.

### Requirements

- A Google Workspace edition with Meet transcription (for example Business Standard/Plus,
  Enterprise, Education Plus, Workspace Individual). Your admin must allow transcripts.
- Transcription must be **on** in the meeting (Activities → Transcripts), or turned on
  automatically from the Calendar event.
- Only meetings **organised by the connected account** are imported: the Meet API lists conference
  records filtered to the organizer. Meetings you only attended (organised by a colleague or another
  organisation) are not visible; whoever organises them has to connect their own account.
- Google deletes transcript entries **30 days** after the meeting ends; import before that.

### Create your own OAuth app (once)

Every installation uses its **own** Google Cloud OAuth client; the plugin ships none.

1. Open <https://console.cloud.google.com/>, create (or pick) a project.
2. **APIs & Services → Library**: enable **Google Meet REST API**.
3. **Google Auth Platform → Branding / Audience** (OAuth consent screen): user type **Internal**
   (Workspace only; no Google review needed). Fill in the app name and support email.
4. **Data access**: add the scope `https://www.googleapis.com/auth/meetings.space.readonly`.
5. **Clients → Create client**: application type **Desktop app**. Download the JSON.

### Connect and use

```text
hermes meeting-scribe google connect --client-secret ~/Downloads/client_secret_XXXX.json [--no-browser]
hermes meeting-scribe config set google_meet_enabled true
hermes meeting-scribe config set google_meet_discord_channel <text channel id>   # recommended
# restart the gateway so its poller starts
hermes meeting-scribe google status [--json]
hermes meeting-scribe google sync [--since 2026-09-01T00:00:00Z | --days N] [--dry-run] [--json]
hermes meeting-scribe google disconnect
```

- `connect` copies the client JSON to `<HERMES_HOME>/plugin-data/meeting-scribe/google/client.json`
  and stores the token in `token.json` next to it (both mode 0600). It opens a browser and listens
  once on `http://127.0.0.1:<free port>`. On a remote/SSH machine use `--no-browser`: open the
  printed URL anywhere, consent, and paste back the full URL you were redirected to (the page may
  fail to load; that is expected) or just the code.
- The gateway polls every `google_meet_poll_minutes` (default 5), starting when the plugin loads
  (Discord does not need to be connected). Only one process polls (a lease in the plugin's SQLite). Only conferences that **end after you connected** are imported
  automatically; use `google sync --days N` for an explicit backfill (max 30 days). Running
  `connect` again (after a revocation, or with a new client) keeps the original connection time,
  so meetings that ended while access was broken are still picked up (within Meet's 30 days);
  only `google disconnect` resets it. `disconnect` revokes the grant at Google and deletes the local
  token; if the revoke fails (offline, Google error) it says so and points to
  https://myaccount.google.com/permissions to remove the access by hand.
- A conference is imported once, ever (unique on its Meet record name), even across restarts or two
  processes. A transcript still being generated (`ENDED`) is retried on the next poll.
- Notes go to `google_meet_discord_channel`, else `delivery_discord_channel`, else an automatic
  channel of the server (never a DM); see "Where notes are posted". Channels can be given by id or
  name. With no usable channel the meeting waits (it is still processed: CLI, agent tools, files,
  Kanban) and is posted as soon as you set one. `setup`, `config set` and `doctor` warn when the
  import is on without a channel, since the full transcript is posted with the notes.
- Meet participants are not Discord users: tasks show their name, without mentions or DMs.
- `invalid_grant` (revoked or expired access) shows as "disconnected" in `google status` and
  `doctor`; run `connect` again.

**What leaves your machine:** the plugin calls only `meet.googleapis.com` (read-only scope
`meetings.space.readonly`: conference records, participants, transcripts and entries) and
`oauth2.googleapis.com` (tokens, revocation). No Drive access, no user profile. The transcript text
then follows the normal path: your Hermes LLM provider, and Discord (see the transcript attachment
below).

## Full transcript in Discord

With `delivery_discord_transcript` (default **on**) every meeting — Discord or Google Meet — gets
its full transcript attached as `transcript-<date>-<slug>.md` (`[mm:ss] Name: text`) right after
the summary in the meeting chat. Only the delivery step attaches it, once: retries never re-attach
and button clicks never attach anything; a reprocess that changes the transcript lines replaces it
(a new title alone does not). Meetings whose summary was posted without the attachment (delivered by
an older version, or with the setting off) never get it later. Files over 8 MB are split into
numbered parts. Without the **Attach Files** permission a one-line notice is posted instead and the
delivery continues. Turn it off with `hermes meeting-scribe config set delivery_discord_transcript false`.

## Integrations

### Hermes Kanban

Only action items owned by one of the **owners** become Kanban tasks. The owners are the ids in
`owners`, or the first `DISCORD_ALLOWED_USERS` entry when `owners` is empty. Each task is created
in **triage**, and its body includes the meeting folder, the quote and the timestamp.

**Modes:**

- `kanban_mode: approve` (the default): approve each task with its ✅ button in Discord.
- `kanban_mode: auto`: tasks are created as soon as the meeting is processed.

**Where each task goes:**

- If the task (or else the meeting) resolves to a **Hermes project**, the task goes to that project's board and gets
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

`<HERMES_HOME>` is the owner profile's home ([Use Meetings from any profile](#use-meetings-from-any-profile)). `reprocess --from transcribe` extracts the speaker
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
- Google Meet import (opt-in) reads transcripts with your own OAuth client and the read-only
  `meetings.space.readonly` scope; see [Google Meet](#google-meet).
- The full transcript is attached to the Discord notes by default (`delivery_discord_transcript`);
  everyone who can read the notes channel can read it.
- To delete a meeting, remove its folder. `audio_retention: none` deletes the audio once processing
  finishes.

## Spaces: several teams on one bot

A **space** is one team or client: its own Discord servers, meetings, settings (overrides on top of
the global ones), owners, people links and Google Meet connection. Nothing crosses from one space to
another. A new install has one space, `main`, which takes every server the bot is in: with a single
team you never need to think about spaces.

```text
hermes meeting-scribe space create "Acme" --slug acme      # a second team
hermes meeting-scribe space add-guild acme 123456789012345678   # its Discord server
hermes meeting-scribe space set acme ui_language en         # a per-space override (unset to remove)
hermes meeting-scribe google connect --space acme --client-secret acme-client.json
hermes meeting-scribe space list                            # servers, meetings, Google per space
```

With several spaces:

- A server that no space owns is never recorded. `/meeting` there answers that an administrator must
  link it and names the command (`space add-guild <space> <server id>`). `doctor` lists those servers.
- In a DM, `/meeting` uses the spaces of the servers you are in. If you are in several, add
  `space=<id>` at the end, for example `/meeting list space=acme`.
- CLI views (`status`, `list`, `show`, `export`) show every space with a space column unless you pass
  `--space`; actions (`reprocess`, `config set`, `google …`) need `--space`.
- Machine-wide settings (`pipeline_*`, `transcribe_model`, `audio_*`…) cannot differ per space.

**Recording in parallel.** Different servers record at the same time, each into its own space. Discord
gives a bot **one voice connection per server**, so one server records one channel at a time: while a
channel is being recorded, auto-join does not move to another channel of that server, and
`/meeting start` from another channel says which channel is being recorded. Google Meet imports of
different spaces run independently (one space paused by Google does not delay another).

## Hermes Desktop: the Meetings page

The plugin ships a **Meetings** page for Hermes Desktop (`desktop/plugin.js`, with its API in
`dashboard/`). It is optional: recording and Discord work the same without it.

**Turn it on.** Install the plugin as usual (the Python half) and restart Hermes so the page's API is
mounted. Then open **Capabilities → Plugins** in Desktop and switch on **Meetings** (off by
default). A **Meetings** row appears in the sidebar and in the command palette.

**What it shows.**

- **Library**: every meeting, newest first, with search over titles and what was said, filters by
  source, status and dates, and pages of 30.
- **Meeting**: summary, topics, decisions, open questions, tasks (owner, project and where each one
  went: Discord, Kanban, Linear, with links), the transcript (loaded in pages, with a search box) and
  the recording when a mixed track was kept. **Reprocess…** asks which step to redo, queues it and
  follows it until the bot finishes.
- **Status**: whether the bot is online (it checks in every ~20 s), the processing queue, meetings
  waiting for a channel, Google Meet (connected, or the commands to connect it) and **Diagnostics**.
- **Settings**: every plugin setting, grouped, with its label in English or Spanish, validation and
  whether it is the default, a custom value or invalid; and **Models** (main model plus ordered
  backups you can add, remove and reorder).

**Any profile.** The page always shows the owner's meetings, whichever profile Desktop was opened
with and whichever profile you switch to. Hermes only serves a plugin's page when Desktop was
*opened* with a profile that has the plugin turned on; otherwise the page says so. Turn it on in the
profiles you open Desktop with, as in [Use Meetings from any profile](#use-meetings-from-any-profile):
it does not start a second bot.

The page reads the owner's files and database and never starts a recording or a pipeline:
**Reprocess** is carried out by the owner's gateway worker, so that gateway must be running. Settings are
written with the same rules as `hermes meeting-scribe config set` and `llm set`.

**Not verified yet:** the page is covered by Node render tests and Hermes' `plugins validate`, but it
has not been exercised in a running Hermes Desktop. Audio playback relies on Desktop's media stream
for `recording.ogg` and is unproven there; multitrack (`.mka`) recordings cannot be played.

**Several spaces.** The page has no space selector yet: with more than one space its library and
status show an error asking for a space. The API underneath already takes `?space=` and manages
spaces and servers (contract: [DESIGN, Appendix A](docs/DESIGN.md#appendix-a-rest-contract-desktop-apipluginsmeeting-scribe)).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/meeting start` says capture is not compatible | Hermes' Discord adapter changed internals the plugin relies on. `doctor` lists the failing checks under `discord_compat`. Update the plugin, or run a Hermes version it supports. |
| "I'm already recording **X** in this server" from another channel | One voice connection per server: stop the other recording first, or use another server. |
| `/meeting` says the server is not linked to any team | Several spaces exist and no space owns this server: `hermes meeting-scribe space add-guild <space> <server id>`. |
| "I'm already connected to a voice channel in this server" | Discord allows **one voice connection per server for each bot**. Run `/voice leave` (Hermes voice chat) first. |
| `/meeting start` fails from the CLI or TUI | Live capture only works from Discord, because it needs the gateway's Discord connection. |
| The first word from a new speaker is missing | Known Discord/DAVE limitation: a new speaker's audio is dropped until Discord maps their stream, which takes about 100 ms. |
| Wrong language or misheard words | Pin `transcribe_language`, and try a larger `transcribe_model`. |
| A meeting is stuck or failed | `hermes meeting-scribe status` shows the stage and the error. Then run `hermes meeting-scribe reprocess <id> --from <stage>`. |
| Meetings says "not available in this window" | Desktop was opened with a profile where Meetings is not turned on: see [Use Meetings from any profile](#use-meetings-from-any-profile). |
| A `hermes meeting-scribe` command only says to use another profile | That profile is not the owner; run it as `hermes -p <owner> meeting-scribe …`. |
| ffmpeg is found but reports no libopus | Install an ffmpeg build that includes libopus. `doctor` shows which binary it picked. |

## Limitations

- Google Meet import needs Meet's own transcription (Workspace) and only sees meetings the connected
  account owns or joined; it has not yet been tested against the live Google API.
- Live capture works on Discord only. Processing, search and the agent tools work on any Hermes
  surface.
- Each Discord server can have one recording at a time (one voice connection per bot and server).
  Several servers can record at once.
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
node tests/desktop/run.mjs                    # Desktop page (Node >= 20.6; React from the Hermes checkout)
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
