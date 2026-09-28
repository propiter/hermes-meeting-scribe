/**
 * meeting-scribe — the «Meetings» page of Hermes Desktop.
 *
 * Plain ESM loaded uncompiled by the Desktop runtime loader: no build step, no JSX (UI is `jsx()`
 * calls through `h()`), and only the imports the loader maps (`@hermes/plugin-sdk`, `react`,
 * `react/jsx-runtime`). The loader evaluates the module from a blob URL, so relative imports
 * cannot resolve: the page is one file, organised top-down (i18n → data → helpers → library →
 * detail tabs → status → settings → page → registration).
 *
 * Layout: master–detail, like Hermes' own Kanban and Scheduled jobs. The meeting list (search by
 * content + filters) stays on the left; the selected meeting opens on the right with the tabs
 * Summary | Transcript | Tasks | Processing. On a narrow window the two stack and the detail gets a
 * back button. Status and Settings are separate views reached from the header, so they never
 * compete with the library.
 *
 * Every read and write goes through `ctx.rest` to this plugin's backend
 * (`/api/plugins/meeting-scribe/v1/...`, meeting_scribe/desktop/api.py). The page never runs the
 * pipeline: «Reprocess» and «Prepare audio» queue a command that the gateway's worker executes,
 * and the page polls it. The data belongs to the owner profile (DESIGN §1.5), so every Desktop
 * profile shows the same library; query keys carry the connection (a different Hermes) and
 * polling runs only while something is in progress and stops on a 4xx.
 */

import {
  atom,
  Badge,
  Button,
  Codicon,
  ConfirmDialog,
  host,
  Input,
  PALETTE_AREA,
  ROUTES_AREA,
  SearchField,
  SegmentedControl,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  SIDEBAR_NAV_AREA,
  Skeleton,
  StatusDot,
  Switch,
  Tabs,
  TabsList,
  TabsTrigger,
  Textarea,
  useI18n,
  usePluginI18n,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

export const ID = 'meeting-scribe'
export const ROUTE = '/meeting-scribe'
const KANBAN_ROUTE = '/kanban'
const ALL = '__all__'
const Q = 'meeting-scribe'
const PAGE_SIZE = 40
const TRANSCRIPT_PAGE = 300
const POLL_MS = 2500
const LIST_POLL_MS = 5000
const STAGES = ['transcribe', 'analyze', 'deliver']
const IN_PROGRESS = ['recording', 'captured', 'transcribing', 'transcribed', 'analyzing', 'analyzed', 'delivering']

/** `h(type, props, ...children)` → jsx/jsxs; `key` travels as jsx's third argument. */
export function h(type, props, ...children) {
  const { key, ...rest } = props || {}
  const kids = children.flat().filter(c => c !== null && c !== undefined && c !== false && c !== '')
  if (kids.length === 0) return jsx(type, rest, key)
  if (kids.length === 1) return jsx(type, { ...rest, children: kids[0] }, key)
  return jsxs(type, { ...rest, children: kids }, key)
}

let CTX = null
/** `library` | `status` | `settings` — kept across visits. */
export const $view = atom('library')
/** Open meeting id (null = none). */
export const $selected = atom(null)
/** Detail tab of the open meeting. */
export const $tab = atom('summary')
/** Library search + filters, kept across visits. */
export const $filters = atom({ q: '', state: ALL, source: ALL, channel: ALL, project: ALL, since: '', until: '' })

// ---------------------------------------------------------------------------------------------
// i18n (plugin-scoped; the bot's own Discord texts live in meeting_scribe/i18n and are not used here)
// ---------------------------------------------------------------------------------------------
export const LOCALES = {
  en: {
    nav: 'Meetings',
    open: 'Open meetings',
    title: 'Meetings',
    views: { library: 'Meetings', status: 'Status', settings: 'Settings' },
    common: {
      retry: 'Try again', loading: 'Loading…', save: 'Save', saved: 'Saved', cancel: 'Cancel', back: 'Meetings',
      refresh: 'Refresh', none: '—', open: 'Open', details: 'Technical details', saving: 'Saving…', close: 'Close'
    },
    disabled: {
      title: 'Meetings is not available in this window',
      body: 'Hermes was opened with a profile where Meetings is not turned on, so there is nothing to show here. Your meetings are safe.',
      steps: 'Open Hermes with a profile where Meetings is turned on, or ask whoever manages Hermes to turn it on for this profile too (README: «Use Meetings from any profile»). Every profile shows the same meetings, and turning it on in another profile does not start a second bot.'
    },
    error: {
      title: 'Could not load this',
      notFound: 'This meeting no longer exists.',
      generic: message => `The server answered: ${message}`,
      offline: 'Hermes is not reachable right now.'
    },
    library: {
      search: 'Search meetings and what was said…', searchLabel: 'Search meetings',
      filters: 'Filters', clear: 'Clear',
      date: 'Date', project: 'Project', state: 'Status', channel: 'Channel', source: 'Source',
      anyDate: 'Any date', anyProject: 'All projects', anyState: 'All statuses', anyChannel: 'All channels',
      anySource: 'All sources', since: 'From', until: 'To',
      dates: { today: 'Today', week: 'Last 7 days', month: 'Last 30 days', custom: 'Custom range' },
      count: (shown, total) => (shown === total ? `${total} meetings` : `${shown} of ${total} meetings`),
      more: 'Show older meetings',
      emptyTitle: 'No meetings yet',
      emptyBody: 'Meetings appear here after the bot records a Discord voice call or imports a Google Meet transcript.',
      noMatchTitle: 'Nothing matches',
      noMatchBody: 'Try other words or clear the filters.',
      untitled: 'Untitled meeting',
      people: n => (n === 1 ? '1 person' : `${n} people`),
      tasks: n => (n === 1 ? '1 task' : `${n} tasks`),
      noTasks: 'No tasks',
      pickTitle: 'Pick a meeting',
      pickBody: 'Its summary, transcript, tasks and processing appear here.'
    },
    source: { discord: 'Discord', google_meet: 'Google Meet' },
    private: { label: 'Private', hint: 'Private meeting: its notes are only in its private channel, and tasks leave it only when someone there shares them.' },
    state: {
      recording: 'Recording', captured: 'Waiting to be processed', transcribing: 'Transcribing',
      transcribed: 'Transcribed', analyzing: 'Writing the notes', analyzed: 'Notes written',
      delivering: 'Publishing', done: 'Ready', failed: 'Needs attention', processing: 'In progress',
      empty: 'Discarded: no audio'
    },
    stateGroup: { recording: 'Recording', processing: 'In progress', done: 'Ready', failed: 'Needs attention', empty: 'Discarded' },
    stage: { transcribe: 'Transcribing', analyze: 'Writing the notes', deliver: 'Publishing', archive: 'Saving the audio' },
    detail: {
      tabs: { summary: 'Summary', transcript: 'Transcript', tasks: 'Tasks', processing: 'Processing' },
      duration: m => (m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`),
      partial: 'Partial recording: the bot joined late or the call was cut.',
      summary: 'Summary', inShort: 'In short', topics: 'Topics', decisions: 'Decisions', questions: 'Open questions',
      pendingTasks: 'Pending tasks', allTasks: n => `See all ${n} tasks`,
      noNotes: 'The notes are not ready yet. They appear here once the meeting has been processed.',
      noNotesFailed: 'Processing stopped before the notes were written. See «Processing».',
      empty: 'No audio was captured (nobody spoke), so there are no notes. Nothing was published.',
      recording: 'This meeting is being recorded. Notes appear once it ends and is processed.',
      noDecisions: 'No decisions were recorded.', noQuestions: 'No open questions.',
      waiting: 'Waiting for a place to publish',
      waitingHelp: 'The notes are ready but no Discord channel could be used. Choose a notes channel in Settings and they are sent automatically.',
      dmNotes: 'Notes were left in a direct message',
      dmNotesHelp: 'The bot could not post in the server, so it sent the notes by DM. They move to the channel once one is available.'
    },
    audio: {
      title: 'Recording', label: 'Meeting recording',
      ready: 'Listen to the whole meeting. Click a time in the transcript to jump there.',
      original: 'The original, one track per person, is kept for reprocessing.',
      multitrack: 'This recording was kept as one track per person. Prepare a listening copy to play it here.',
      prepare: 'Prepare audio', preparing: 'Preparing the audio…', prepareFailed: message => `Could not prepare the audio: ${message}`,
      imported: 'Meetings imported from Google Meet come with their transcript only, without audio.',
      not_retained: 'The audio of this meeting was not kept (Settings → Privacy → audio kept).',
      recording: 'The recording is available when the meeting ends.',
      empty: 'No audio was captured.',
      failed: 'The recording could not be played here. The file is still on the machine where Hermes runs.'
    },
    transcript: {
      search: 'Find in the transcript…', searchLabel: 'Find in the transcript',
      lines: (shown, total) => (shown >= total ? `${total} lines` : `${shown} of ${total} lines`),
      matches: n => (n === 1 ? '1 match' : `${n} matches`),
      more: 'Load more', all: 'Load everything',
      partialSearch: 'Searching the lines loaded so far. Load everything to search the whole meeting.',
      empty: 'There is no transcript for this meeting yet.', noMatch: 'No line matches.',
      jump: time => `Play from ${time}`
    },
    tasks: {
      empty: 'No tasks came out of this meeting.',
      owner: 'Owner', project: 'Project', due: 'Due', noOwner: 'Unassigned', noProject: 'No project',
      destinations: 'Destinations',
      discord: 'Discord', kanban: 'Kanban', linear: 'Linear',
      dismissed: 'Dismissed',
      status: {
        pending: 'Waiting for approval', approved: 'Sending…', delivered: 'Sent', dismissed: 'Dismissed',
        failed: 'Failed', skipped: 'Skipped', off: 'Off', posted: 'Posted', notPosted: 'Not posted'
      },
      openDiscord: 'Open in Discord', openLinear: 'Open in Linear', openKanban: 'Open the board',
      quote: 'Said in the meeting'
    },
    processing: {
      current: 'Current step', history: 'History', none: 'Nothing has happened yet.',
      ev: {
        started: 'Recording started', ended: 'Recording ended', queued: 'Queued for processing',
        running: step => `${step}…`, processed: 'Processed', waiting: 'Waiting to retry',
        failed: step => `Stopped while ${step.toLowerCase()}`, waiting_destination: 'Waiting for a place to publish',
        command: 'Action from this page'
      },
      attempts: n => `Attempt ${n}`,
      problem: {
        ffmpeg: 'The audio tool (ffmpeg) is missing or failed. Install ffmpeg or set its path in Settings → Recording.',
        no_audio: 'No audio could be read from the recording.',
        model: 'The local transcription model could not run. Check the transcription settings (model, device).',
        llm: 'The model that writes the notes did not answer correctly. It is retried automatically; check the models in Settings if it persists.',
        destination: 'The notes could not be posted in Discord (channel missing or no permission). Check the destinations in Settings.',
        kanban: 'The task could not be created in Kanban.',
        linear: 'The task could not be created in Linear. Check the Linear connection.',
        unknown: 'Something unexpected stopped the processing. The technical details below can help.'
      },
      retryAuto: 'It will be retried automatically.',
      reprocess: 'Reprocess…',
      reprocessHelp: 'Redo a step and everything after it. Already published messages are updated, not duplicated.',
      actions: { reprocess: step => `Reprocess from «${step}»`, prepare_audio: 'Prepare the audio to listen to', unknown: 'An action from this page' },
      cmd: {
        queued: 'Queued: the bot starts in a few seconds.', running: 'Running…', done: 'Finished.',
        failed: message => `Failed: ${message}`,
        unknown: 'The bot stopped while doing this; the result is unknown. Check the meeting, then mark it as reviewed.',
        acknowledged: 'Reviewed.', stalled: 'Still running, but the bot has not reported progress for a while.',
        gone: 'This request is no longer available.', lost: message => `Could not check the request: ${message}`
      },
      acknowledge: 'Mark as reviewed',
      busyRecording: 'The meeting is still being recorded.', busyRunning: 'The meeting is being processed right now.',
      busyEmpty: 'Nothing to reprocess: no audio was captured.',
      workerStale: 'The bot has not checked in for a while, so the action may wait until it is back online.'
    },
    reprocess: {
      title: 'Reprocess this meeting?',
      description: 'Hermes will redo the step you pick and every step after it.',
      from: 'Start again from', confirm: 'Reprocess', busy: 'Sending…',
      transcribe: 'Transcription', transcribeHelp: 'Listen to the recording again and rewrite transcript, notes and tasks. The slowest option.',
      analyze: 'Notes', analyzeHelp: 'Keep the transcript and write the summary, decisions and tasks again.',
      deliver: 'Publishing', deliverHelp: 'Keep the notes and send them again to Discord and the other destinations.'
    },
    status: {
      bot: 'Bot', queue: 'Processing queue', google: 'Google Meet', warnings: 'Settings that could not be used',
      warningsHelp: 'These values are invalid, so the default is used. Fix them in Settings.',
      worker: {
        recent: 'Online and processing meetings.', stale: 'Has not checked in recently. Is the Hermes gateway running?',
        unknown: 'Has not checked in yet. It reports once the gateway runs this version of the plugin.'
      },
      lastSeen: when => `Last seen ${when}`,
      running: 'In progress', queued: 'Waiting', failed: 'Need attention',
      noJobs: 'Nothing is waiting to be processed.',
      waiting: 'Waiting for a place to publish', dmNotes: 'Notes left in a direct message',
      commands: 'Recent actions', noCommands: 'No actions from this page yet.',
      googleConnected: 'Connected. New Meet transcripts are imported automatically.',
      googleConnectedOff: 'Connected, but importing is turned off.',
      googleRevoked: 'The connection was revoked. Connect again from a terminal.',
      googleNot: 'Not connected.', googleGuide: 'To connect, run in a terminal on the machine where Hermes runs:',
      googleLastPoll: when => `Last check ${when}`, googleLastImport: when => `Last import ${when}`,
      googleError: message => `Last error: ${message}`,
      doctor: 'Diagnostics', doctorHelp: 'Checks the configuration, storage and connected services.',
      doctorRun: 'Run diagnostics', doctorAgain: 'Run again', doctorOk: 'Everything looks good.',
      doctorIssues: n => (n === 1 ? '1 thing to check.' : `${n} things to check.`),
      check: { ok: 'OK', warn: 'Check', fail: 'Problem' },
      openMeeting: 'Open',
      cmd: { queued: 'Waiting to start', running: 'In progress', done: 'Finished', failed: 'Could not finish', unknown: 'Result unknown', acknowledged: 'Reviewed' }
    },
    settings: {
      intro: 'Changes reach the bot within a few seconds. Values set by an administrator cannot be changed here.',
      origin: { default: 'Default', configured: 'Custom', invalid: 'Invalid' },
      defaultIs: value => `Default: ${value}`,
      listHelp: 'One per line.', empty: 'empty', yes: 'On', no: 'Off',
      invalidNumber: 'Enter a number.', invalidInteger: 'Enter a whole number.',
      min: n => `Must be at least ${n}.`, max: n => `Must be at most ${n}.`,
      requeued: n => (n === 1 ? '1 waiting meeting will be published again.' : `${n} waiting meetings will be published again.`),
      sections: 'Sections'
    },
    projects: {
      title: 'Project channels',
      intro: 'Tasks of a project are posted in its channel. Without an entry, Hermes looks for a channel named like the project.',
      project: 'Project', channel: 'Discord channel id', add: 'Add project', remove: 'Remove',
      none: 'No project has its own channel yet.',
      needName: 'Every row needs a project name.', needChannel: 'Use the channel id (right-click the channel in Discord → Copy ID).',
      duplicate: name => `“${name}” appears twice.`,
      save: 'Save project channels', saved: 'Project channels saved.',
      row: n => `Project ${n}`
    },
    llm: {
      title: 'Models',
      intro: 'The model that writes the notes, and the backups tried in order when it fails (limits, connection or billing).',
      primary: 'Main model', provider: 'Provider', model: 'Model', baseUrl: 'Own endpoint (optional)',
      timeout: 'Time limit per call (s)', providerHelp: '“auto” uses the main model of Hermes.',
      effective: label => `In use: ${label}`,
      fallbacks: 'Backups, in order', noFallbacks: 'No backups: if the main model fails, the meeting waits and retries.',
      add: 'Add backup', remove: 'Remove', up: 'Move up', down: 'Move down',
      save: 'Save models', saved: 'Models saved.', problems: 'Problems',
      source: { 'hermes-config': 'Custom', 'plugin-default': 'Default' },
      row: n => `Backup ${n}`, needProvider: 'Every backup needs a provider.'
    },
    choice: {
      transcribe_device: { auto: 'Automatic', cpu: 'Processor (CPU)', cuda: 'Graphics card (CUDA)' },
      transcribe_compute_type: { auto: 'Automatic' },
      kanban_mode: { approve: 'Ask before creating', auto: 'Create automatically', off: 'Off' },
      linear_mode: { approve: 'Ask before creating', auto: 'Create automatically', off: 'Off' },
      audio_retention: { multitrack: 'One track per person', mixed: 'One mixed track', none: 'Do not keep audio' },
      ui_language: { en: 'English', es: 'Spanish' }
    }
  },
  es: {
    nav: 'Reuniones',
    open: 'Abrir reuniones',
    title: 'Reuniones',
    views: { library: 'Reuniones', status: 'Estado', settings: 'Ajustes' },
    common: {
      retry: 'Reintentar', loading: 'Cargando…', save: 'Guardar', saved: 'Guardado', cancel: 'Cancelar', back: 'Reuniones',
      refresh: 'Actualizar', none: '—', open: 'Abrir', details: 'Detalles técnicos', saving: 'Guardando…', close: 'Cerrar'
    },
    disabled: {
      title: 'Reuniones no está disponible en esta ventana',
      body: 'Hermes se abrió con un perfil que no tiene Reuniones activado, así que aquí no hay nada que mostrar. Tus reuniones siguen a salvo.',
      steps: 'Abre Hermes con un perfil que tenga Reuniones activado, o pide a quien administra Hermes que lo active también en este perfil (README: «Usar Reuniones desde cualquier perfil»). Todos los perfiles muestran las mismas reuniones, y activarlo en otro perfil no arranca un segundo bot.'
    },
    error: {
      title: 'No se pudo cargar',
      notFound: 'Esta reunión ya no existe.',
      generic: message => `El servidor respondió: ${message}`,
      offline: 'Ahora mismo no se puede contactar con Hermes.'
    },
    library: {
      search: 'Buscar en reuniones y en lo que se dijo…', searchLabel: 'Buscar reuniones',
      filters: 'Filtros', clear: 'Quitar',
      date: 'Fecha', project: 'Proyecto', state: 'Estado', channel: 'Canal', source: 'Origen',
      anyDate: 'Cualquier fecha', anyProject: 'Todos los proyectos', anyState: 'Todos los estados', anyChannel: 'Todos los canales',
      anySource: 'Todos los orígenes', since: 'Desde', until: 'Hasta',
      dates: { today: 'Hoy', week: 'Últimos 7 días', month: 'Últimos 30 días', custom: 'Rango personalizado' },
      count: (shown, total) => (shown === total ? `${total} reuniones` : `${shown} de ${total} reuniones`),
      more: 'Ver reuniones anteriores',
      emptyTitle: 'Todavía no hay reuniones',
      emptyBody: 'Las reuniones aparecen aquí cuando el bot graba una llamada de voz de Discord o importa una transcripción de Google Meet.',
      noMatchTitle: 'Nada coincide',
      noMatchBody: 'Prueba con otras palabras o quita los filtros.',
      untitled: 'Reunión sin título',
      people: n => (n === 1 ? '1 persona' : `${n} personas`),
      tasks: n => (n === 1 ? '1 tarea' : `${n} tareas`),
      noTasks: 'Sin tareas',
      pickTitle: 'Elige una reunión',
      pickBody: 'Aquí verás su resumen, transcripción, tareas y procesamiento.'
    },
    source: { discord: 'Discord', google_meet: 'Google Meet' },
    private: { label: 'Privada', hint: 'Reunión privada: sus notas solo están en su canal privado y las tareas salen de ahí solo cuando alguien del canal las comparte.' },
    state: {
      recording: 'Grabando', captured: 'Esperando proceso', transcribing: 'Transcribiendo',
      transcribed: 'Transcrita', analyzing: 'Escribiendo las notas', analyzed: 'Notas escritas',
      delivering: 'Publicando', done: 'Lista', failed: 'Requiere atención', processing: 'En proceso',
      empty: 'Descartada: sin audio'
    },
    stateGroup: { recording: 'Grabando', processing: 'En proceso', done: 'Listas', failed: 'Requieren atención', empty: 'Descartadas' },
    stage: { transcribe: 'Transcribiendo', analyze: 'Escribiendo las notas', deliver: 'Publicando', archive: 'Guardando el audio' },
    detail: {
      tabs: { summary: 'Resumen', transcript: 'Transcripción', tasks: 'Tareas', processing: 'Procesamiento' },
      duration: m => (m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`),
      partial: 'Grabación parcial: el bot entró tarde o la llamada se cortó.',
      summary: 'Resumen', inShort: 'En pocas palabras', topics: 'Temas', decisions: 'Decisiones', questions: 'Preguntas abiertas',
      pendingTasks: 'Tareas pendientes', allTasks: n => `Ver las ${n} tareas`,
      noNotes: 'Las notas aún no están listas. Aparecerán aquí cuando termine el proceso de la reunión.',
      noNotesFailed: 'El proceso se detuvo antes de escribir las notas. Mira «Procesamiento».',
      empty: 'No se captó audio (nadie habló), así que no hay notas. No se publicó nada.',
      recording: 'Esta reunión se está grabando. Las notas aparecerán cuando termine y se procese.',
      noDecisions: 'No se registraron decisiones.', noQuestions: 'No hay preguntas abiertas.',
      waiting: 'Esperando un lugar donde publicar',
      waitingHelp: 'Las notas están listas pero no se pudo usar ningún canal de Discord. Elige un canal de notas en Ajustes y se enviarán solas.',
      dmNotes: 'Las notas quedaron en un mensaje directo',
      dmNotesHelp: 'El bot no pudo publicar en el servidor y envió las notas por mensaje directo. Se moverán al canal en cuanto haya uno disponible.'
    },
    audio: {
      title: 'Grabación', label: 'Grabación de la reunión',
      ready: 'Escucha la reunión completa. Haz clic en una hora de la transcripción para saltar ahí.',
      original: 'El original, una pista por persona, se conserva para reprocesar.',
      multitrack: 'Esta grabación se guardó con una pista por persona. Prepara una copia para escucharla aquí.',
      prepare: 'Preparar audio', preparing: 'Preparando el audio…', prepareFailed: message => `No se pudo preparar el audio: ${message}`,
      imported: 'Las reuniones importadas de Google Meet traen solo la transcripción, sin audio.',
      not_retained: 'El audio de esta reunión no se conservó (Ajustes → Privacidad → audio que se guarda).',
      recording: 'La grabación estará disponible cuando termine la reunión.',
      empty: 'No se captó audio.',
      failed: 'La grabación no se pudo reproducir aquí. El archivo sigue en la máquina donde corre Hermes.'
    },
    transcript: {
      search: 'Buscar en la transcripción…', searchLabel: 'Buscar en la transcripción',
      lines: (shown, total) => (shown >= total ? `${total} líneas` : `${shown} de ${total} líneas`),
      matches: n => (n === 1 ? '1 coincidencia' : `${n} coincidencias`),
      more: 'Cargar más', all: 'Cargar todo',
      partialSearch: 'Se busca en las líneas cargadas. Carga todo para buscar en toda la reunión.',
      empty: 'Esta reunión todavía no tiene transcripción.', noMatch: 'Ninguna línea coincide.',
      jump: time => `Reproducir desde ${time}`
    },
    tasks: {
      empty: 'De esta reunión no salieron tareas.',
      owner: 'Responsable', project: 'Proyecto', due: 'Fecha límite', noOwner: 'Sin asignar', noProject: 'Sin proyecto',
      destinations: 'Destinos',
      discord: 'Discord', kanban: 'Kanban', linear: 'Linear',
      dismissed: 'Descartada',
      status: {
        pending: 'Esperando aprobación', approved: 'Enviando…', delivered: 'Enviada', dismissed: 'Descartada',
        failed: 'Falló', skipped: 'Omitida', off: 'Desactivado', posted: 'Publicada', notPosted: 'Sin publicar'
      },
      openDiscord: 'Abrir en Discord', openLinear: 'Abrir en Linear', openKanban: 'Abrir el tablero',
      quote: 'Dicho en la reunión'
    },
    processing: {
      current: 'Etapa actual', history: 'Historial', none: 'Todavía no ha pasado nada.',
      ev: {
        started: 'Empezó la grabación', ended: 'Terminó la grabación', queued: 'En cola para procesar',
        running: step => `${step}…`, processed: 'Procesada', waiting: 'Esperando para reintentar',
        failed: step => `Se detuvo mientras estaba ${step.toLowerCase()}`, waiting_destination: 'Esperando un lugar donde publicar',
        command: 'Acción desde esta página'
      },
      attempts: n => `Intento ${n}`,
      problem: {
        ffmpeg: 'Falta la herramienta de audio (ffmpeg) o falló. Instala ffmpeg o indica su ruta en Ajustes → Grabación.',
        no_audio: 'No se pudo leer audio de la grabación.',
        model: 'No se pudo ejecutar el modelo local de transcripción. Revisa los ajustes de transcripción (modelo, dispositivo).',
        llm: 'El modelo que escribe las notas no respondió bien. Se reintenta solo; si sigue, revisa los modelos en Ajustes.',
        destination: 'No se pudieron publicar las notas en Discord (falta el canal o no hay permiso). Revisa los destinos en Ajustes.',
        kanban: 'No se pudo crear la tarea en Kanban.',
        linear: 'No se pudo crear la tarea en Linear. Revisa la conexión con Linear.',
        unknown: 'Algo inesperado detuvo el proceso. Los detalles técnicos de abajo pueden ayudar.'
      },
      retryAuto: 'Se reintentará automáticamente.',
      reprocess: 'Reprocesar…',
      reprocessHelp: 'Repite una etapa y todas las siguientes. Lo ya publicado se actualiza, no se duplica.',
      actions: { reprocess: step => `Reprocesar desde «${step}»`, prepare_audio: 'Preparar el audio para escucharlo', unknown: 'Una acción de esta página' },
      cmd: {
        queued: 'En cola: el bot empieza en unos segundos.', running: 'En marcha…', done: 'Terminado.',
        failed: message => `Falló: ${message}`,
        unknown: 'El bot se detuvo mientras lo hacía; el resultado es desconocido. Revisa la reunión y márcala como revisada.',
        acknowledged: 'Revisado.', stalled: 'Sigue en marcha, pero el bot no informa progreso desde hace un rato.',
        gone: 'Esta solicitud ya no está disponible.', lost: message => `No se pudo consultar la solicitud: ${message}`
      },
      acknowledge: 'Marcar como revisada',
      busyRecording: 'La reunión todavía se está grabando.', busyRunning: 'La reunión se está procesando ahora mismo.',
      busyEmpty: 'No hay nada que reprocesar: no se captó audio.',
      workerStale: 'El bot no se reporta desde hace un rato, así que la acción puede esperar a que vuelva.'
    },
    reprocess: {
      title: '¿Reprocesar esta reunión?',
      description: 'Hermes repetirá la etapa que elijas y todas las siguientes.',
      from: 'Empezar de nuevo desde', confirm: 'Reprocesar', busy: 'Enviando…',
      transcribe: 'Transcripción', transcribeHelp: 'Volver a escuchar la grabación y reescribir transcripción, notas y tareas. La opción más lenta.',
      analyze: 'Notas', analyzeHelp: 'Conservar la transcripción y volver a escribir resumen, decisiones y tareas.',
      deliver: 'Publicación', deliverHelp: 'Conservar las notas y volver a enviarlas a Discord y a los demás destinos.'
    },
    status: {
      bot: 'Bot', queue: 'Cola de proceso', google: 'Google Meet', warnings: 'Ajustes que no se pudieron usar',
      warningsHelp: 'Estos valores no son válidos, así que se usa el predeterminado. Corrígelos en Ajustes.',
      worker: {
        recent: 'En línea y procesando reuniones.', stale: 'No se reporta desde hace un rato. ¿Está en marcha el gateway de Hermes?',
        unknown: 'Todavía no se ha reportado. Lo hará cuando el gateway ejecute esta versión del plugin.'
      },
      lastSeen: when => `Visto por última vez ${when}`,
      running: 'En proceso', queued: 'En espera', failed: 'Requieren atención',
      noJobs: 'No hay nada esperando proceso.',
      waiting: 'Esperando un lugar donde publicar', dmNotes: 'Notas que quedaron en un mensaje directo',
      commands: 'Acciones recientes', noCommands: 'Todavía no hay acciones desde esta página.',
      googleConnected: 'Conectado. Las transcripciones nuevas de Meet se importan solas.',
      googleConnectedOff: 'Conectado, pero la importación está desactivada.',
      googleRevoked: 'Se revocó la conexión. Vuelve a conectar desde una terminal.',
      googleNot: 'No conectado.', googleGuide: 'Para conectar, ejecuta en una terminal de la máquina donde corre Hermes:',
      googleLastPoll: when => `Última comprobación ${when}`, googleLastImport: when => `Última importación ${when}`,
      googleError: message => `Último error: ${message}`,
      doctor: 'Diagnóstico', doctorHelp: 'Comprueba la configuración, el almacenamiento y los servicios conectados.',
      doctorRun: 'Ejecutar diagnóstico', doctorAgain: 'Volver a ejecutar', doctorOk: 'Todo en orden.',
      doctorIssues: n => (n === 1 ? '1 cosa que revisar.' : `${n} cosas que revisar.`),
      check: { ok: 'Bien', warn: 'Revisar', fail: 'Problema' },
      openMeeting: 'Abrir',
      cmd: { queued: 'Pendiente de empezar', running: 'En marcha', done: 'Terminado', failed: 'No se pudo terminar', unknown: 'Resultado desconocido', acknowledged: 'Revisado' }
    },
    settings: {
      intro: 'Los cambios llegan al bot en unos segundos. Los valores fijados por un administrador no se pueden cambiar aquí.',
      origin: { default: 'Predeterminado', configured: 'Personalizado', invalid: 'No válido' },
      defaultIs: value => `Predeterminado: ${value}`,
      listHelp: 'Uno por línea.', empty: 'vacío', yes: 'Sí', no: 'No',
      invalidNumber: 'Escribe un número.', invalidInteger: 'Escribe un número entero.',
      min: n => `Debe ser al menos ${n}.`, max: n => `Debe ser como mucho ${n}.`,
      requeued: n => (n === 1 ? '1 reunión en espera se volverá a publicar.' : `${n} reuniones en espera se volverán a publicar.`),
      sections: 'Secciones'
    },
    projects: {
      title: 'Canales por proyecto',
      intro: 'Las tareas de un proyecto se publican en su canal. Sin una entrada, Hermes busca un canal con el nombre del proyecto.',
      project: 'Proyecto', channel: 'ID del canal de Discord', add: 'Añadir proyecto', remove: 'Quitar',
      none: 'Ningún proyecto tiene canal propio todavía.',
      needName: 'Cada fila necesita el nombre del proyecto.', needChannel: 'Usa el ID del canal (clic derecho en el canal en Discord → Copiar ID).',
      duplicate: name => `«${name}» aparece dos veces.`,
      save: 'Guardar canales por proyecto', saved: 'Canales por proyecto guardados.',
      row: n => `Proyecto ${n}`
    },
    llm: {
      title: 'Modelos',
      intro: 'El modelo que escribe las notas y los respaldos que se prueban en orden cuando falla (límites, conexión o facturación).',
      primary: 'Modelo principal', provider: 'Proveedor', model: 'Modelo', baseUrl: 'Endpoint propio (opcional)',
      timeout: 'Tiempo máximo por llamada (s)', providerHelp: '«auto» usa el modelo principal de Hermes.',
      effective: label => `En uso: ${label}`,
      fallbacks: 'Respaldos, en orden', noFallbacks: 'Sin respaldos: si el modelo principal falla, la reunión espera y se reintenta.',
      add: 'Añadir respaldo', remove: 'Quitar', up: 'Subir', down: 'Bajar',
      save: 'Guardar modelos', saved: 'Modelos guardados.', problems: 'Problemas',
      source: { 'hermes-config': 'Personalizado', 'plugin-default': 'Predeterminado' },
      row: n => `Respaldo ${n}`, needProvider: 'Cada respaldo necesita un proveedor.'
    },
    choice: {
      transcribe_device: { auto: 'Automático', cpu: 'Procesador (CPU)', cuda: 'Tarjeta gráfica (CUDA)' },
      transcribe_compute_type: { auto: 'Automático' },
      kanban_mode: { approve: 'Preguntar antes de crear', auto: 'Crear automáticamente', off: 'Desactivado' },
      linear_mode: { approve: 'Preguntar antes de crear', auto: 'Crear automáticamente', off: 'Desactivado' },
      audio_retention: { multitrack: 'Una pista por persona', mixed: 'Una pista mezclada', none: 'No guardar audio' },
      ui_language: { en: 'Inglés', es: 'Español' }
    }
  }
}

// ---------------------------------------------------------------------------------------------
// data
// ---------------------------------------------------------------------------------------------
/** `"404: {\"detail\":\"not found\"}"` (Desktop's transport shape) → `{status, message}`. */
export function parseError(error) {
  const raw = error instanceof Error ? error.message : String(error ?? '')
  // Electron IPC wraps the backend answer: "Error invoking remote method 'hermes:api': Error: 404: {...}".
  const m = /(?:^|Error:\s*)(\d{3}):\s*([\s\S]*)$/.exec(raw)
  if (!m) return { status: 0, message: raw }
  let message = m[2]
  try {
    const body = JSON.parse(m[2])
    const detail = body && (body.detail ?? body.error)
    if (typeof detail === 'string') message = detail
    else if (Array.isArray(detail) && detail[0]?.msg) message = detail.map(d => d.msg).join('; ')
  } catch {
    /* not JSON: keep the text */
  }
  return { status: Number(m[1]), message }
}

/** The host answers `404 {"detail":"Plugin not found"}` when the profile Desktop's backend was
 *  LAUNCHED with does not have meeting-scribe enabled (Hermes gates plugin routes on the launch
 *  profile, whatever profile is active): a guided empty state, not an error. Enabling it in that
 *  profile is safe: only the owner profile runs the bot (DESIGN §1.5). */
export function isPluginMissing(error) {
  const { status, message } = parseError(error)
  if (status === 404 && /plugin not found/i.test(message)) return true
  return /bridge unavailable|not registered/i.test(message)
}

export function qs(params) {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== ALL)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
  return parts.length ? `?${parts.join('&')}` : ''
}

function rest(path, opts) {
  if (!CTX) return Promise.reject(new Error('meeting-scribe is not registered'))
  return CTX.rest(path, opts)
}

function useScope() {
  const connection = useValue(host.state.connectionId)
  return connection || 'local'
}

/** Poll every `ms` while `active(data)` holds; never after a client error (401/404: gone/disabled). */
export function pollWhile(active, ms = POLL_MS) {
  return query => {
    const state = query?.state
    if (state?.error && parseError(state.error).status < 500) return false
    return state?.data && active(state.data) ? ms : false
  }
}

/** One GET through the plugin's REST door, cached per connection + profile + path. */
function useRest(path, options = {}) {
  const scope = useScope()
  return useQuery({
    queryKey: [Q, scope, path],
    queryFn: () => rest(path),
    enabled: options.enabled !== false && Boolean(path),
    refetchInterval: options.refetchInterval,
    staleTime: options.staleTime ?? 5_000,
    placeholderData: options.keepPrevious ? previous => previous : undefined,
    retry: (count, error) => count < 1 && parseError(error).status >= 500
  })
}

function useInvalidate() {
  const client = useQueryClient()
  return () => client.invalidateQueries({ queryKey: [Q] })
}

function newRequestId() {
  const c = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  return `r${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
}

// ---------------------------------------------------------------------------------------------
// formatting + small pure helpers (exported for the tests)
// ---------------------------------------------------------------------------------------------
function useLocale() {
  const { locale } = useI18n()
  return locale || 'en'
}

function fmt(value, locale, options) {
  if (value === null || value === undefined || value === '') return ''
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return String(value)
  try {
    return new Intl.DateTimeFormat(locale, options).format(d)
  } catch {
    return d.toLocaleString()
  }
}
const fmtDate = (v, l) => fmt(v, l, { dateStyle: 'medium', timeStyle: 'short' })
const fmtEpoch = (s, l) => (typeof s === 'number' ? fmtDate(s * 1000, l) : '')

/** Short list date: time today, weekday this week, else day + month. */
export function fmtShort(value, locale, now = new Date()) {
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return ''
  const days = Math.floor((new Date(now).setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 86400000)
  if (days === 0) return fmt(d, locale, { hour: '2-digit', minute: '2-digit' })
  if (days > 0 && days < 7) return fmt(d, locale, { weekday: 'short' })
  return fmt(d, locale, { day: 'numeric', month: 'short' })
}

export function fmtClock(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0))
  const hh = Math.floor(s / 3600)
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0')
  const ss = String(s % 60).padStart(2, '0')
  return hh ? `${hh}:${mm}:${ss}` : `${mm}:${ss}`
}

export function minutesBetween(a, b) {
  if (!a || !b) return null
  const ms = new Date(b).getTime() - new Date(a).getTime()
  return Number.isFinite(ms) && ms > 0 ? Math.max(1, Math.round(ms / 60000)) : null
}

/** `today|week|month` → inclusive UTC days for the API (`since`/`until`). */
export function dateRange(preset, now = new Date()) {
  const day = d => d.toISOString().slice(0, 10)
  const back = n => day(new Date(now.getTime() - n * 86400000))
  if (preset === 'today') return { since: day(now), until: day(now) }
  if (preset === 'week') return { since: back(6), until: day(now) }
  if (preset === 'month') return { since: back(29), until: day(now) }
  return { since: '', until: '' }
}

/** Library state → one visual tone (dot + label colour). */
export function stateTone(state) {
  if (state === 'done') return 'good'
  if (state === 'failed') return 'bad'
  if (state === 'recording') return 'live'
  if (state === 'empty') return 'muted'
  return 'busy'
}

export function isInProgress(state) {
  return IN_PROGRESS.includes(state)
}

/** Row subtitle: the step under way, or «N people · M tasks». */
export function rowSubtitle(t, m) {
  if (m.state === 'failed') return t(`state.failed`)
  if (m.state === 'empty') return t('state.empty')
  if (isInProgress(m.state)) {
    if (m.waiting_destination) return t('detail.waiting')
    return t(`state.${m.state}`)
  }
  const bits = []
  if (m.people) bits.push(t('library.people', m.people))
  bits.push(m.task_count ? t('library.tasks', m.task_count) : t('library.noTasks'))
  return bits.join(' · ')
}

/** Translate `key`; when the bundle has no such key, return `fallback` instead of the raw key. */
function tOr(t, key, fallback, ...args) {
  const value = t(key, ...args)
  return value === key ? fallback : value
}

function meetingTitle(t, m) {
  return (m && (m.title || m.channel_name)) || t('library.untitled')
}

/** Host palette tokens speakers cycle through (light + dark themes define every one). */
export const SPEAKER_TONES = ['--ui-blue', '--ui-green', '--ui-purple', '--ui-orange', '--ui-cyan', '--ui-yellow', '--ui-red']

/** Deterministic palette token per speaker (stable across renders and meetings). */
export function speakerTone(key) {
  let hash = 0
  for (const ch of String(key || '?')) hash = (hash * 31 + ch.codePointAt(0)) >>> 0
  return `var(${SPEAKER_TONES[hash % SPEAKER_TONES.length]})`
}

function initials(name) {
  const parts = String(name || '?').trim().split(/\s+/).filter(Boolean)
  return ((parts[0]?.[0] || '?') + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase()
}

/** Split `text` around case-insensitive `needle` matches (for <mark>). */
export function splitMatches(text, needle) {
  const value = String(text || '')
  if (!needle) return [{ text: value, match: false }]
  const lower = value.toLowerCase()
  const n = needle.toLowerCase()
  const out = []
  let from = 0
  let at = lower.indexOf(n)
  while (at >= 0) {
    if (at > from) out.push({ text: value.slice(from, at), match: false })
    out.push({ text: value.slice(at, at + n.length), match: true })
    from = at + n.length
    at = lower.indexOf(n, from)
  }
  if (from < value.length) out.push({ text: value.slice(from), match: false })
  return out
}

function highlight(text, needle) {
  return splitMatches(text, needle).map((p, i) => (p.match ? h('mark', { key: i, className: 'ms-mark' }, p.text) : p.text))
}

/** Where the player can stream the listening copy from. `hermes-media://` is Desktop's own
 *  authenticated media protocol: locally it reads the file (Range/seek included); on a remote
 *  connection it proxies Hermes' `/api/files/stream`. Unknown connection kinds fail closed. */
export function audioSources(audio, connectionId, profile, mode) {
  if (!audio || !audio.available || !audio.path) return []
  const file = encodeURIComponent(audio.path)
  if (mode === 'local') return [`hermes-media://stream/${file}`]
  if (!mode || !connectionId) return []
  const scope = [`connectionId=${encodeURIComponent(connectionId)}`, profile ? `profile=${encodeURIComponent(profile)}` : '']
    .filter(Boolean).join('&')
  return [`hermes-media://remote/${file}?${scope}`]
}

/** Status of one task in one destination, as a tone + label key. */
export function sinkView(status, mode) {
  if (mode === 'off') return { tone: 'muted', key: 'off' }
  if (status === 'delivered') return { tone: 'good', key: 'delivered' }
  if (status === 'failed') return { tone: 'bad', key: 'failed' }
  if (status === 'approved') return { tone: 'busy', key: 'approved' }
  if (status === 'dismissed') return { tone: 'muted', key: 'dismissed' }
  if (status === 'skipped') return { tone: 'muted', key: 'skipped' }
  return { tone: 'warn', key: 'pending' }
}

/** `["Alfa=111", …]` ⇄ editor rows; malformed entries survive as rows so nothing is lost silently. */
export function parseProjectChannels(list) {
  return (Array.isArray(list) ? list : []).map(entry => {
    const s = String(entry)
    const at = s.lastIndexOf('=')
    return at < 0 ? { project: s.trim(), channel: '' } : { project: s.slice(0, at).trim(), channel: s.slice(at + 1).trim() }
  })
}

/** Validate editor rows → `{value}` (the list to store) or `{errors: {index: key}}`. */
export function checkProjectChannels(rows, t) {
  const errors = {}
  const seen = new Map()
  const value = []
  rows.forEach((r, i) => {
    const project = String(r.project || '').trim()
    const channel = String(r.channel || '').trim().replace(/^<#(\d+)>$/, '$1')
    if (!project && !channel) return
    if (!project) errors[i] = t('projects.needName')
    else if (!/^\d{5,25}$/.test(channel)) errors[i] = t('projects.needChannel')
    else if (seen.has(project.toLowerCase())) errors[i] = t('projects.duplicate', project)
    seen.set(project.toLowerCase(), i)
    value.push(`${project}=${channel}`)
  })
  return Object.keys(errors).length ? { errors } : { value }
}

// ---------------------------------------------------------------------------------------------
// shared UI bits
// ---------------------------------------------------------------------------------------------
function Dot({ tone, label }) {
  return h('span', { className: `ms-dot ms-tone-${tone}`, role: label ? 'img' : undefined, 'aria-label': label, 'aria-hidden': label ? undefined : 'true' })
}

function Pill({ tone = 'muted', children, title }) {
  return h('span', { className: `ms-pill ms-tone-${tone}`, title }, children)
}

function SectionLabel({ children, id, actions }) {
  return h('div', { className: 'ms-section-label-row' },
    h('h3', { className: 'ms-section-label', id }, children),
    actions || null)
}

function Block({ title, id, actions, children, className = '' }) {
  const headingId = id ? `${id}-title` : undefined
  return h('section', { className: `ms-block ${className}`, 'aria-labelledby': headingId },
    title ? h(SectionLabel, { id: headingId, actions }, title) : null,
    children)
}

function Callout({ tone = 'muted', icon, title, children }) {
  return h('div', { className: `ms-callout ms-callout-${tone}`, role: tone === 'bad' ? 'alert' : undefined },
    h(Codicon, { name: icon || (tone === 'bad' ? 'error' : tone === 'warn' ? 'warning' : 'info'), size: '0.9rem', className: 'ms-callout-icon' }),
    h('div', { className: 'ms-callout-body' }, title ? h('p', { className: 'ms-callout-title' }, title) : null, children))
}

function ListSkeleton({ rows = 6 }) {
  const t = usePluginI18n(ID)
  return h('div', { className: 'ms-skel-list', role: 'status', 'aria-label': t('common.loading') },
    Array.from({ length: rows }, (_, i) => h('div', { key: i, className: 'ms-skel-row' },
      h(Skeleton, { className: 'ms-skel-dot' }),
      h('div', { className: 'ms-skel-lines' }, h(Skeleton, { className: 'ms-skel-a' }), h(Skeleton, { className: 'ms-skel-b' })))))
}

function DetailSkeleton() {
  const t = usePluginI18n(ID)
  return h('div', { className: 'ms-detail-pad', role: 'status', 'aria-label': t('common.loading') },
    h(Skeleton, { className: 'ms-skel-title' }), h(Skeleton, { className: 'ms-skel-meta' }),
    h(Skeleton, { className: 'ms-skel-tabs' }),
    Array.from({ length: 4 }, (_, i) => h(Skeleton, { key: i, className: 'ms-skel-para' })))
}

function Empty({ icon = 'inbox', title, children, action }) {
  return h('div', { className: 'ms-empty' },
    h('div', { className: 'ms-empty-icon' }, h(Codicon, { name: icon, size: '1.25rem' })),
    title ? h('p', { className: 'ms-empty-title' }, title) : null,
    children ? h('div', { className: 'ms-empty-body' }, children) : null,
    action ? h('div', { className: 'ms-empty-action' }, action) : null)
}

/** A backend launched without the plugin: guided, calm, not an error. */
function PluginMissing() {
  const t = usePluginI18n(ID)
  return h(Empty, { icon: 'mic', title: t('disabled.title') },
    h('p', null, t('disabled.body')),
    h('p', { className: 'ms-muted' }, t('disabled.steps')))
}

function Failure({ error, onRetry, missingIsMeeting = false }) {
  const t = usePluginI18n(ID)
  if (isPluginMissing(error)) return h(PluginMissing, null)
  const { status, message } = parseError(error)
  const text = status === 404 && missingIsMeeting ? t('error.notFound') : status === 0 && !message ? t('error.offline') : t('error.generic', message || String(status))
  return h(Empty, {
    icon: 'warning', title: t('error.title'),
    action: onRetry ? h(Button, { type: 'button', variant: 'secondary', size: 'sm', onClick: onRetry }, t('common.retry')) : null
  }, h('p', null, text))
}

function ExternalLink({ href, children, icon = 'link-external' }) {
  if (!href || !/^https:\/\//i.test(href)) return null
  const open = event => {
    event.preventDefault()
    if (CTX?.os?.openExternal) CTX.os.openExternal(href)
  }
  return h('a', { className: 'ms-link', href, onClick: open, rel: 'noreferrer noopener', target: '_blank' },
    children, h(Codicon, { name: icon, size: '0.7rem' }))
}

function FilterSelect({ label, value, onChange, options, allLabel, id }) {
  return h('div', { className: 'ms-filter' },
    h('label', { className: 'ms-filter-label', htmlFor: id }, label),
    h(Select, { value, onValueChange: onChange },
      h(SelectTrigger, { id, size: 'sm', className: 'ms-filter-trigger', 'aria-label': label }, h(SelectValue, null)),
      h(SelectContent, null,
        h(SelectItem, { value: ALL }, allLabel),
        options.map(o => h(SelectItem, { key: o.value, value: o.value }, o.label)))))
}

// ---------------------------------------------------------------------------------------------
// library (left)
// ---------------------------------------------------------------------------------------------
const STATE_FILTERS = ['recording', 'processing', 'done', 'failed', 'empty']
const SOURCE_FILTERS = ['discord', 'google_meet']
const DATE_PRESETS = ['today', 'week', 'month', 'custom']

function useDebounced(value, ms = 250) {
  const [out, setOut] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setOut(value), ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return out
}

function listPath(filters, limit, date) {
  const range = date === 'custom' ? { since: filters.since, until: filters.until } : dateRange(date)
  return `/v1/meetings${qs({
    q: filters.q.trim(), state: filters.state, source: filters.source, channel: filters.channel,
    project: filters.project, since: range.since, until: range.until, limit
  })}`
}

export function LibraryList({ selected, onSelect }) {
  const t = usePluginI18n(ID)
  const locale = useLocale()
  const filters = useValue($filters)
  const [text, setText] = useState(filters.q)
  const [date, setDate] = useState(filters.date || ALL)
  const [showFilters, setShowFilters] = useState(false)
  const [limit, setLimit] = useState(PAGE_SIZE)
  const q = useDebounced(text)
  useEffect(() => { if (q !== filters.q) $filters.set({ ...filters, q }) }, [q])
  const set = patch => { $filters.set({ ...filters, ...patch }); setLimit(PAGE_SIZE) }
  const query = useRest(listPath(filters, limit, date), {
    keepPrevious: true,
    refetchInterval: pollWhile(d => (d.items || []).some(m => isInProgress(m.state)), LIST_POLL_MS)
  })
  const data = query.data
  const facets = data?.facets || {}
  const active = [filters.state, filters.source, filters.channel, filters.project].filter(v => v !== ALL).length + (date !== ALL ? 1 : 0)

  const clear = () => {
    setText('')
    setDate(ALL)
    $filters.set({ q: '', state: ALL, source: ALL, channel: ALL, project: ALL, since: '', until: '' })
  }
  const move = delta => {
    const items = data?.items || []
    const at = items.findIndex(m => m.id === selected)
    const next = items[Math.max(0, Math.min(items.length - 1, at + delta))]
    if (next) onSelect(next.id)
  }

  let body
  if (query.isLoading) body = h(ListSkeleton, null)
  else if (query.isError) body = h(Failure, { error: query.error, onRetry: () => query.refetch() })
  else if (!data?.items?.length) {
    body = facets.total
      ? h(Empty, { icon: 'search', title: t('library.noMatchTitle'), action: h(Button, { type: 'button', variant: 'ghost', size: 'sm', onClick: clear }, t('library.clear')) }, h('p', null, t('library.noMatchBody')))
      : h(Empty, { icon: 'mic', title: t('library.emptyTitle') }, h('p', null, t('library.emptyBody')))
  } else {
    body = h(Fragment, null,
      h('ul', {
        className: 'ms-rows', role: 'listbox', 'aria-label': t('title'),
        onKeyDown: e => {
          if (e.key === 'ArrowDown') { e.preventDefault(); move(1) }
          if (e.key === 'ArrowUp') { e.preventDefault(); move(-1) }
        }
      }, data.items.map(m => h(MeetingRow, { key: m.id, meeting: m, active: m.id === selected, locale, onSelect }))),
      data.next_cursor
        ? h('div', { className: 'ms-more' }, h(Button, { type: 'button', variant: 'ghost', size: 'sm', onClick: () => setLimit(l => Math.min(100, l + PAGE_SIZE)), disabled: limit >= 100 || query.isFetching }, t('library.more')))
        : null)
  }

  const channelOptions = (facets.channels || []).map(c => ({ value: c.id, label: `#${c.name}` }))
  const projectOptions = (facets.projects || []).map(p => ({ value: p.name, label: p.name }))

  return h('aside', { className: 'ms-master', 'aria-label': t('title') },
    h('div', { className: 'ms-master-head' },
      h('div', { className: 'ms-search-row' },
        h('div', { className: 'ms-grow' },
          h(SearchField, { variant: 'box', 'aria-label': t('library.searchLabel'), placeholder: t('library.search'), value: text, onChange: setText, loading: query.isFetching && Boolean(text) })),
        h(Button, {
          type: 'button', variant: showFilters || active ? 'secondary' : 'ghost', size: 'sm', className: 'ms-filter-toggle',
          'aria-expanded': showFilters, 'aria-controls': 'ms-filters', onClick: () => setShowFilters(v => !v)
        }, h(Codicon, { name: 'filter', size: '0.8rem' }), t('library.filters'), active ? h(Badge, { size: 'xs' }, String(active)) : null)),
      showFilters
        ? h('div', { className: 'ms-filters', id: 'ms-filters' },
          h(FilterSelect, {
            id: 'ms-f-date', label: t('library.date'), value: date, allLabel: t('library.anyDate'),
            options: DATE_PRESETS.map(p => ({ value: p, label: t(`library.dates.${p}`) })),
            onChange: v => { setDate(v); $filters.set({ ...filters, date: v }); setLimit(PAGE_SIZE) }
          }),
          date === 'custom'
            ? h('div', { className: 'ms-daterange' },
              h(Input, { type: 'date', size: 'sm', 'aria-label': t('library.since'), value: filters.since, onChange: e => set({ since: e.target.value }), className: 'ms-date' }),
              h(Input, { type: 'date', size: 'sm', 'aria-label': t('library.until'), value: filters.until, onChange: e => set({ until: e.target.value }), className: 'ms-date' }))
            : null,
          h(FilterSelect, { id: 'ms-f-project', label: t('library.project'), value: filters.project, allLabel: t('library.anyProject'), options: projectOptions, onChange: v => set({ project: v }) }),
          h(FilterSelect, {
            id: 'ms-f-state', label: t('library.state'), value: filters.state, allLabel: t('library.anyState'),
            options: STATE_FILTERS.map(s => ({ value: s, label: `${t(`stateGroup.${s}`)}${facets.states?.[s] ? ` · ${facets.states[s]}` : ''}` })),
            onChange: v => set({ state: v })
          }),
          h(FilterSelect, { id: 'ms-f-channel', label: t('library.channel'), value: filters.channel, allLabel: t('library.anyChannel'), options: channelOptions, onChange: v => set({ channel: v }) }),
          h(FilterSelect, {
            id: 'ms-f-source', label: t('library.source'), value: filters.source, allLabel: t('library.anySource'),
            options: SOURCE_FILTERS.map(s => ({ value: s, label: t(`source.${s}`) })), onChange: v => set({ source: v })
          }),
          active ? h(Button, { type: 'button', variant: 'ghost', size: 'sm', onClick: clear, className: 'ms-clear' }, t('library.clear')) : null)
        : null,
      data ? h('p', { className: 'ms-count', 'aria-live': 'polite' }, t('library.count', data.items.length, data.total ?? data.items.length)) : null),
    h('div', { className: 'ms-master-body' }, body))
}

function MeetingRow({ meeting: m, active, locale, onSelect }) {
  const t = usePluginI18n(ID)
  const tone = stateTone(m.state)
  const ref = useRef(null)
  useEffect(() => { if (active && ref.current && document.activeElement?.closest?.('.ms-rows')) ref.current.focus() }, [active])
  return h('li', { role: 'presentation' },
    h('button', {
      ref, type: 'button', role: 'option', 'aria-selected': active, className: `ms-row${active ? ' is-active' : ''}`,
      tabIndex: active ? 0 : -1, onClick: () => onSelect(m.id), 'data-meeting': m.id
    },
    h('span', { className: 'ms-row-lead' },
      tone === 'good' ? h(Codicon, { name: 'check', size: '0.8rem', className: 'ms-tone-text-good' })
        : tone === 'bad' ? h(Codicon, { name: 'error', size: '0.8rem', className: 'ms-tone-text-bad' })
          : tone === 'muted' ? h(Codicon, { name: 'circle-slash', size: '0.75rem', className: 'ms-tone-text-muted' })
            : h(Dot, { tone })),
    h('span', { className: 'ms-row-main' },
      h('span', { className: 'ms-row-title' }, meetingTitle(t, m)),
      h('span', { className: `ms-row-sub${tone === 'bad' ? ' ms-tone-text-bad' : ''}` }, rowSubtitle(t, m))),
    h('span', { className: 'ms-row-meta' },
      h('span', null, fmtShort(m.started_at, locale)),
      m.private ? h(Codicon, { name: 'lock', size: '0.7rem', 'aria-label': t('private.label'), title: t('private.hint') }) : null,
      m.source === 'google_meet' ? h(Codicon, { name: 'device-camera-video', size: '0.7rem', 'aria-label': t('source.google_meet') }) : null)))
}

// ---------------------------------------------------------------------------------------------
// detail (right)
// ---------------------------------------------------------------------------------------------
const TABS = ['summary', 'transcript', 'tasks', 'processing']

export function MeetingDetail({ id, onBack }) {
  const t = usePluginI18n(ID)
  const locale = useLocale()
  const tab = useValue($tab)
  const [commandId, setCommandId] = useState(null)
  const audioRef = useRef(null)
  useEffect(() => setCommandId(null), [id])
  const query = useRest(`/v1/meetings/${encodeURIComponent(id)}`, {
    refetchInterval: pollWhile(d => isInProgress(d.meeting?.state) || d.job?.state === 'running' || ['queued', 'running'].includes(d.command?.state))
  })
  const d = query.data
  if (query.isLoading) return h(DetailSkeleton, null)
  if (query.isError) return h('div', { className: 'ms-detail-pad' }, onBack ? h(BackButton, { onBack }) : null, h(Failure, { error: query.error, onRetry: () => query.refetch(), missingIsMeeting: true }))
  if (!d) return null
  const m = d.meeting
  const minutes = minutesBetween(m.started_at, m.ended_at)
  const openTasks = (d.tasks || []).filter(x => x.status !== 'dismissed')
  const cmd = commandId || (d.command && ['queued', 'running', 'unknown'].includes(d.command.state) ? d.command.id : null)
  const seek = seconds => {
    const el = audioRef.current
    if (!el) return
    el.currentTime = Math.max(0, Number(seconds) || 0)
    el.play?.().catch(() => {})
  }

  const meta = [
    fmtDate(m.started_at, locale),
    m.channel_name ? (m.source === 'google_meet' ? m.channel_name : `#${m.channel_name}`) : null,
    minutes ? t('detail.duration', minutes) : null,
    m.people ? t('library.people', m.people) : null
  ].filter(Boolean)

  let panel
  if (tab === 'transcript') panel = h(TranscriptTab, { id, total: d.transcript_total, onSeek: d.audio?.available ? seek : null })
  else if (tab === 'tasks') panel = h(TasksTab, { tasks: d.tasks || [], destinations: d.destinations || {} })
  else if (tab === 'processing') panel = h(ProcessingTab, { detail: d, commandId: cmd, onSubmitted: setCommandId, onFinished: () => query.refetch() })
  else panel = h(SummaryTab, { detail: d, tasks: openTasks, audioRef, onSubmitted: setCommandId, commandId: cmd, onFinished: () => query.refetch() })

  return h('article', { className: 'ms-detail', 'aria-labelledby': 'ms-detail-title' },
    h('header', { className: 'ms-detail-head' },
      onBack ? h(BackButton, { onBack }) : null,
      h('div', { className: 'ms-title-row' },
        h('h2', { className: 'ms-detail-title', id: 'ms-detail-title' }, meetingTitle(t, m)),
        h(StatePill, { state: m.state })),
      h('p', { className: 'ms-detail-meta' }, meta.join(' · ')),
      h('div', { className: 'ms-chips' },
        h(Pill, null, h(Codicon, { name: m.source === 'google_meet' ? 'device-camera-video' : 'comment-discussion', size: '0.7rem' }), t(`source.${m.source}`) || m.source),
        m.private ? h(Pill, { tone: 'warn', title: t('private.hint') }, h(Codicon, { name: 'lock', size: '0.7rem' }), t('private.label')) : null,
        (d.projects || []).map(p => h(Pill, { key: p, tone: 'accent' }, h(Codicon, { name: 'folder', size: '0.7rem' }), p)))),
    h(Tabs, { value: tab, onValueChange: v => $tab.set(v), className: 'ms-tabs' },
      h(TabsList, { className: 'ms-tabs-list', 'aria-label': meetingTitle(t, m) },
        TABS.map(k => h(TabsTrigger, { key: k, value: k, className: 'ms-tab' },
          t(`detail.tabs.${k}`),
          k === 'tasks' && openTasks.length ? h('span', { className: 'ms-tab-count' }, String(openTasks.length)) : null,
          k === 'processing' && (m.state === 'failed' || d.command?.state === 'unknown') ? h(Dot, { tone: 'bad' }) : null,
          k === 'processing' && isInProgress(m.state) && m.state !== 'recording' ? h(Dot, { tone: 'busy' }) : null)))),
    h('div', { className: 'ms-detail-body', role: 'tabpanel', 'aria-label': t(`detail.tabs.${tab}`) }, panel))
}

function BackButton({ onBack }) {
  const t = usePluginI18n(ID)
  return h(Button, { type: 'button', variant: 'ghost', size: 'sm', className: 'ms-back', onClick: onBack },
    h(Codicon, { name: 'arrow-left', size: '0.8rem' }), t('common.back'))
}

function StatePill({ state }) {
  const t = usePluginI18n(ID)
  const tone = stateTone(state)
  return h(Pill, { tone: tone === 'live' ? 'bad' : tone }, tone === 'live' || tone === 'busy' ? h(Dot, { tone }) : null, t(`state.${state}`))
}

// -- summary ------------------------------------------------------------------------------------
function SummaryTab({ detail: d, tasks, audioRef, commandId, onSubmitted, onFinished }) {
  const t = usePluginI18n(ID)
  const m = d.meeting
  const n = d.notes
  const notices = h(Fragment, null,
    m.partial ? h(Callout, { tone: 'warn' }, h('p', null, t('detail.partial'))) : null,
    d.waiting_destination ? h(Callout, { tone: 'warn', title: t('detail.waiting') }, h('p', null, t('detail.waitingHelp'))) : null,
    d.dm_notes ? h(Callout, { tone: 'muted', title: t('detail.dmNotes') }, h('p', null, t('detail.dmNotesHelp'))) : null)
  const audio = h(AudioBlock, { id: m.id, audio: d.audio || {}, audioRef, commandId, onSubmitted, onFinished })
  if (!n) {
    const text = m.state === 'empty' ? t('detail.empty') : m.state === 'recording' ? t('detail.recording') : m.state === 'failed' ? t('detail.noNotesFailed') : t('detail.noNotes')
    return h('div', { className: 'ms-stack' }, notices,
      h(Empty, { icon: m.state === 'empty' ? 'circle-slash' : m.state === 'failed' ? 'warning' : m.state === 'recording' ? 'record' : 'loading~spin' },
        h('p', null, text),
        m.state === 'failed' ? h(Button, { type: 'button', variant: 'secondary', size: 'sm', onClick: () => $tab.set('processing') }, t('detail.tabs.processing')) : null),
      d.audio?.available || d.audio?.can_prepare ? audio : null)
  }
  return h('div', { className: 'ms-stack' }, notices,
    n.tldr ? h('p', { className: 'ms-lead' }, n.tldr) : null,
    n.summary ? h(Block, { title: t('detail.summary'), id: 'ms-summary' }, h('p', { className: 'ms-prose' }, n.summary)) : null,
    h('div', { className: 'ms-grid2' },
      h(Block, { title: t('detail.decisions'), id: 'ms-decisions' },
        n.decisions?.length ? h('ul', { className: 'ms-bullets' }, n.decisions.map((x, i) => h('li', { key: i }, x))) : h('p', { className: 'ms-muted' }, t('detail.noDecisions'))),
      h(Block, { title: t('detail.questions'), id: 'ms-questions' },
        n.open_questions?.length ? h('ul', { className: 'ms-bullets ms-bullets-q' }, n.open_questions.map((x, i) => h('li', { key: i }, x))) : h('p', { className: 'ms-muted' }, t('detail.noQuestions')))),
    tasks.length
      ? h(Block, {
        title: t('detail.pendingTasks'), id: 'ms-pending',
        actions: h(Button, { type: 'button', variant: 'link', size: 'sm', onClick: () => $tab.set('tasks') }, t('detail.allTasks', tasks.length))
      }, h('ul', { className: 'ms-mini-tasks' }, tasks.slice(0, 5).map(x => h('li', { key: x.id, className: 'ms-mini-task' },
        h('span', { className: 'ms-mini-title' }, x.title),
        h('span', { className: 'ms-mini-meta' }, [x.owner_name || t('tasks.noOwner'), x.project || null, x.due || null].filter(Boolean).join(' · '))))))
      : null,
    n.topics?.length
      ? h(Block, { title: t('detail.topics'), id: 'ms-topics' },
        h('div', { className: 'ms-topics' }, n.topics.map((tp, i) => h('div', { key: i, className: 'ms-topic' },
          h('p', { className: 'ms-topic-title' }, tp.title),
          tp.points?.length ? h('ul', { className: 'ms-bullets' }, tp.points.map((p, j) => h('li', { key: j }, p))) : null))))
      : null,
    audio)
}

function AudioBlock({ id, audio, audioRef, commandId, onSubmitted, onFinished }) {
  const t = usePluginI18n(ID)
  const connectionId = useValue(host.state.connectionId)
  const profile = useValue(host.state.profile)
  const scope = useScope()
  // The registry tells local from remote; unknown connections fail closed.
  const connection = useQuery({
    queryKey: [Q, scope, 'connection-kind'],
    queryFn: async () => (await host.connections()).find(c => c.id === connectionId) || null,
    enabled: Boolean(audio.available && connectionId), retry: false, staleTime: 60_000
  })
  const mode = connectionId ? connection.data?.kind : 'local'
  const sources = useMemo(() => audioSources(audio, connectionId || 'local', profile, mode), [audio?.path, audio?.available, connectionId, profile, mode])
  const [attempt, setAttempt] = useState(0)
  useEffect(() => setAttempt(0), [audio?.path, connectionId, profile, mode])

  let body
  if (audio.available) {
    if (connectionId && connection.isLoading) body = h(Skeleton, { className: 'ms-skel-audio' })
    else if (attempt >= sources.length) body = h('p', { className: 'ms-muted' }, t('audio.failed'))
    else {
      body = h(Fragment, null,
        h('audio', {
          key: sources[attempt], ref: audioRef, className: 'ms-audio', controls: true, preload: 'metadata', src: sources[attempt],
          'aria-label': t('audio.label'), onError: () => setAttempt(n => n + 1)
        }),
        h('p', { className: 'ms-hint' }, t('audio.ready'), audio.original ? ` ${t('audio.original')}` : ''))
    }
  } else if (audio.can_prepare) {
    body = h(PrepareAudio, { id, commandId, onSubmitted, onFinished })
  } else {
    body = h('p', { className: 'ms-muted' }, tOr(t, `audio.${audio.reason}`, t('audio.not_retained')))
  }
  return h(Block, { title: t('audio.title'), id: 'ms-audio', className: 'ms-audio-block' }, body)
}

function PrepareAudio({ id, commandId, onSubmitted, onFinished }) {
  const t = usePluginI18n(ID)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const cmd = useCommand(commandId, onFinished)
  const running = cmd.data && ['queued', 'running'].includes(cmd.data.state) && cmd.data.action === 'prepare_audio'
  const submit = async () => {
    setError('')
    setBusy(true)
    const rid = newRequestId()
    try {
      await rest(`/v1/meetings/${encodeURIComponent(id)}/commands`, { method: 'POST', body: { request_id: rid, action: 'prepare_audio', confirm: true } })
      onSubmitted(rid)
    } catch (e) {
      setError(parseError(e).message)
    } finally {
      setBusy(false)
    }
  }
  const failed = cmd.data?.action === 'prepare_audio' && cmd.data.state === 'failed'
  return h('div', { className: 'ms-stack-sm' },
    h('p', { className: 'ms-muted' }, t('audio.multitrack')),
    h('div', { className: 'ms-inline' },
      h(Button, { type: 'button', variant: 'secondary', size: 'sm', disabled: busy || running, onClick: submit },
        h(Codicon, { name: running ? 'loading~spin' : 'unmute', size: '0.8rem' }), running ? t('audio.preparing') : t('audio.prepare'))),
    failed ? h('p', { className: 'ms-error' }, t('audio.prepareFailed', cmd.data.error || '')) : null,
    error ? h('p', { className: 'ms-error', role: 'alert' }, error) : null)
}

// -- transcript ---------------------------------------------------------------------------------
function TranscriptTab({ id, total, onSeek }) {
  const t = usePluginI18n(ID)
  const first = useRest(total ? `/v1/meetings/${encodeURIComponent(id)}/transcript${qs({ limit: TRANSCRIPT_PAGE })}` : '', { staleTime: 30_000 })
  const [extra, setExtra] = useState({ items: [], cursor: undefined, error: null, busy: false })
  const [text, setText] = useState('')
  const cancelled = useRef(false)
  useEffect(() => {
    cancelled.current = false
    setExtra({ items: [], cursor: undefined, error: null, busy: false })
    return () => { cancelled.current = true }
  }, [id, first.data])

  const items = [...(first.data?.items || []), ...extra.items]
  const nextCursor = extra.cursor === undefined ? first.data?.next_cursor : extra.cursor
  const needle = text.trim().toLowerCase()
  const shown = needle ? items.filter(u => `${u.speaker} ${u.text}`.toLowerCase().includes(needle)) : items

  const load = async all => {
    let cursor = nextCursor
    setExtra(e => ({ ...e, busy: true, error: null }))
    try {
      const acc = []
      do {
        const page = await rest(`/v1/meetings/${encodeURIComponent(id)}/transcript${qs({ cursor, limit: 500 })}`)
        acc.push(...(page.items || []))
        cursor = page.next_cursor
      } while (all && cursor && !cancelled.current)
      if (!cancelled.current) setExtra(e => ({ items: [...e.items, ...acc], cursor: cursor || null, error: null, busy: false }))
    } catch (error) {
      if (!cancelled.current) setExtra(e => ({ ...e, busy: false, error }))
    }
  }

  if (!total) return h(Empty, { icon: 'note' }, h('p', null, t('transcript.empty')))
  if (first.isLoading) return h(ListSkeleton, { rows: 8 })
  if (first.isError) return h(Failure, { error: first.error, onRetry: () => first.refetch(), missingIsMeeting: true })
  let previous = null
  return h('div', { className: 'ms-stack' },
    h('div', { className: 'ms-transcript-bar' },
      h('div', { className: 'ms-grow' }, h(SearchField, { variant: 'box', 'aria-label': t('transcript.searchLabel'), placeholder: t('transcript.search'), value: text, onChange: setText })),
      h('span', { className: 'ms-count', 'aria-live': 'polite' }, needle ? t('transcript.matches', shown.length) : t('transcript.lines', items.length, total))),
    needle && nextCursor ? h('p', { className: 'ms-hint' }, t('transcript.partialSearch')) : null,
    shown.length === 0 && needle
      ? h(Empty, { icon: 'search' }, h('p', null, t('transcript.noMatch')))
      : h('ol', { className: 'ms-utts' }, shown.map(u => {
        const who = u.speaker || u.speaker_id || '?'
        const same = !needle && previous === who
        previous = who
        const tone = speakerTone(u.speaker_id || who)
        return h('li', { key: u.id, className: `ms-utt${same ? ' is-cont' : ''}` },
          h('span', { className: 'ms-avatar', style: { '--ms-tone': tone }, 'aria-hidden': 'true' }, same ? '' : initials(who)),
          h('div', { className: 'ms-utt-main' },
            same ? null : h('div', { className: 'ms-utt-head' },
              h('span', { className: 'ms-speaker', style: { '--ms-tone': tone } }, who),
              onSeek
                ? h('button', { type: 'button', className: 'ms-time is-link', onClick: () => onSeek(u.t0), 'aria-label': t('transcript.jump', fmtClock(u.t0)) }, fmtClock(u.t0))
                : h('span', { className: 'ms-time' }, fmtClock(u.t0))),
            h('p', { className: 'ms-utt-text' },
              same && onSeek ? h('button', { type: 'button', className: 'ms-time ms-time-inline is-link', onClick: () => onSeek(u.t0), 'aria-label': t('transcript.jump', fmtClock(u.t0)) }, fmtClock(u.t0)) : null,
              highlight(u.text, needle))))
      })),
    extra.error ? h('p', { className: 'ms-error', role: 'alert' }, parseError(extra.error).message) : null,
    nextCursor
      ? h('div', { className: 'ms-inline' },
        h(Button, { type: 'button', variant: 'secondary', size: 'sm', disabled: extra.busy, onClick: () => load(false) }, extra.busy ? t('common.loading') : t('transcript.more')),
        h(Button, { type: 'button', variant: 'ghost', size: 'sm', disabled: extra.busy, onClick: () => load(true) }, t('transcript.all')))
      : null)
}

// -- tasks --------------------------------------------------------------------------------------
function TasksTab({ tasks, destinations }) {
  const t = usePluginI18n(ID)
  if (!tasks.length) return h(Empty, { icon: 'checklist' }, h('p', null, t('tasks.empty')))
  const kanbanOn = destinations.kanban && destinations.kanban !== 'off'
  return h('ul', { className: 'ms-tasks' }, tasks.map(task => {
    const dismissed = task.status === 'dismissed'
    const sinks = task.sinks || {}
    const rows = [
      { key: 'discord', view: task.discord ? { tone: 'good', key: 'posted' } : { tone: 'muted', key: 'notPosted' }, link: task.discord?.url ? h(ExternalLink, { href: task.discord.url }, t('tasks.openDiscord')) : null },
      { key: 'kanban', view: sinkView(sinks.kanban?.status, destinations.kanban), link: kanbanOn && sinks.kanban?.status === 'delivered' ? h(Button, { type: 'button', variant: 'link', size: 'xs', className: 'ms-linkbtn', onClick: () => host.navigate(KANBAN_ROUTE) }, t('tasks.openKanban'), h(Codicon, { name: 'arrow-right', size: '0.7rem' })) : null },
      { key: 'linear', view: sinkView(sinks.linear?.status, destinations.linear), link: sinks.linear?.url ? h(ExternalLink, { href: sinks.linear.url }, t('tasks.openLinear')) : null }
    ]
    return h('li', { key: task.id, className: `ms-task${dismissed ? ' is-dismissed' : ''}` },
      h('div', { className: 'ms-task-head' },
        h('p', { className: 'ms-task-title' }, task.title),
        dismissed ? h(Pill, null, t('tasks.dismissed')) : null),
      task.description ? h('p', { className: 'ms-task-desc' }, task.description) : null,
      h('dl', { className: 'ms-task-meta' },
        h('div', null, h('dt', null, t('tasks.owner')), h('dd', null, task.owner_name || t('tasks.noOwner'))),
        h('div', null, h('dt', null, t('tasks.project')), h('dd', null, task.project || t('tasks.noProject'))),
        task.due ? h('div', null, h('dt', null, t('tasks.due')), h('dd', null, task.due)) : null),
      h('div', { className: 'ms-dests', role: 'group', 'aria-label': t('tasks.destinations') },
        rows.map(r => h('div', { key: r.key, className: 'ms-dest' },
          h('span', { className: 'ms-dest-name' }, t(`tasks.${r.key}`)),
          h(Pill, { tone: r.view.tone }, t(`tasks.status.${r.view.key}`)),
          r.link))),
      task.quote ? h('blockquote', { className: 'ms-quote', title: t('tasks.quote') }, `“${task.quote}”`) : null)
  }))
}

// -- processing ---------------------------------------------------------------------------------
function useCommand(commandId, onFinished) {
  const invalidate = useInvalidate()
  const command = useRest(commandId ? `/v1/commands/${encodeURIComponent(commandId)}` : '', {
    staleTime: 0,
    refetchInterval: query => {
      const state = query?.state
      if (state?.error && parseError(state.error).status < 500) return false
      return !state?.data || ['queued', 'running'].includes(state.data.state) ? POLL_MS : false
    }
  })
  const settled = command.data && ['done', 'failed', 'unknown', 'acknowledged'].includes(command.data.state)
  const reported = useRef(null)
  useEffect(() => {
    if (settled && reported.current !== command.data.id) {
      reported.current = command.data.id
      invalidate()
      onFinished?.()
    }
  }, [settled, command.data?.id])
  return command
}

function ProcessingTab({ detail: d, commandId, onSubmitted, onFinished }) {
  const t = usePluginI18n(ID)
  const locale = useLocale()
  const m = d.meeting
  const job = d.job
  const history = d.history || []
  const stepName = s => tOr(t, `stage.${s}`, s || '')
  let current
  if (m.state === 'failed' && job) {
    current = h(Callout, { tone: 'bad', title: t('processing.ev.failed', stepName(job.failed_stage || job.stage)) },
      h('p', null, t(`processing.problem.${job.problem || 'unknown'}`)),
      job.error ? h('details', { className: 'ms-details' }, h('summary', null, t('common.details')), h('pre', { className: 'ms-code' }, job.error)) : null)
  } else if (job?.state === 'queued' && job.error) {
    current = h(Callout, { tone: 'warn', title: t('processing.ev.waiting') },
      h('p', null, t(`processing.problem.${job.problem || 'unknown'}`), ' ', t('processing.retryAuto')),
      h('details', { className: 'ms-details' }, h('summary', null, t('common.details')), h('pre', { className: 'ms-code' }, job.error)))
  } else {
    current = h('div', { className: 'ms-current' },
      h(StatePill, { state: m.state }),
      job && isInProgress(m.state) && job.attempts > 1 ? h('span', { className: 'ms-hint' }, t('processing.attempts', job.attempts)) : null)
  }
  return h('div', { className: 'ms-stack' },
    h(Block, { title: t('processing.current'), id: 'ms-current' }, current),
    h(Block, { title: t('processing.history'), id: 'ms-history' },
      history.length
        ? h('ol', { className: 'ms-timeline' }, history.map((e, i) => {
          const tone = e.kind === 'failed' ? 'bad' : e.kind === 'processed' ? 'good' : e.kind === 'running' ? 'busy' : e.kind === 'waiting_destination' || e.kind === 'waiting' ? 'warn' : 'muted'
          let label
          if (e.kind === 'running') label = t('processing.ev.running', stepName(e.stage))
          else if (e.kind === 'failed') label = t('processing.ev.failed', stepName(e.stage))
          else if (e.kind === 'command') label = commandLabel(e, t)
          else label = tOr(t, `processing.ev.${e.kind}`, e.kind)
          return h('li', { key: i, className: 'ms-tl-item' },
            h(Dot, { tone }),
            h('div', { className: 'ms-tl-main' },
              h('span', { className: 'ms-tl-label' }, label, e.kind === 'command' ? h(Pill, { tone: e.state === 'failed' || e.state === 'unknown' ? 'bad' : e.state === 'done' ? 'good' : 'muted' }, tOr(t, `status.cmd.${e.state}`, e.state)) : null),
              e.at ? h('span', { className: 'ms-tl-time' }, fmtEpoch(e.at, locale)) : null))
        }))
        : h('p', { className: 'ms-muted' }, t('processing.none'))),
    h(ReprocessBlock, { meeting: m, job, commandId, onSubmitted, onFinished }))
}

function ReprocessBlock({ meeting, job, commandId, onSubmitted, onFinished }) {
  const t = usePluginI18n(ID)
  const [open, setOpen] = useState(false)
  const [stage, setStage] = useState('analyze')
  const command = useCommand(commandId, onFinished)
  const status = useRest('/v1/status', { staleTime: 15_000 })
  const cmd = command.data
  const commandError = command.error ? parseError(command.error) : null
  const [ackError, setAckError] = useState('')
  const recording = meeting.state === 'recording'
  const running = job?.state === 'running'
  const empty = meeting.state === 'empty'
  const pending = cmd && ['queued', 'running', 'unknown'].includes(cmd.state)
  const stages = meeting.source === 'google_meet' ? STAGES.filter(s => s !== 'transcribe') : STAGES

  const acknowledge = async () => {
    setAckError('')
    try {
      await rest(`/v1/commands/${encodeURIComponent(cmd.id)}/acknowledge`, { method: 'POST', body: { confirm: true } })
      command.refetch?.()
      onFinished?.()
    } catch (error) {
      setAckError(parseError(error).message)
    }
  }
  const submit = async () => {
    const rid = newRequestId()
    try {
      await rest(`/v1/meetings/${encodeURIComponent(meeting.id)}/commands`, { method: 'POST', body: { request_id: rid, action: 'reprocess', stage, confirm: true } })
    } catch (error) {
      throw new Error(parseError(error).message)
    }
    onSubmitted(rid)
  }

  let line = null
  let tone = 'muted'
  if (cmd) {
    line = cmd.state === 'failed' ? t('processing.cmd.failed', cmd.error || '') : tOr(t, `processing.cmd.${cmd.state}`, cmd.state)
    if (cmd.state === 'running' && cmd.stalled) line = t('processing.cmd.stalled')
    tone = cmd.state === 'failed' || cmd.state === 'unknown' ? 'bad' : cmd.state === 'done' ? 'good' : 'busy'
  } else if (commandError) {
    line = commandError.status === 404 ? t('processing.cmd.gone') : t('processing.cmd.lost', commandError.message)
    tone = 'bad'
  }
  const hint = recording ? t('processing.busyRecording') : running ? t('processing.busyRunning') : empty ? t('processing.busyEmpty') : null
  const stale = status.data?.worker?.state && status.data.worker.state !== 'recent'

  return h(Block, { title: t('processing.reprocess').replace('…', ''), id: 'ms-reprocess' },
    h('p', { className: 'ms-muted' }, t('processing.reprocessHelp')),
    h('div', { className: 'ms-inline' },
      h(Button, { type: 'button', variant: 'secondary', size: 'sm', disabled: recording || running || pending || empty, onClick: () => setOpen(true) },
        h(Codicon, { name: 'refresh', size: '0.8rem' }), t('processing.reprocess')),
      hint ? h('span', { className: 'ms-hint' }, hint) : null),
    line ? h('p', { className: `ms-cmd ms-tone-text-${tone}`, role: 'status', 'aria-live': 'polite' }, h(Dot, { tone }), line) : null,
    pending && stale ? h('p', { className: 'ms-hint' }, t('processing.workerStale')) : null,
    cmd?.state === 'unknown' ? h('div', null, h(Button, { type: 'button', variant: 'secondary', size: 'sm', onClick: acknowledge }, t('processing.acknowledge'))) : null,
    ackError ? h('p', { className: 'ms-error', role: 'alert' }, ackError) : null,
    h(ConfirmDialog, {
      open, onClose: () => setOpen(false), onConfirm: submit, title: t('reprocess.title'), description: t('reprocess.description'),
      confirmLabel: t('reprocess.confirm'), busyLabel: t('reprocess.busy'), cancelLabel: t('common.cancel')
    },
    h('div', { className: 'ms-choices', role: 'radiogroup', 'aria-label': t('reprocess.from') },
      h('p', { className: 'ms-filter-label' }, t('reprocess.from')),
      stages.map(s => h('button', {
        key: s, type: 'button', role: 'radio', 'aria-checked': stage === s, className: `ms-choice${stage === s ? ' is-active' : ''}`,
        onClick: () => setStage(s)
      },
      h('span', { className: 'ms-radio-dot', 'aria-hidden': 'true' }),
      h('span', null, h('span', { className: 'ms-choice-title' }, t(`reprocess.${s}`)), h('span', { className: 'ms-choice-help' }, t(`reprocess.${s}Help`))))))))
}

/** What a page action asked for, in plain words («Reprocess from “Transcription”», «Prepare audio»). */
export function commandLabel(c, t) {
  if (c.action === 'reprocess') return t('processing.actions.reprocess', tOr(t, `reprocess.${c.stage}`, t('processing.actions.unknown')))
  return tOr(t, `processing.actions.${c.action}`, t('processing.actions.unknown'))
}

// ---------------------------------------------------------------------------------------------
// status
// ---------------------------------------------------------------------------------------------
export function StatusView() {
  const t = usePluginI18n(ID)
  const locale = useLocale()
  const query = useRest('/v1/status', { refetchInterval: pollWhile(() => true, 15_000) })
  const s = query.data
  if (query.isLoading) return h('div', { className: 'ms-page-pad' }, h(DetailSkeleton, null))
  if (query.isError) return h('div', { className: 'ms-page-pad' }, h(Failure, { error: query.error, onRetry: () => query.refetch() }))
  if (!s) return null
  const worker = s.worker || {}
  const openMeeting = id => h(Button, { type: 'button', variant: 'ghost', size: 'xs', onClick: () => { $selected.set(id); $tab.set('processing'); $view.set('library') } }, t('status.openMeeting'), h(Codicon, { name: 'arrow-right', size: '0.7rem' }))
  const workerTone = worker.state === 'recent' ? 'good' : worker.state === 'stale' ? 'bad' : 'warn'
  return h('div', { className: 'ms-page-pad ms-cards' },
    h('section', { className: 'ms-card' },
      h(SectionLabel, { actions: h(Button, { type: 'button', variant: 'ghost', size: 'xs', onClick: () => query.refetch() }, h(Codicon, { name: 'refresh', size: '0.75rem' }), t('common.refresh')) }, t('status.bot')),
      h('p', { className: 'ms-status-line' }, h(Dot, { tone: workerTone }), tOr(t, `status.worker.${worker.state}`, t('status.worker.unknown'))),
      worker.last_seen ? h('p', { className: 'ms-hint' }, t('status.lastSeen', fmtEpoch(worker.last_seen, locale))) : null,
      h('div', { className: 'ms-counters' }, ['running', 'queued', 'failed'].map(k => h('div', { key: k, className: `ms-counter ms-counter-${k}${s.counts?.[k] ? '' : ' is-zero'}` },
        h('span', { className: 'ms-counter-n' }, String(s.counts?.[k] ?? 0)), h('span', { className: 'ms-counter-l' }, t(`status.${k}`)))))),
    h('section', { className: 'ms-card' },
      h(SectionLabel, null, t('status.queue')),
      s.jobs?.length
        ? h('ul', { className: 'ms-list' }, s.jobs.map(j => h('li', { key: j.meeting_id, className: 'ms-list-row' },
          h(Dot, { tone: j.state === 'failed' ? 'bad' : j.state === 'running' ? 'busy' : 'muted' }),
          h('span', { className: 'ms-grow' }, h('span', { className: 'ms-list-title' }, j.title || j.meeting_id),
            h('span', { className: 'ms-list-sub' }, j.state === 'failed' ? t(`processing.problem.${j.problem || 'unknown'}`) : tOr(t, `stage.${j.stage}`, j.stage || ''))),
          openMeeting(j.meeting_id))))
        : h('p', { className: 'ms-muted' }, t('status.noJobs')),
      s.waiting_destination?.length
        ? h(Fragment, null, h(SectionLabel, null, t('status.waiting')),
          h('ul', { className: 'ms-list' }, s.waiting_destination.map(w => h('li', { key: w.meeting_id, className: 'ms-list-row' },
            h(Dot, { tone: 'warn' }), h('span', { className: 'ms-grow ms-list-title' }, w.title), openMeeting(w.meeting_id)))))
        : null,
      s.dm_notes?.length
        ? h(Fragment, null, h(SectionLabel, null, t('status.dmNotes')),
          h('ul', { className: 'ms-list' }, s.dm_notes.map(w => h('li', { key: w.meeting_id, className: 'ms-list-row' },
            h(Dot, { tone: 'muted' }), h('span', { className: 'ms-grow ms-list-title' }, w.title), openMeeting(w.meeting_id)))))
        : null),
    s.settings_warnings?.length
      ? h(Callout, { tone: 'warn', title: t('status.warnings') }, h('p', null, t('status.warningsHelp')),
        h('ul', { className: 'ms-bullets ms-mono' }, s.settings_warnings.map((w, i) => h('li', { key: i }, w))))
      : null,
    h(GoogleCard, { google: s.google || {}, locale }),
    h('section', { className: 'ms-card' },
      h(SectionLabel, null, t('status.commands')),
      s.commands?.length
        ? h('ul', { className: 'ms-list' }, s.commands.map(c => h('li', { key: c.id, className: 'ms-list-row' },
          h(Dot, { tone: c.state === 'done' ? 'good' : c.state === 'failed' || c.state === 'unknown' ? 'bad' : 'busy' }),
          h('span', { className: 'ms-grow' },
            h('span', { className: 'ms-list-title' }, commandLabel(c, t)),
            h('span', { className: 'ms-list-sub' }, [c.title, fmtEpoch(c.created_at, locale), tOr(t, `status.cmd.${c.state}`, t('status.cmd.unknown'))].filter(Boolean).join(' · '))),
          openMeeting(c.meeting_id))))
        : h('p', { className: 'ms-muted' }, t('status.noCommands'))),
    h(DoctorCard, null))
}

function GoogleCard({ google: g, locale }) {
  const t = usePluginI18n(ID)
  const line = g.connected ? (g.enabled ? t('status.googleConnected') : t('status.googleConnectedOff')) : g.revoked ? t('status.googleRevoked') : t('status.googleNot')
  const commands = g.commands || {}
  const steps = (g.connected ? (g.enabled ? [] : [commands.enable]) : [commands.connect, g.enabled ? '' : commands.enable, commands.status]).filter(Boolean)
  return h('section', { className: 'ms-card' },
    h(SectionLabel, null, t('status.google')),
    h('p', { className: 'ms-status-line' }, h(Dot, { tone: g.connected && g.enabled ? 'good' : g.connected ? 'warn' : 'muted' }), line),
    g.last_poll_at ? h('p', { className: 'ms-hint' }, t('status.googleLastPoll', fmtDate(g.last_poll_at, locale))) : null,
    g.last_import_at ? h('p', { className: 'ms-hint' }, t('status.googleLastImport', fmtDate(g.last_import_at, locale))) : null,
    g.last_error ? h('p', { className: 'ms-error' }, t('status.googleError', g.last_error)) : null,
    steps.length ? h(Fragment, null, h('p', { className: 'ms-hint' }, t('status.googleGuide')), h('pre', { className: 'ms-code' }, steps.join('\n'))) : null)
}

function DoctorCard() {
  const t = usePluginI18n(ID)
  const [asked, setAsked] = useState(false)
  const query = useRest(asked ? '/v1/doctor' : '', { staleTime: 60_000 })
  const r = query.data
  const failed = r ? r.checks.filter(c => c.status !== 'ok').length : 0
  const action = h(Button, { type: 'button', variant: 'secondary', size: 'xs', disabled: query.isFetching, onClick: () => (asked ? query.refetch() : setAsked(true)) },
    h(Codicon, { name: query.isFetching ? 'loading~spin' : 'pulse', size: '0.75rem' }), asked ? t('status.doctorAgain') : t('status.doctorRun'))
  let body = h('p', { className: 'ms-muted' }, t('status.doctorHelp'))
  if (asked && query.isLoading) body = h(ListSkeleton, { rows: 4 })
  else if (query.isError) body = h(Failure, { error: query.error, onRetry: () => query.refetch() })
  else if (r) {
    body = h(Fragment, null,
      h('p', { className: 'ms-status-line', role: 'status' }, h(Dot, { tone: failed ? 'warn' : 'good' }), failed ? t('status.doctorIssues', failed) : t('status.doctorOk')),
      h('ul', { className: 'ms-list' }, r.checks.map(c => h('li', { key: c.name, className: 'ms-list-row' },
        h(Pill, { tone: c.status === 'ok' ? 'good' : c.status === 'warn' ? 'warn' : 'bad' }, tOr(t, `status.check.${c.status}`, c.status)),
        h('span', { className: 'ms-grow' }, h('span', { className: 'ms-list-title' }, c.name), c.detail ? h('span', { className: 'ms-list-sub' }, c.detail) : null)))))
  }
  return h('section', { className: 'ms-card' }, h(SectionLabel, { actions: action }, t('status.doctor')), body)
}

// ---------------------------------------------------------------------------------------------
// settings
// ---------------------------------------------------------------------------------------------
/** Client-side check mirroring the schema bounds; the server stays the authority. */
export function validateDraft(field, draft, t) {
  if (field.type === 'int' || field.type === 'float') {
    const text = String(draft).trim()
    if (text === '' || !Number.isFinite(Number(text))) return { error: t('settings.invalidNumber') }
    const n = Number(text)
    if (field.type === 'int' && !Number.isInteger(n)) return { error: t('settings.invalidInteger') }
    if (typeof field.minimum === 'number' && n < field.minimum) return { error: t('settings.min', field.minimum) }
    if (typeof field.maximum === 'number' && n > field.maximum) return { error: t('settings.max', field.maximum) }
    return { value: n }
  }
  if (field.type === 'list') return { value: String(draft).split('\n').map(s => s.trim()).filter(Boolean) }
  if (field.type === 'bool') return { value: Boolean(draft) }
  return { value: String(draft) }
}

function toDraft(field, value) {
  if (field.type === 'list') return (Array.isArray(value) ? value : []).join('\n')
  if (field.type === 'bool') return Boolean(value)
  return value === null || value === undefined ? '' : String(value)
}

function showDefault(field, t) {
  const v = field.default
  if (field.type === 'bool') return v ? t('settings.yes') : t('settings.no')
  if (Array.isArray(v)) return v.length ? v.join(', ') : t('settings.empty')
  if (field.choices?.length) return tOr(t, `choice.${field.key}.${v}`, String(v))
  return v === '' || v === null || v === undefined ? t('settings.empty') : String(v)
}

async function saveSetting(key, value) {
  return rest(`/v1/settings/${encodeURIComponent(key)}`, { method: 'PUT', body: { value } })
}

export function SettingsView() {
  const t = usePluginI18n(ID)
  const locale = useLocale()
  const lang = String(locale).toLowerCase().startsWith('es') ? 'es' : 'en'
  const query = useRest(`/v1/settings${qs({ lang })}`, { staleTime: 30_000 })
  const [group, setGroup] = useState(null)
  const d = query.data
  if (query.isLoading) return h('div', { className: 'ms-page-pad' }, h(DetailSkeleton, null))
  if (query.isError) return h('div', { className: 'ms-page-pad' }, h(Failure, { error: query.error, onRetry: () => query.refetch() }))
  if (!d) return null
  const groups = d.schema?.groups || []
  const fields = d.schema?.fields || []
  const current = group && groups.some(g => g.key === group) ? group : groups[0]?.key
  const refetch = () => query.refetch()
  let panel
  if (current === 'llm') panel = h(ModelsSection, { llm: d.llm || {}, onSaved: refetch })
  else {
    const own = fields.filter(f => f.group === current && f.storage !== 'hermes')
    panel = h(Fragment, null,
      current === 'projects' ? h(ProjectChannelsEditor, { value: d.values?.project_channels?.value || [], onSaved: refetch }) : null,
      h('div', { className: 'ms-fields' }, own.filter(f => f.format !== 'project_channel').map(f =>
        h(SettingField, { key: f.key, field: f, current: d.values?.[f.key] || {}, onSaved: refetch }))))
  }
  return h('div', { className: 'ms-settings' },
    h('nav', { className: 'ms-settings-nav', 'aria-label': t('settings.sections') },
      groups.map(g => h('button', {
        key: g.key, type: 'button', className: `ms-nav-item${g.key === current ? ' is-active' : ''}`, 'aria-current': g.key === current ? 'page' : undefined,
        onClick: () => setGroup(g.key)
      }, g.label))),
    h('div', { className: 'ms-settings-main' },
      h('div', { className: 'ms-settings-inner' },
        h('h2', { className: 'ms-settings-title' }, groups.find(g => g.key === current)?.label || ''),
        h('p', { className: 'ms-hint' }, t('settings.intro')),
        panel)))
}

export function SettingField({ field, current, onSaved }) {
  const t = usePluginI18n(ID)
  const id = `ms-set-${field.key}`
  const initial = toDraft(field, current.value)
  const [draft, setDraft] = useState(initial)
  const [error, setError] = useState('')
  const [state, setState] = useState('idle')
  const [note, setNote] = useState('')
  useEffect(() => { setDraft(toDraft(field, current.value)) }, [JSON.stringify(current.value)])
  const dirty = JSON.stringify(draft) !== JSON.stringify(initial)

  const save = async value => {
    const checked = validateDraft(field, value, t)
    if (checked.error) { setError(checked.error); return }
    setError('')
    setState('saving')
    try {
      const out = await saveSetting(field.key, checked.value)
      setState('saved')
      setNote(out?.requeued ? t('settings.requeued', out.requeued) : '')
      onSaved?.()
    } catch (e) {
      setState('idle')
      setError(parseError(e).message.replace(new RegExp(`^${field.key}:\\s*`), ''))
    }
  }

  const help = [field.help, field.type === 'list' ? t('settings.listHelp') : ''].filter(Boolean).join(' ')
  const aria = { 'aria-describedby': `${id}-help`, 'aria-invalid': error ? true : undefined }
  let control
  if (field.type === 'bool') {
    control = h(Switch, { id, checked: Boolean(draft), disabled: state === 'saving', onCheckedChange: v => { setDraft(v); save(v) }, ...aria })
  } else if (field.choices?.length) {
    control = h(Select, { value: String(draft), disabled: state === 'saving', onValueChange: v => { setDraft(v); save(v) } },
      h(SelectTrigger, { id, size: 'sm', className: 'ms-select', ...aria }, h(SelectValue, null)),
      h(SelectContent, null, field.choices.map(c => h(SelectItem, { key: String(c), value: String(c) }, tOr(t, `choice.${field.key}.${c}`, String(c))))))
  } else if (field.type === 'list') {
    control = h(Textarea, { id, className: 'ms-textarea', rows: Math.min(6, Math.max(2, String(draft).split('\n').length + 1)), value: draft, onChange: e => { setDraft(e.target.value); setState('idle') }, ...aria })
  } else {
    const numeric = field.type === 'int' || field.type === 'float'
    control = h(Input, {
      id, size: 'sm', type: numeric ? 'number' : 'text', value: draft, step: field.type === 'float' ? 'any' : numeric ? 1 : undefined,
      min: field.minimum, max: field.maximum, onChange: e => { setDraft(e.target.value); setState('idle') },
      onKeyDown: e => { if (e.key === 'Enter' && dirty) { e.preventDefault(); save(draft) } }, ...aria
    })
  }
  const inline = field.type === 'bool'
  const needsButton = field.type !== 'bool' && !field.choices?.length
  const origin = current.origin || 'default'
  return h('div', { className: `ms-field${inline ? ' is-inline' : ''}` },
    h('div', { className: 'ms-field-text' },
      h('div', { className: 'ms-field-label-row' },
        h('label', { className: 'ms-field-label', htmlFor: id }, field.label),
        origin !== 'default' ? h(Pill, { tone: origin === 'invalid' ? 'bad' : 'accent' }, tOr(t, `settings.origin.${origin}`, origin)) : null),
      h('p', { className: 'ms-field-help', id: `${id}-help` }, help, help ? ' ' : '', h('span', { className: 'ms-default' }, t('settings.defaultIs', showDefault(field, t))))),
    h('div', { className: 'ms-field-control' },
      control,
      needsButton && dirty ? h(Button, { type: 'button', size: 'sm', disabled: state === 'saving', onClick: () => save(draft) }, state === 'saving' ? t('common.saving') : t('common.save')) : null,
      state === 'saved' && !dirty && needsButton ? h('span', { className: 'ms-saved', role: 'status' }, h(Codicon, { name: 'check', size: '0.75rem' }), t('common.saved')) : null),
    error ? h('p', { className: 'ms-error', role: 'alert' }, error) : null,
    note ? h('p', { className: 'ms-hint', role: 'status' }, note) : null)
}

export function ProjectChannelsEditor({ value, onSaved }) {
  const t = usePluginI18n(ID)
  const fromServer = () => parseProjectChannels(value).map((r, i) => ({ ...r, _k: `s${i}` }))
  const [rows, setRows] = useState(fromServer)
  const [errors, setErrors] = useState({})
  const [error, setError] = useState('')
  const [state, setState] = useState('idle')
  const counter = useRef(0)
  const serial = JSON.stringify(value)
  useEffect(() => setRows(fromServer()), [serial])
  const dirty = JSON.stringify(rows.map(({ project, channel }) => ({ project, channel }))) !== JSON.stringify(parseProjectChannels(value))
  const setRow = (i, patch) => { setRows(rs => rs.map((r, j) => (j === i ? { ...r, ...patch } : r))); setState('idle') }
  const add = () => { counter.current += 1; setRows(rs => [...rs, { project: '', channel: '', _k: `n${counter.current}` }]) }
  const remove = i => { setRows(rs => rs.filter((_, j) => j !== i)); setState('idle') }
  const save = async () => {
    const checked = checkProjectChannels(rows, t)
    setErrors(checked.errors || {})
    if (checked.errors) return
    setError('')
    setState('saving')
    try {
      await saveSetting('project_channels', checked.value)
      setState('saved')
      onSaved?.()
    } catch (e) {
      setState('idle')
      setError(parseError(e).message.replace(/^project_channels:\s*/, ''))
    }
  }
  return h('section', { className: 'ms-card ms-editor', 'aria-labelledby': 'ms-pc-title' },
    h('h3', { className: 'ms-card-title', id: 'ms-pc-title' }, t('projects.title')),
    h('p', { className: 'ms-hint' }, t('projects.intro')),
    rows.length
      ? h('div', { className: 'ms-pc-table', role: 'table', 'aria-label': t('projects.title') },
        h('div', { className: 'ms-pc-row ms-pc-headrow', role: 'row' },
          h('span', { role: 'columnheader' }, t('projects.project')), h('span', { role: 'columnheader' }, t('projects.channel')), h('span', null)),
        rows.map((r, i) => h(Fragment, { key: r._k },
          h('div', { className: 'ms-pc-row', role: 'row' },
            h(Input, { size: 'sm', value: r.project, placeholder: 'Proyecto Alfa', 'aria-label': `${t('projects.row', i + 1)} — ${t('projects.project')}`, 'aria-invalid': errors[i] ? true : undefined, onChange: e => setRow(i, { project: e.target.value }) }),
            h('div', { className: 'ms-pc-channel' },
              h('span', { className: 'ms-hash', 'aria-hidden': 'true' }, '#'),
              h(Input, { size: 'sm', value: r.channel, inputMode: 'numeric', placeholder: '123456789012345678', className: 'ms-mono', 'aria-label': `${t('projects.row', i + 1)} — ${t('projects.channel')}`, 'aria-invalid': errors[i] ? true : undefined, onChange: e => setRow(i, { channel: e.target.value }) })),
            h(Button, { type: 'button', variant: 'ghost', size: 'icon-xs', 'aria-label': `${t('projects.remove')} — ${t('projects.row', i + 1)}`, onClick: () => remove(i) }, h(Codicon, { name: 'trash', size: '0.8rem' }))),
          errors[i] ? h('p', { className: 'ms-error ms-pc-error', role: 'alert' }, errors[i]) : null)))
      : h('p', { className: 'ms-muted' }, t('projects.none')),
    h('div', { className: 'ms-inline' },
      h(Button, { type: 'button', variant: 'ghost', size: 'sm', onClick: add }, h(Codicon, { name: 'add', size: '0.8rem' }), t('projects.add')),
      h('span', { className: 'ms-grow' }),
      state === 'saved' && !dirty ? h('span', { className: 'ms-saved', role: 'status' }, h(Codicon, { name: 'check', size: '0.75rem' }), t('projects.saved')) : null,
      h(Button, { type: 'button', size: 'sm', disabled: !dirty || state === 'saving', onClick: save }, state === 'saving' ? t('common.saving') : t('projects.save'))),
    error ? h('p', { className: 'ms-error', role: 'alert' }, error) : null)
}

export function ModelsSection({ llm, onSaved }) {
  const t = usePluginI18n(ID)
  const fromServer = () => ({
    provider: llm.provider || 'auto', model: llm.model || '', base_url: llm.base_url || '',
    timeout: llm.timeout === undefined || llm.timeout === null ? '' : String(llm.timeout),
    chain: (llm.fallback_chain || []).map((f, i) => ({ ...f, base_url: f.base_url || '', _k: `s${i}` }))
  })
  const [form, setForm] = useState(fromServer)
  const [error, setError] = useState('')
  const [state, setState] = useState('idle')
  const serial = JSON.stringify(llm)
  useEffect(() => setForm(fromServer()), [serial])
  const counter = useRef(0)
  const set = patch => { setForm(f => ({ ...f, ...patch })); setState('idle') }
  const setRow = (i, patch) => set({ chain: form.chain.map((r, j) => (j === i ? { ...r, ...patch } : r)) })
  const move = (i, delta) => {
    const chain = [...form.chain]
    const j = i + delta
    if (j < 0 || j >= chain.length) return
    ;[chain[i], chain[j]] = [chain[j], chain[i]]
    set({ chain })
  }
  const remove = i => set({ chain: form.chain.filter((_, j) => j !== i) })
  const add = () => { counter.current += 1; set({ chain: [...form.chain, { provider: '', model: '', base_url: '', _k: `n${counter.current}` }] }) }
  const save = async () => {
    if (form.chain.some(r => !String(r.provider).trim())) { setError(t('llm.needProvider')); return }
    const timeout = String(form.timeout).trim()
    if (timeout && !(Number(timeout) > 0)) { setError(t('settings.min', 1)); return }
    setError('')
    setState('saving')
    const body = {
      provider: form.provider.trim() || 'auto', model: form.model.trim(), base_url: form.base_url.trim(),
      fallback_chain: form.chain.map(r => ({ provider: r.provider.trim(), model: String(r.model || '').trim(), base_url: String(r.base_url || '').trim() }))
    }
    if (timeout) body.timeout = Number(timeout)
    try {
      await rest('/v1/llm', { method: 'PUT', body })
      setState('saved')
      onSaved?.()
    } catch (e) {
      setState('idle')
      setError(parseError(e).message)
    }
  }
  const field = (fid, label, value, onChange, extra = {}) => h('div', { className: 'ms-mfield' },
    h('label', { className: 'ms-field-label', htmlFor: fid }, label),
    h(Input, { id: fid, size: 'sm', value, onChange: e => onChange(e.target.value), type: extra.type || 'text', min: extra.min, placeholder: extra.placeholder, 'aria-describedby': extra.help ? `${fid}-help` : undefined }),
    extra.help ? h('p', { className: 'ms-field-help', id: `${fid}-help` }, extra.help) : null)
  const sources = llm.sources || {}
  const effective = llm.effective ? [llm.effective.provider, llm.effective.model].filter(Boolean).join(' / ') : ''
  return h('div', { className: 'ms-stack' },
    h('p', { className: 'ms-muted' }, t('llm.intro')),
    h('section', { className: 'ms-card', 'aria-labelledby': 'ms-llm-primary' },
      h('div', { className: 'ms-field-label-row' },
        h('h3', { className: 'ms-card-title', id: 'ms-llm-primary' }, t('llm.primary')),
        sources.provider ? h(Pill, { tone: sources.provider === 'hermes-config' ? 'accent' : 'muted' }, tOr(t, `llm.source.${sources.provider}`, sources.provider)) : null),
      effective ? h('p', { className: 'ms-hint' }, t('llm.effective', effective)) : null,
      h('div', { className: 'ms-mgrid' },
        field('ms-llm-provider', t('llm.provider'), form.provider, v => set({ provider: v }), { help: t('llm.providerHelp') }),
        field('ms-llm-model', t('llm.model'), form.model, v => set({ model: v })),
        field('ms-llm-base', t('llm.baseUrl'), form.base_url, v => set({ base_url: v }), { placeholder: 'https://…' }),
        field('ms-llm-timeout', t('llm.timeout'), form.timeout, v => set({ timeout: v }), { type: 'number', min: 1 }))),
    h('section', { className: 'ms-card', 'aria-labelledby': 'ms-llm-chain' },
      h('h3', { className: 'ms-card-title', id: 'ms-llm-chain' }, t('llm.fallbacks')),
      form.chain.length === 0 ? h('p', { className: 'ms-muted' }, t('llm.noFallbacks')) : null,
      h('ol', { className: 'ms-chain' }, form.chain.map((r, i) => h('li', { key: r._k, className: 'ms-chain-row', 'aria-label': t('llm.row', i + 1) },
        h('span', { className: 'ms-chain-n', 'aria-hidden': 'true' }, String(i + 1)),
        h(Input, { size: 'sm', 'aria-label': `${t('llm.row', i + 1)} — ${t('llm.provider')}`, placeholder: t('llm.provider'), value: r.provider, onChange: e => setRow(i, { provider: e.target.value }) }),
        h(Input, { size: 'sm', 'aria-label': `${t('llm.row', i + 1)} — ${t('llm.model')}`, placeholder: t('llm.model'), value: r.model, onChange: e => setRow(i, { model: e.target.value }) }),
        h(Input, { size: 'sm', 'aria-label': `${t('llm.row', i + 1)} — ${t('llm.baseUrl')}`, placeholder: t('llm.baseUrl'), value: r.base_url, onChange: e => setRow(i, { base_url: e.target.value }) }),
        h('span', { className: 'ms-chain-actions' },
          h(Button, { type: 'button', variant: 'ghost', size: 'icon-xs', disabled: i === 0, onClick: () => move(i, -1), 'aria-label': `${t('llm.up')} — ${t('llm.row', i + 1)}` }, h(Codicon, { name: 'arrow-up', size: '0.75rem' })),
          h(Button, { type: 'button', variant: 'ghost', size: 'icon-xs', disabled: i === form.chain.length - 1, onClick: () => move(i, 1), 'aria-label': `${t('llm.down')} — ${t('llm.row', i + 1)}` }, h(Codicon, { name: 'arrow-down', size: '0.75rem' })),
          h(Button, { type: 'button', variant: 'ghost', size: 'icon-xs', onClick: () => remove(i), 'aria-label': `${t('llm.remove')} — ${t('llm.row', i + 1)}` }, h(Codicon, { name: 'trash', size: '0.75rem' })))))),
      h('div', null, h(Button, { type: 'button', variant: 'ghost', size: 'sm', onClick: add, disabled: form.chain.length >= 10 }, h(Codicon, { name: 'add', size: '0.8rem' }), t('llm.add')))),
    llm.problems?.length ? h(Callout, { tone: 'warn', title: t('llm.problems') }, h('ul', { className: 'ms-bullets' }, llm.problems.map((p, i) => h('li', { key: i }, p)))) : null,
    error ? h('p', { className: 'ms-error', role: 'alert' }, error) : null,
    h('div', { className: 'ms-inline' },
      h(Button, { type: 'button', size: 'sm', onClick: save, disabled: state === 'saving' }, state === 'saving' ? t('common.saving') : t('llm.save')),
      state === 'saved' ? h('span', { className: 'ms-saved', role: 'status' }, h(Codicon, { name: 'check', size: '0.75rem' }), t('llm.saved')) : null))
}

// ---------------------------------------------------------------------------------------------
// page
// ---------------------------------------------------------------------------------------------
const VIEWS = ['library', 'status', 'settings']

export function MeetingsPage() {
  const t = usePluginI18n(ID)
  const view = useValue($view)
  const selected = useValue($selected)
  const scope = useScope()
  const firstScope = useRef(scope)
  // A connection switch (another Hermes) drops the open meeting: it belongs to that other install.
  useEffect(() => {
    if (firstScope.current !== scope) {
      firstScope.current = scope
      $selected.set(null)
    }
  }, [scope])
  // Probe once: a backend launched without the plugin gets the guided empty state for the whole page.
  const probe = useRest('/v1/status', { staleTime: 30_000 })
  const missing = probe.isError && isPluginMissing(probe.error)
  const select = id => { $selected.set(id); if (id !== selected) $tab.set('summary') }

  let body
  if (missing) body = h('div', { className: 'ms-page-pad' }, h(PluginMissing, null))
  else if (view === 'status') body = h('div', { className: 'ms-scroll' }, h(StatusView, null))
  else if (view === 'settings') body = h(SettingsView, null)
  else {
    body = h('div', { className: `ms-md${selected ? ' has-selection' : ''}` },
      h(LibraryList, { selected, onSelect: select }),
      h('section', { className: 'ms-pane', 'aria-label': selected ? undefined : t('library.pickTitle') },
        selected
          ? h(MeetingDetail, { key: selected, id: selected, onBack: () => $selected.set(null) })
          : h(Empty, { icon: 'comment-discussion', title: t('library.pickTitle') }, h('p', null, t('library.pickBody')))))
  }

  return h('div', { className: 'ms-page' },
    h('header', { className: 'ms-page-head' },
      h('h1', { className: 'ms-h1' }, t('title')),
      missing ? null : h(SegmentedControl, {
        value: view,
        options: VIEWS.map(k => ({ id: k, label: t(`views.${k}`) })),
        onChange: k => $view.set(k)
      })),
    h('main', { className: 'ms-main', 'aria-label': t(`views.${view}`) }, body))
}

// ---------------------------------------------------------------------------------------------
// styles — host tokens only (--ui-*, --dt-*), so the page follows the active theme (light/dark)
// ---------------------------------------------------------------------------------------------
export const CSS = `
.ms-page{display:flex;flex-direction:column;height:100%;min-height:0;color:var(--ui-text-primary);font-size:13px;line-height:1.5;background:var(--ui-surface-background)}
.ms-page-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 20px 10px;flex-shrink:0}
.ms-h1{font-size:15px;font-weight:600;margin:0;letter-spacing:-.01em}
.ms-main{flex:1;min-height:0;display:flex;flex-direction:column}
.ms-scroll{flex:1;min-height:0;overflow-y:auto}
.ms-page-pad{padding:4px 20px 32px}
.ms-grow{flex:1;min-width:0}
.ms-muted{color:var(--ui-text-tertiary);margin:0}
.ms-hint{color:var(--ui-text-tertiary);font-size:12px;margin:0}
.ms-mono{font-family:var(--dt-font-mono,ui-monospace,monospace)}
.ms-error{color:var(--ui-red,var(--dt-destructive));font-size:12px;margin:0}
.ms-stack{display:flex;flex-direction:column;gap:18px}
.ms-stack-sm{display:flex;flex-direction:column;gap:8px}
.ms-inline{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.ms-code{margin:0;padding:8px 10px;border-radius:6px;background:var(--ui-inline-code-background,var(--ui-bg-tertiary));font-family:var(--dt-font-mono,ui-monospace,monospace);font-size:11.5px;white-space:pre-wrap;word-break:break-word;user-select:text;color:var(--ui-text-secondary)}
/* tones */
.ms-dot{display:inline-block;flex-shrink:0;width:7px;height:7px;border-radius:999px;background:var(--ui-text-quaternary)}
.ms-dot.ms-tone-good{background:var(--ui-green)}
.ms-dot.ms-tone-bad{background:var(--ui-red)}
.ms-dot.ms-tone-warn{background:var(--ui-yellow)}
.ms-dot.ms-tone-busy{background:var(--ui-blue);animation:ms-pulse 1.6s ease-in-out infinite}
.ms-dot.ms-tone-live{background:var(--ui-red);box-shadow:0 0 0 3px color-mix(in srgb,var(--ui-red) 22%,transparent);animation:ms-pulse 1.2s ease-in-out infinite}
@keyframes ms-pulse{50%{opacity:.45}}
@media (prefers-reduced-motion:reduce){.ms-dot{animation:none!important}}
.ms-tone-text-good{color:var(--ui-green)}
.ms-tone-text-bad{color:var(--ui-red)}
.ms-tone-text-warn{color:var(--ui-yellow)}
.ms-tone-text-busy{color:var(--ui-blue)}
.ms-tone-text-muted{color:var(--ui-text-tertiary)}
.ms-pill{display:inline-flex;align-items:center;gap:5px;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:500;line-height:18px;white-space:nowrap;background:var(--ui-bg-quaternary);color:var(--ui-text-secondary)}
.ms-pill .ms-dot{width:6px;height:6px}
.ms-pill.ms-tone-good{background:color-mix(in srgb,var(--ui-green) 14%,transparent);color:var(--ui-green)}
.ms-pill.ms-tone-bad{background:color-mix(in srgb,var(--ui-red) 13%,transparent);color:var(--ui-red)}
.ms-pill.ms-tone-warn{background:color-mix(in srgb,var(--ui-yellow) 15%,transparent);color:color-mix(in srgb,var(--ui-yellow) 80%,var(--ui-text-primary))}
.ms-pill.ms-tone-busy{background:color-mix(in srgb,var(--ui-blue) 13%,transparent);color:var(--ui-blue)}
.ms-pill.ms-tone-accent{background:color-mix(in srgb,var(--ui-accent) 12%,transparent);color:var(--ui-accent)}
/* master–detail */
.ms-md{flex:1;min-height:0;display:grid;grid-template-columns:minmax(280px,340px) 1fr;border-top:1px solid var(--ui-stroke-tertiary)}
.ms-master{display:flex;flex-direction:column;min-height:0;border-right:1px solid var(--ui-stroke-tertiary);background:transparent}
.ms-master-head{display:flex;flex-direction:column;gap:8px;padding:12px 12px 6px}
.ms-search-row{display:flex;align-items:center;gap:6px}
.ms-filter-toggle{flex-shrink:0;gap:5px}
.ms-filters{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:10px;border-radius:8px;background:transparent;border:1px solid var(--ui-stroke-tertiary)}
.ms-filter{display:flex;flex-direction:column;gap:3px;min-width:0}
.ms-filter-label{font-size:10.5px;font-weight:600;letter-spacing:.02em;text-transform:uppercase;color:var(--ui-text-tertiary);margin:0}
.ms-filter-trigger{width:100%}
.ms-daterange{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr;gap:8px}
.ms-date{color-scheme:light dark}
.ms-clear{grid-column:1/-1;justify-self:end}
.ms-count{font-size:11px;color:var(--ui-text-tertiary);margin:0;padding:0 4px}
.ms-master-body{flex:1;min-height:0;overflow-y:auto;padding:2px 6px 12px}
.ms-rows{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:1px}
.ms-row{display:grid;grid-template-columns:16px 1fr auto;align-items:start;gap:8px;width:100%;padding:8px 8px;border:0;border-radius:6px;background:transparent;color:inherit;text-align:left;cursor:pointer;font:inherit}
.ms-row:hover{background:var(--ui-row-hover-background)}
.ms-row.is-active{background:var(--ui-row-active-background)}
.ms-row:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}
.ms-row-lead{display:flex;align-items:center;justify-content:center;height:19px}
.ms-row-main{display:flex;flex-direction:column;min-width:0}
.ms-row-title{font-weight:500;font-size:13px;color:var(--ui-text-primary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ms-row-sub{font-size:11.5px;color:var(--ui-text-tertiary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ms-row-meta{display:flex;align-items:center;gap:4px;font-size:11px;color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums;padding-top:1px}
.ms-more{display:flex;justify-content:center;padding:8px 0}
.ms-pane{min-width:0;min-height:0;display:flex;flex-direction:column;overflow:hidden}
/* detail */
.ms-detail{display:flex;flex-direction:column;min-height:0;height:100%}
.ms-detail-head{padding:18px 28px 0;display:flex;flex-direction:column;gap:4px}
.ms-back{align-self:flex-start;margin-left:-8px;display:none}
.ms-title-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.ms-detail-title{font-size:19px;font-weight:600;margin:0;letter-spacing:-.015em;line-height:1.3}
.ms-detail-meta{margin:0;color:var(--ui-text-secondary);font-size:12.5px}
.ms-chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.ms-tabs{padding:14px 28px 0;gap:0}
.ms-tabs-list{align-self:flex-start;height:auto;padding:0;gap:18px;border-radius:0;background:transparent;border-bottom:1px solid var(--ui-stroke-tertiary);width:100%;justify-content:flex-start}
.ms-tab{height:auto;padding:6px 0 9px;margin-bottom:-1px;border-radius:0;border-bottom:2px solid transparent;background:transparent!important;box-shadow:none!important;color:var(--ui-text-tertiary);font-size:13px;font-weight:500;gap:6px}
.ms-tab:hover{color:var(--ui-text-primary)}
.ms-tab[data-state=active]{color:var(--ui-text-primary);border-bottom-color:var(--ui-accent)}
.ms-tab:focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px;border-radius:3px}
.ms-tab-count{font-size:10.5px;padding:0 6px;border-radius:999px;background:var(--ui-bg-quaternary);color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums}
.ms-detail-body{flex:1;min-height:0;overflow-y:auto;padding:20px 28px 40px}
.ms-detail-body>*{max-width:860px}
.ms-detail-pad{padding:20px 28px;display:flex;flex-direction:column;gap:10px}
.ms-lead{font-size:14.5px;line-height:1.55;margin:0;color:var(--ui-text-primary);font-weight:450}
.ms-prose{white-space:pre-wrap;margin:0;color:var(--ui-text-secondary)}
.ms-block{display:flex;flex-direction:column;gap:8px}
.ms-section-label-row{display:flex;align-items:center;justify-content:space-between;gap:8px}
.ms-section-label{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--ui-text-tertiary);margin:0}
.ms-grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px 28px}
.ms-bullets{margin:0;padding-left:0;list-style:none;display:flex;flex-direction:column;gap:6px}
.ms-bullets li{position:relative;padding-left:16px;color:var(--ui-text-secondary)}
.ms-bullets li::before{content:'';position:absolute;left:3px;top:.62em;width:5px;height:5px;border-radius:999px;background:var(--ui-text-quaternary)}
.ms-bullets-q li::before{background:var(--ui-yellow)}
.ms-topics{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.ms-topic{padding:10px 12px;border-radius:8px;border:1px solid var(--ui-stroke-tertiary)}
.ms-topic-title{margin:0 0 6px;font-weight:600;font-size:12.5px}
.ms-mini-tasks{list-style:none;margin:0;padding:0;border:1px solid var(--ui-stroke-tertiary);border-radius:8px;overflow:hidden}
.ms-mini-task{display:flex;justify-content:space-between;gap:12px;padding:8px 12px;border-top:1px solid var(--ui-stroke-tertiary)}
.ms-mini-task:first-child{border-top:0}
.ms-mini-title{font-weight:500;min-width:0}
.ms-mini-meta{font-size:12px;color:var(--ui-text-tertiary);white-space:nowrap;text-align:right}
.ms-audio-block{padding:12px 14px;border-radius:10px;background:transparent;border:1px solid var(--ui-stroke-tertiary)}
.ms-audio{width:100%;height:36px;color-scheme:light dark}
.ms-callout{display:flex;gap:10px;padding:10px 12px;border-radius:8px;border:1px solid var(--ui-stroke-tertiary);background:transparent}
.ms-callout p{margin:0}
.ms-callout-icon{margin-top:2px;flex-shrink:0;color:var(--ui-text-tertiary)}
.ms-callout-body{display:flex;flex-direction:column;gap:4px;min-width:0;color:var(--ui-text-secondary)}
.ms-callout-title{font-weight:600;color:var(--ui-text-primary)}
.ms-callout-warn{border-color:color-mix(in srgb,var(--ui-yellow) 35%,transparent);background:color-mix(in srgb,var(--ui-yellow) 8%,transparent)}
.ms-callout-warn .ms-callout-icon{color:var(--ui-yellow)}
.ms-callout-bad{border-color:color-mix(in srgb,var(--ui-red) 35%,transparent);background:color-mix(in srgb,var(--ui-red) 7%,transparent)}
.ms-callout-bad .ms-callout-icon{color:var(--ui-red)}
.ms-details summary{cursor:pointer;font-size:12px;color:var(--ui-text-tertiary);margin-top:4px}
.ms-details summary:focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
.ms-details .ms-code{margin-top:6px}
/* transcript */
.ms-transcript-bar{display:flex;align-items:center;gap:12px}
.ms-utts{list-style:none;margin:0;padding:0;display:flex;flex-direction:column}
.ms-utt{display:grid;grid-template-columns:28px 1fr;gap:10px;padding:8px 0 2px}
.ms-utt.is-cont{padding-top:0}
.ms-avatar{width:26px;height:26px;border-radius:999px;display:flex;align-items:center;justify-content:center;font-size:10.5px;font-weight:600;color:var(--ms-tone);background:color-mix(in srgb,var(--ms-tone) 16%,transparent)}
.ms-utt.is-cont .ms-avatar{background:transparent}
.ms-utt-main{min-width:0}
.ms-utt-head{display:flex;align-items:baseline;gap:8px}
.ms-speaker{font-weight:600;font-size:12.5px;color:var(--ms-tone)}
.ms-time{font-size:11px;color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums;font-family:var(--dt-font-mono,ui-monospace,monospace);background:none;border:0;padding:0}
.ms-time.is-link{cursor:pointer;border-radius:3px}
.ms-time.is-link:hover{color:var(--ui-accent);text-decoration:underline}
.ms-time.is-link:focus-visible{outline:2px solid var(--ui-accent);outline-offset:1px}
.ms-time-inline{margin-right:8px;opacity:0}
.ms-utt:hover .ms-time-inline,.ms-time-inline:focus-visible{opacity:1}
.ms-utt-text{margin:1px 0 0;white-space:pre-wrap;color:var(--ui-text-secondary);line-height:1.55}
.ms-mark{background:color-mix(in srgb,var(--ui-yellow) 38%,transparent);color:var(--ui-text-primary);border-radius:2px;padding:0 1px}
/* tasks */
.ms-tasks{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px}
.ms-task{padding:12px 14px;border:1px solid var(--ui-stroke-tertiary);border-radius:10px;display:flex;flex-direction:column;gap:8px;background:transparent}
.ms-task.is-dismissed{opacity:.6}
.ms-task-head{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}
.ms-task-title{margin:0;font-weight:600;font-size:13.5px}
.ms-task-desc{margin:0;color:var(--ui-text-secondary)}
.ms-task-meta{display:flex;flex-wrap:wrap;gap:4px 20px;margin:0}
.ms-task-meta div{display:flex;gap:6px;font-size:12px}
.ms-task-meta dt{color:var(--ui-text-tertiary)}
.ms-task-meta dd{margin:0;color:var(--ui-text-primary);font-weight:500}
.ms-dests{display:flex;flex-wrap:wrap;gap:6px 18px;padding-top:8px;border-top:1px solid var(--ui-stroke-tertiary)}
.ms-dest{display:flex;align-items:center;gap:6px;font-size:12px}
.ms-dest-name{color:var(--ui-text-tertiary);min-width:44px}
.ms-link,.ms-linkbtn{display:inline-flex;align-items:center;gap:3px;color:var(--ui-accent);font-size:12px;text-decoration:none}
.ms-link:hover{text-decoration:underline}
.ms-link:focus-visible{outline:2px solid var(--ui-accent);outline-offset:1px;border-radius:2px}
.ms-quote{margin:0;padding-left:10px;border-left:2px solid var(--ui-stroke-secondary);color:var(--ui-text-tertiary);font-size:12px;font-style:italic}
/* processing */
.ms-current{display:flex;align-items:center;gap:10px}
.ms-timeline{list-style:none;margin:0;padding:0;position:relative}
.ms-tl-item{display:grid;grid-template-columns:14px 1fr;gap:10px;padding:5px 0;position:relative}
.ms-tl-item .ms-dot{margin-top:6px;margin-left:3px;position:relative;z-index:1}
.ms-tl-item:not(:last-child)::after{content:'';position:absolute;left:6px;top:17px;bottom:-5px;width:1px;background:var(--ui-stroke-tertiary)}
.ms-tl-main{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
.ms-tl-label{display:inline-flex;align-items:center;gap:8px;color:var(--ui-text-primary)}
.ms-tl-time{font-size:11.5px;color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums}
.ms-cmd{display:flex;align-items:center;gap:8px;margin:0;font-size:12.5px}
.ms-choices{display:flex;flex-direction:column;gap:6px;margin-top:4px}
.ms-choice{display:flex;gap:10px;align-items:flex-start;text-align:left;padding:9px 11px;border-radius:8px;border:1px solid var(--ui-stroke-tertiary);background:transparent;color:inherit;cursor:pointer;font:inherit}
.ms-choice:hover{background:var(--ui-row-hover-background)}
.ms-choice.is-active{border-color:var(--ui-accent);background:color-mix(in srgb,var(--ui-accent) 8%,transparent)}
.ms-choice:focus-visible{outline:2px solid var(--ui-accent);outline-offset:1px}
.ms-radio-dot{width:14px;height:14px;border-radius:999px;border:1.5px solid var(--ui-stroke-primary,var(--ui-text-tertiary));margin-top:2px;flex-shrink:0}
.ms-choice.is-active .ms-radio-dot{border-color:var(--ui-accent);box-shadow:inset 0 0 0 3px var(--ui-bg-elevated,var(--dt-popover)),inset 0 0 0 8px var(--ui-accent)}
.ms-choice-title{display:block;font-weight:600;font-size:12.5px}
.ms-choice-help{display:block;font-size:12px;color:var(--ui-text-tertiary)}
/* empty + skeleton */
.ms-empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:6px;padding:40px 24px;min-height:220px}
.ms-empty-icon{width:40px;height:40px;border-radius:12px;display:flex;align-items:center;justify-content:center;color:var(--ui-text-tertiary);background:var(--ui-bg-quaternary);margin-bottom:4px}
.ms-empty-title{margin:0;font-weight:600;font-size:13.5px}
.ms-empty-body{max-width:420px;color:var(--ui-text-tertiary);font-size:12.5px;display:flex;flex-direction:column;gap:8px}
.ms-empty-body p{margin:0}
.ms-empty-body .ms-code{text-align:left}
.ms-empty-action{margin-top:8px}
.ms-skel-list{display:flex;flex-direction:column;gap:4px;padding:4px}
.ms-skel-row{display:grid;grid-template-columns:16px 1fr;gap:8px;padding:8px}
.ms-skel-dot{width:8px;height:8px;border-radius:999px;margin-top:5px}
.ms-skel-lines{display:flex;flex-direction:column;gap:6px}
.ms-skel-a{height:11px;width:70%}
.ms-skel-b{height:9px;width:45%}
.ms-skel-title{height:20px;width:45%}
.ms-skel-meta{height:12px;width:60%}
.ms-skel-tabs{height:26px;width:70%;margin:10px 0}
.ms-skel-para{height:12px;width:92%}
.ms-skel-audio{height:36px;width:100%}
/* status */
.ms-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px;align-items:start;max-width:1200px}
.ms-card{display:flex;flex-direction:column;gap:10px;padding:14px 16px;border-radius:10px;border:1px solid var(--ui-stroke-tertiary);background:transparent}
.ms-card-title{margin:0;font-size:13px;font-weight:600}
.ms-status-line{display:flex;align-items:center;gap:8px;margin:0}
.ms-counters{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.ms-counter{display:flex;flex-direction:column;padding:6px 0 0;border-top:1px solid var(--ui-stroke-tertiary)}
.ms-counter-n{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.2}
.ms-counter.is-zero .ms-counter-n{color:var(--ui-text-quaternary)}
.ms-counter-failed:not(.is-zero) .ms-counter-n{color:var(--ui-red)}
.ms-counter-running:not(.is-zero) .ms-counter-n{color:var(--ui-blue)}
.ms-counter-l{font-size:11px;color:var(--ui-text-tertiary)}
.ms-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column}
.ms-list-row{display:flex;align-items:center;gap:10px;padding:7px 0;border-top:1px solid var(--ui-stroke-tertiary)}
.ms-list-row:first-child{border-top:0}
.ms-list-title{display:block;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ms-list-sub{display:block;font-size:11.5px;color:var(--ui-text-tertiary)}
/* settings */
.ms-settings{flex:1;min-height:0;display:grid;grid-template-columns:200px 1fr;border-top:1px solid var(--ui-stroke-tertiary)}
.ms-settings-nav{display:flex;flex-direction:column;gap:1px;padding:12px 8px;border-right:1px solid var(--ui-stroke-tertiary);overflow-y:auto;background:transparent}
.ms-nav-item{text-align:left;padding:6px 10px;border-radius:6px;border:0;background:transparent;color:var(--ui-text-secondary);font:inherit;font-size:12.5px;cursor:pointer}
.ms-nav-item:hover{background:var(--ui-row-hover-background);color:var(--ui-text-primary)}
.ms-nav-item.is-active{background:var(--ui-row-active-background);color:var(--ui-text-primary);font-weight:500}
.ms-nav-item:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}
.ms-settings-main{min-height:0;overflow-y:auto}
.ms-settings-inner{max-width:760px;padding:20px 28px 48px;display:flex;flex-direction:column;gap:16px}
.ms-settings-title{margin:0;font-size:17px;font-weight:600;letter-spacing:-.01em}
.ms-fields{display:flex;flex-direction:column}
.ms-field{display:grid;grid-template-columns:minmax(0,1fr) minmax(200px,300px);gap:6px 24px;padding:14px 0;border-top:1px solid var(--ui-stroke-tertiary);align-items:start}
.ms-field:first-child{border-top:0}
.ms-field-text{display:flex;flex-direction:column;gap:3px;min-width:0}
.ms-field-label-row{display:flex;align-items:center;gap:8px}
.ms-field-label{font-weight:500;font-size:13px;color:var(--ui-text-primary)}
.ms-field-help{margin:0;font-size:12px;color:var(--ui-text-tertiary)}
.ms-default{color:var(--ui-text-quaternary)}
.ms-field-control{display:flex;align-items:center;gap:8px;justify-content:flex-end;flex-wrap:wrap}
.ms-field-control>*:first-child:not(button[role=switch]){flex:1;min-width:0}
.ms-field.is-inline .ms-field-control{justify-content:flex-end}
.ms-field>.ms-error,.ms-field>.ms-hint{grid-column:1/-1}
.ms-select{width:100%}
.ms-textarea{resize:vertical;font-family:var(--dt-font-mono,ui-monospace,monospace);font-size:12px}
.ms-saved{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:var(--ui-green)}
.ms-editor .ms-pc-table{display:flex;flex-direction:column;gap:6px}
.ms-pc-row{display:grid;grid-template-columns:1fr 1fr 28px;gap:8px;align-items:center}
.ms-pc-headrow span{font-size:10.5px;font-weight:600;letter-spacing:.02em;text-transform:uppercase;color:var(--ui-text-tertiary)}
.ms-pc-channel{position:relative;display:flex;align-items:center}
.ms-hash{position:absolute;left:8px;color:var(--ui-text-quaternary);font-size:12px;pointer-events:none;z-index:1}
.ms-pc-channel input{padding-left:20px}
.ms-pc-error{margin-top:-2px}
.ms-mgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px 16px}
.ms-mfield{display:flex;flex-direction:column;gap:4px}
.ms-chain{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}
.ms-chain-row{display:grid;grid-template-columns:18px 1fr 1fr 1.2fr auto;gap:6px;align-items:center}
.ms-chain-n{color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums;text-align:right;font-size:12px}
.ms-chain-actions{display:flex;gap:2px}
/* narrow window: stack master/detail, back button in the detail */
@media (max-width:760px){
.ms-md{grid-template-columns:1fr}
.ms-master{border-right:0}
.ms-md.has-selection .ms-master{display:none}
.ms-md:not(.has-selection) .ms-pane{display:none}
.ms-back{display:inline-flex}
.ms-detail-head,.ms-tabs,.ms-detail-body,.ms-detail-pad{padding-left:16px;padding-right:16px}
.ms-tabs-list{gap:14px;overflow-x:auto}
.ms-settings{grid-template-columns:1fr;grid-template-rows:auto 1fr}
.ms-settings-nav{flex-direction:row;overflow-x:auto;border-right:0;border-bottom:1px solid var(--ui-stroke-tertiary);padding:8px}
.ms-nav-item{white-space:nowrap}
.ms-settings-inner{padding:16px}
.ms-field{grid-template-columns:1fr}
.ms-field-control{justify-content:flex-start}
.ms-chain-row{grid-template-columns:18px 1fr 1fr}
.ms-chain-row>*:nth-child(4){grid-column:2/-1}
.ms-cards{grid-template-columns:1fr}
.ms-page-head{padding:12px 16px 8px}
}
`

// ---------------------------------------------------------------------------------------------
// registration
// ---------------------------------------------------------------------------------------------
const plugin = {
  id: ID,
  name: 'Meetings',
  description: 'Meeting library with notes, transcripts, tasks, processing status and settings for meeting-scribe.',
  defaultEnabled: false,
  register(ctx) {
    CTX = ctx
    ctx.i18n.register(LOCALES)
    if (typeof document !== 'undefined') {
      const style = document.createElement('style')
      style.dataset.plugin = ID
      style.textContent = CSS
      document.head.append(style)
      ctx.onDispose(() => style.remove())
    }
    ctx.onDispose(() => {
      if (CTX === ctx) CTX = null
    })
    ctx.register({ id: 'page', area: ROUTES_AREA, data: { path: ROUTE }, render: () => h(MeetingsPage, null) })
    // The SDK has no "hide when the backend is missing" hook for sidebar rows (contributions are
    // static per plugin); a profile without the backend gets the guided empty state instead.
    const labels = () => ctx.registerMany([
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 55, data: { codicon: 'mic', label: ctx.i18n.t('nav'), path: ROUTE } },
      {
        id: 'open', area: PALETTE_AREA,
        data: { id: 'meeting-scribe.open', label: ctx.i18n.t('open'), keywords: ['meetings', 'reuniones', 'transcript', 'notes', 'notas'], run: () => host.navigate(ROUTE) }
      }
    ])
    let dispose = labels()
    ctx.i18n.onLocaleChange(() => {
      dispose()
      dispose = labels()
    })
  }
}

export default plugin
