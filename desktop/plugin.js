/**
 * meeting-scribe — the «Meetings» page of Hermes Desktop.
 *
 * Plain ESM loaded uncompiled by the Desktop runtime loader: no build step, no JSX (UI is `jsx()`
 * calls through the `h()` helper below) and only the imports the loader maps
 * (`@hermes/plugin-sdk`, `react`, `react/jsx-runtime`).
 *
 * Every read and write goes through `ctx.rest` to this plugin's own backend
 * (`/api/plugins/meeting-scribe/v1/...`, see meeting_scribe/desktop/api.py). The page never runs a
 * pipeline: «Reprocess» queues a command that the gateway's worker executes, and the page polls it.
 *
 * One route (`/meeting-scribe`); the selected tab and meeting live in module atoms so leaving the
 * page and coming back keeps the operator where they were. Query keys carry the active connection +
 * profile so a switch never shows another profile's meetings.
 */

import {
  atom,
  Badge,
  Button,
  Codicon,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  host,
  PALETTE_AREA,
  ROUTES_AREA,
  SearchField,
  SIDEBAR_NAV_AREA,
  StatusDot,
  Switch,
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
const Q = 'meeting-scribe'
const PAGE_SIZE = 30
const TRANSCRIPT_PAGE = 200
const POLL_MS = 2000

/** `h(type, props, ...children)` → jsx/jsxs; `key` travels as jsx's third argument. */
export function h(type, props, ...children) {
  const { key, ...rest } = props || {}
  const kids = children.flat().filter(c => c !== null && c !== undefined && c !== false && c !== '')
  if (kids.length === 0) return jsx(type, rest, key)
  if (kids.length === 1) return jsx(type, { ...rest, children: kids[0] }, key)
  return jsxs(type, { ...rest, children: kids }, key)
}

let CTX = null
export const $tab = atom('library')
export const $selected = atom(null)

// ---------------------------------------------------------------------------------------------
// i18n (plugin-scoped; the bot's own Discord texts live in meeting_scribe/i18n and are not used here)
// ---------------------------------------------------------------------------------------------
export const LOCALES = {
  en: {
    nav: 'Meetings',
    open: 'Open meetings',
    title: 'Meetings',
    subtitle: 'Recordings, notes and tasks from your meetings.',
    tabs: { library: 'Library', status: 'Status', settings: 'Settings' },
    common: {
      retry: 'Try again', loading: 'Loading…', save: 'Save', saved: 'Saved', cancel: 'Cancel',
      back: 'Back to the library', none: '—', refresh: 'Refresh', open: 'Open', copy: 'Copy',
      notAvailable: 'Not available'
    },
    error: {
      title: 'Something went wrong',
      notFound: 'This meeting no longer exists.',
      disabled: 'The Meetings backend is not reachable. Check that the meeting-scribe plugin is enabled for this profile and that Hermes was restarted after installing it.',
      generic: message => `The server answered: ${message}`
    },
    library: {
      search: 'Search titles and what was said…',
      searchLabel: 'Search meetings',
      source: 'Source', state: 'Status', since: 'From', until: 'To',
      allSources: 'All sources', allStates: 'All statuses',
      clear: 'Clear filters',
      count: (shown, total) => `${shown} shown · ${total} in total`,
      prev: 'Newer', next: 'Older', page: n => `Page ${n}`,
      emptyTitle: 'No meetings yet',
      emptyBody: 'Meetings appear here after the bot records a Discord voice call or imports a Google Meet transcript.',
      noMatchTitle: 'Nothing matches',
      noMatchBody: 'Try other words or clear the filters.',
      untitled: 'Untitled meeting',
      speakers: n => (n === 1 ? '1 person' : `${n} people`),
      duration: m => `${m} min`
    },
    source: { discord: 'Discord', google_meet: 'Google Meet' },
    state: {
      recording: 'Recording', captured: 'Waiting to process', transcribing: 'Transcribing',
      transcribed: 'Transcribed', analyzing: 'Writing notes', analyzed: 'Notes ready',
      delivering: 'Publishing', done: 'Done', failed: 'Needs attention', processing: 'In progress',
      empty: 'No audio: discarded'
    },
    detail: {
      summary: 'Summary', tldr: 'In short', topics: 'Topics', decisions: 'Decisions',
      questions: 'Open questions', tasks: 'Tasks', transcript: 'Transcript', audio: 'Recording',
      noNotes: 'The notes are not ready yet. They appear here once the meeting has been processed.',
      empty: 'No audio was captured in this recording (nobody spoke), so there are no notes. Nothing was published and there is nothing to reprocess.',
      noDecisions: 'No decisions were recorded.', noQuestions: 'No open questions.',
      noTasks: 'No tasks came out of this meeting.',
      owner: 'Owner', due: 'Due', project: 'Project', noProject: 'No project',
      destinations: 'Where it went', discord: 'Discord', kanban: 'Kanban', linear: 'Linear',
      openLink: 'Open', partial: 'Partial recording: the bot joined late or the call was cut.',
      startedAt: 'Started', people: 'People', where: 'Channel',
      waiting: 'Waiting for a place to publish',
      waitingHelp: 'The notes are ready but no Discord channel could be used. Choose a notes channel in Settings → Where notes are posted and they will be sent automatically.',
      dmNotes: 'Notes were left in a direct message',
      dmNotesHelp: 'The bot could not post in the server, so it sent the notes by DM. They are moved to the channel once one is available.',
      jobFailed: stage => `Processing stopped while ${stage}.`,
      jobRetry: 'It will be retried automatically.',
      attempts: n => `Attempts: ${n}`,
      stage: { transcribe: 'transcribing', analyze: 'writing the notes', deliver: 'publishing', archive: 'archiving' }
    },
    sink: {
      pending: 'Pending', approved: 'Approved, sending…', delivered: 'Sent', dismissed: 'Dismissed',
      failed: 'Failed', skipped: 'Skipped'
    },
    transcript: {
      search: 'Find in the transcript…', searchLabel: 'Find in the transcript',
      lines: (shown, total) => `${shown} of ${total} lines loaded`,
      more: 'Load more', all: 'Load everything',
      matches: n => (n === 1 ? '1 matching line' : `${n} matching lines`),
      partialSearch: 'Searching only the lines loaded so far. Load everything to search the whole meeting.',
      empty: 'There is no transcript for this meeting yet.',
      noMatch: 'No line matches.'
    },
    audio: {
      label: 'Meeting recording',
      mixed: 'Play the whole meeting.',
      multitrack: 'This meeting was kept as one track per person, a format this page cannot play. The files stay on the machine where Hermes runs.',
      imported: 'Meetings imported from Google Meet come with their transcript only, without audio.',
      not_retained: 'The audio of this meeting was not kept (see «Audio kept» in Settings → Privacy).',
      failed: 'The recording could not be played here. The file is still on the machine where Hermes runs.'
    },
    reprocess: {
      button: 'Reprocess…',
      title: 'Reprocess this meeting?',
      description: 'Hermes will redo the step you pick and every step after it. What was already published is kept unless it has to be sent again.',
      from: 'Start again from',
      transcribe: 'Transcription', transcribeHelp: 'Listen to the recording again and rewrite the transcript, notes and tasks. The slowest option.',
      analyze: 'Notes', analyzeHelp: 'Keep the transcript and write the summary, decisions and tasks again.',
      deliver: 'Publishing', deliverHelp: 'Keep the notes and send them again to Discord and the other destinations.',
      confirm: 'Reprocess', busy: 'Sending…',
      queued: 'Queued: the bot will start in a few seconds.',
      running: 'Reprocessing…',
      done: 'Reprocessing finished.',
      failed: message => `Reprocessing failed: ${message}`,
      unknown: 'The bot stopped while reprocessing; the result is unknown. Check the meeting notes, then mark it as reviewed to reprocess again.',
      acknowledged: 'Reviewed. You can reprocess this meeting again.',
      acknowledge: 'Mark as reviewed',
      stalled: 'Still running, but the bot has not reported progress for a while.',
      gone: 'This request is no longer available. Refresh the meeting.',
      lost: message => `Could not check the request: ${message}`,
      recording: 'This meeting is still being recorded.',
      busyNow: 'This meeting is being processed right now.',
      workerStale: 'The bot has not checked in for a while, so the command may wait until it is back online.'
    },
    status: {
      title: 'Processing',
      worker: { recent: 'The bot is online and processing.', stale: 'The bot has not checked in recently. Is the gateway running?', unknown: 'The bot has not checked in yet. It reports once the gateway runs this version of the plugin.' },
      lastSeen: when => `Last seen ${when}`,
      running: 'In progress', queued: 'Waiting', failed: 'Need attention',
      jobs: 'Meetings in the queue', noJobs: 'Nothing is waiting to be processed.',
      waiting: 'Waiting for a place to publish', noWaiting: 'Every meeting found a place to publish.',
      dmNotes: 'Notes left in a direct message',
      commands: 'Recent actions from this page', noCommands: 'No actions yet.',
      warnings: 'Settings that could not be used',
      warningsHelp: 'These values are invalid, so the default is used instead. Fix them in Settings.',
      google: 'Google Meet',
      googleConnected: 'Connected. New Meet transcripts are imported automatically.',
      googleConnectedOff: 'Connected, but importing is turned off.',
      googleRevoked: 'The connection was revoked. Connect again from a terminal.',
      googleNot: 'Not connected.',
      googleGuide: 'To connect, run these commands in a terminal on the machine where Hermes runs:',
      googleLastPoll: when => `Last check ${when}`,
      googleLastImport: when => `Last import ${when}`,
      googleError: message => `Last error: ${message}`,
      doctor: 'Diagnostics', doctorHelp: 'Checks the configuration, storage and connected services.',
      doctorRun: 'Run diagnostics', doctorAgain: 'Run again',
      doctorOk: 'Everything looks good.', doctorIssues: n => (n === 1 ? '1 problem found.' : `${n} problems found.`),
      check: { ok: 'OK', warn: 'Check', fail: 'Problem' },
      openMeeting: 'Open meeting',
      cmd: { queued: 'Queued', running: 'Running', done: 'Done', failed: 'Failed', unknown: 'Unknown', acknowledged: 'Reviewed' }
    },
    choice: {
      transcribe_device: { auto: 'Automatic', cpu: 'Processor (CPU)', cuda: 'Graphics card (CUDA)' },
      transcribe_compute_type: { auto: 'Automatic' },
      kanban_mode: { approve: 'Ask before creating', auto: 'Create automatically', off: 'Off' },
      linear_mode: { approve: 'Ask before creating', auto: 'Create automatically', off: 'Off' },
      audio_retention: { multitrack: 'One track per person', mixed: 'One mixed track', none: 'Do not keep audio' },
      ui_language: { en: 'English', es: 'Spanish' }
    },
    settings: {
      intro: 'Changes apply to the bot within a few seconds. Values set by an administrator cannot be changed here.',
      origin: { default: 'Default', configured: 'Custom', invalid: 'Invalid, using the default' },
      defaultIs: value => `Default: ${value}`,
      listHelp: 'One per line.',
      empty: 'empty',
      yes: 'On', no: 'Off',
      saveError: message => message,
      invalidNumber: 'Enter a number.',
      invalidInteger: 'Enter a whole number.',
      min: n => `Must be at least ${n}.`,
      max: n => `Must be at most ${n}.`,
      requeued: n => (n === 1 ? '1 waiting meeting will be published again.' : `${n} waiting meetings will be published again.`),
      saving: 'Saving…',
      jump: 'Jump to section'
    },
    llm: {
      title: 'Models',
      intro: 'The model that writes the notes, and the backups Hermes tries in order when it fails (limits, connection or billing errors).',
      primary: 'Main model', provider: 'Provider', model: 'Model', baseUrl: 'Own endpoint (optional)',
      timeout: 'Time limit per call (s)',
      providerHelp: '“auto” uses the main model of Hermes.',
      effective: label => `In use: ${label}`,
      fallbacks: 'Backups, in order', noFallbacks: 'No backups: if the main model fails, the meeting waits and retries.',
      add: 'Add backup', remove: 'Remove', up: 'Move up', down: 'Move down',
      save: 'Save models', saved: 'Models saved.',
      problems: 'Problems', source: { 'hermes-config': 'Custom', 'plugin-default': 'Default' },
      row: n => `Backup ${n}`,
      needProvider: 'Every backup needs a provider.'
    }
  },
  es: {
    nav: 'Reuniones',
    open: 'Abrir reuniones',
    title: 'Reuniones',
    subtitle: 'Grabaciones, notas y tareas de tus reuniones.',
    tabs: { library: 'Biblioteca', status: 'Estado', settings: 'Ajustes' },
    common: {
      retry: 'Reintentar', loading: 'Cargando…', save: 'Guardar', saved: 'Guardado', cancel: 'Cancelar',
      back: 'Volver a la biblioteca', none: '—', refresh: 'Actualizar', open: 'Abrir', copy: 'Copiar',
      notAvailable: 'No disponible'
    },
    error: {
      title: 'Algo salió mal',
      notFound: 'Esta reunión ya no existe.',
      disabled: 'No se puede conectar con el módulo de Reuniones. Comprueba que el plugin meeting-scribe esté activado en este perfil y que Hermes se haya reiniciado después de instalarlo.',
      generic: message => `El servidor respondió: ${message}`
    },
    library: {
      search: 'Busca en títulos y en lo que se dijo…',
      searchLabel: 'Buscar reuniones',
      source: 'Origen', state: 'Estado', since: 'Desde', until: 'Hasta',
      allSources: 'Todos los orígenes', allStates: 'Todos los estados',
      clear: 'Quitar filtros',
      count: (shown, total) => `${shown} en pantalla · ${total} en total`,
      prev: 'Más recientes', next: 'Más antiguas', page: n => `Página ${n}`,
      emptyTitle: 'Todavía no hay reuniones',
      emptyBody: 'Las reuniones aparecen aquí cuando el bot graba una llamada de voz de Discord o importa una transcripción de Google Meet.',
      noMatchTitle: 'Nada coincide',
      noMatchBody: 'Prueba con otras palabras o quita los filtros.',
      untitled: 'Reunión sin título',
      speakers: n => (n === 1 ? '1 persona' : `${n} personas`),
      duration: m => `${m} min`
    },
    source: { discord: 'Discord', google_meet: 'Google Meet' },
    state: {
      recording: 'Grabando', captured: 'Esperando proceso', transcribing: 'Transcribiendo',
      transcribed: 'Transcrita', analyzing: 'Escribiendo notas', analyzed: 'Notas listas',
      delivering: 'Publicando', done: 'Lista', failed: 'Requiere atención', processing: 'En proceso',
      empty: 'Sin audio: descartada'
    },
    detail: {
      summary: 'Resumen', tldr: 'En pocas palabras', topics: 'Temas', decisions: 'Decisiones',
      questions: 'Preguntas abiertas', tasks: 'Tareas', transcript: 'Transcripción', audio: 'Grabación',
      noNotes: 'Las notas aún no están listas. Aparecerán aquí cuando termine el proceso de la reunión.',
      empty: 'No se captó audio en esta grabación (nadie habló), así que no hay notas. No se publicó nada y no hay nada que reprocesar.',
      noDecisions: 'No se registraron decisiones.', noQuestions: 'No hay preguntas abiertas.',
      noTasks: 'De esta reunión no salieron tareas.',
      owner: 'Responsable', due: 'Fecha', project: 'Proyecto', noProject: 'Sin proyecto',
      destinations: 'A dónde fue', discord: 'Discord', kanban: 'Kanban', linear: 'Linear',
      openLink: 'Abrir', partial: 'Grabación parcial: el bot entró tarde o la llamada se cortó.',
      startedAt: 'Inicio', people: 'Personas', where: 'Canal',
      waiting: 'Esperando un lugar donde publicar',
      waitingHelp: 'Las notas están listas pero no se pudo usar ningún canal de Discord. Elige un canal de notas en Ajustes → Dónde se publica y se enviarán solas.',
      dmNotes: 'Las notas quedaron en un mensaje directo',
      dmNotesHelp: 'El bot no pudo publicar en el servidor y envió las notas por mensaje directo. Se mueven al canal en cuanto haya uno disponible.',
      jobFailed: stage => `El proceso se detuvo mientras estaba ${stage}.`,
      jobRetry: 'Se reintentará automáticamente.',
      attempts: n => `Intentos: ${n}`,
      stage: { transcribe: 'transcribiendo', analyze: 'escribiendo las notas', deliver: 'publicando', archive: 'archivando' }
    },
    sink: {
      pending: 'Pendiente', approved: 'Aprobada, enviando…', delivered: 'Enviada', dismissed: 'Descartada',
      failed: 'Falló', skipped: 'Omitida'
    },
    transcript: {
      search: 'Buscar en la transcripción…', searchLabel: 'Buscar en la transcripción',
      lines: (shown, total) => `${shown} de ${total} líneas cargadas`,
      more: 'Cargar más', all: 'Cargar todo',
      matches: n => (n === 1 ? '1 línea coincide' : `${n} líneas coinciden`),
      partialSearch: 'Solo se busca en las líneas cargadas. Carga todo para buscar en la reunión completa.',
      empty: 'Esta reunión aún no tiene transcripción.',
      noMatch: 'Ninguna línea coincide.'
    },
    audio: {
      label: 'Grabación de la reunión',
      mixed: 'Escucha la reunión completa.',
      multitrack: 'Esta reunión se guardó con una pista por persona, un formato que esta página no puede reproducir. Los archivos siguen en el equipo donde corre Hermes.',
      imported: 'Las reuniones importadas de Google Meet llegan solo con su transcripción, sin audio.',
      not_retained: 'No se conservó el audio de esta reunión (ver «Audio conservado» en Ajustes → Privacidad).',
      failed: 'No se pudo reproducir la grabación aquí. El archivo sigue en el equipo donde corre Hermes.'
    },
    reprocess: {
      button: 'Reprocesar…',
      title: '¿Reprocesar esta reunión?',
      description: 'Hermes repetirá el paso que elijas y todos los siguientes. Lo que ya se publicó se mantiene, salvo lo que haya que volver a enviar.',
      from: 'Empezar de nuevo desde',
      transcribe: 'Transcripción', transcribeHelp: 'Volver a escuchar la grabación y rehacer la transcripción, las notas y las tareas. Es la opción más lenta.',
      analyze: 'Notas', analyzeHelp: 'Conservar la transcripción y volver a escribir el resumen, las decisiones y las tareas.',
      deliver: 'Publicación', deliverHelp: 'Conservar las notas y volver a enviarlas a Discord y a los demás destinos.',
      confirm: 'Reprocesar', busy: 'Enviando…',
      queued: 'En cola: el bot empezará en unos segundos.',
      running: 'Reprocesando…',
      done: 'Reproceso terminado.',
      failed: message => `El reproceso falló: ${message}`,
      unknown: 'El bot se detuvo durante el reproceso; no se sabe el resultado. Revisa las notas de la reunión y márcalo como revisado para volver a reprocesar.',
      acknowledged: 'Revisado. Ya puedes volver a reprocesar esta reunión.',
      acknowledge: 'Marcar como revisado',
      stalled: 'Sigue en curso, pero el bot no informa avances hace un rato.',
      gone: 'Esta solicitud ya no está disponible. Actualiza la reunión.',
      lost: message => `No se pudo consultar la solicitud: ${message}`,
      recording: 'Esta reunión todavía se está grabando.',
      busyNow: 'Esta reunión se está procesando en este momento.',
      workerStale: 'El bot no ha dado señales hace un rato; la orden puede esperar hasta que vuelva a estar en línea.'
    },
    status: {
      title: 'Procesamiento',
      worker: { recent: 'El bot está en línea y procesando.', stale: 'El bot no ha dado señales recientemente. ¿Está corriendo el gateway?', unknown: 'El bot aún no ha dado señales. Lo hará cuando el gateway use esta versión del plugin.' },
      lastSeen: when => `Última señal ${when}`,
      running: 'En curso', queued: 'En espera', failed: 'Requieren atención',
      jobs: 'Reuniones en la cola', noJobs: 'No hay nada esperando proceso.',
      waiting: 'Esperando un lugar donde publicar', noWaiting: 'Todas las reuniones encontraron dónde publicarse.',
      dmNotes: 'Notas que quedaron en mensaje directo',
      commands: 'Acciones recientes desde esta página', noCommands: 'Aún no hay acciones.',
      warnings: 'Ajustes que no se pudieron usar',
      warningsHelp: 'Estos valores no son válidos y se usa el valor predeterminado. Corrígelos en Ajustes.',
      google: 'Google Meet',
      googleConnected: 'Conectado. Las nuevas transcripciones de Meet se importan solas.',
      googleConnectedOff: 'Conectado, pero la importación está desactivada.',
      googleRevoked: 'Se revocó la conexión. Vuelve a conectar desde una terminal.',
      googleNot: 'No conectado.',
      googleGuide: 'Para conectar, ejecuta estos comandos en una terminal del equipo donde corre Hermes:',
      googleLastPoll: when => `Última revisión ${when}`,
      googleLastImport: when => `Última importación ${when}`,
      googleError: message => `Último error: ${message}`,
      doctor: 'Diagnóstico', doctorHelp: 'Revisa la configuración, el almacenamiento y los servicios conectados.',
      doctorRun: 'Ejecutar diagnóstico', doctorAgain: 'Volver a ejecutar',
      doctorOk: 'Todo en orden.', doctorIssues: n => (n === 1 ? 'Se encontró 1 problema.' : `Se encontraron ${n} problemas.`),
      check: { ok: 'Bien', warn: 'Revisar', fail: 'Problema' },
      openMeeting: 'Abrir reunión',
      cmd: { queued: 'En cola', running: 'En curso', done: 'Hecho', failed: 'Falló', unknown: 'Desconocido', acknowledged: 'Revisado' }
    },
    choice: {
      transcribe_device: { auto: 'Automático', cpu: 'Procesador (CPU)', cuda: 'Tarjeta gráfica (CUDA)' },
      transcribe_compute_type: { auto: 'Automático' },
      kanban_mode: { approve: 'Preguntar antes de crear', auto: 'Crear automáticamente', off: 'Desactivado' },
      linear_mode: { approve: 'Preguntar antes de crear', auto: 'Crear automáticamente', off: 'Desactivado' },
      audio_retention: { multitrack: 'Una pista por persona', mixed: 'Una pista mezclada', none: 'No guardar audio' },
      ui_language: { en: 'Inglés', es: 'Español' }
    },
    settings: {
      intro: 'Los cambios llegan al bot en pocos segundos. Los valores que fijó un administrador no se pueden cambiar aquí.',
      origin: { default: 'Predeterminado', configured: 'Personalizado', invalid: 'No válido, se usa el predeterminado' },
      defaultIs: value => `Predeterminado: ${value}`,
      listHelp: 'Uno por línea.',
      empty: 'vacío',
      yes: 'Sí', no: 'No',
      saveError: message => message,
      invalidNumber: 'Escribe un número.',
      invalidInteger: 'Escribe un número entero.',
      min: n => `Debe ser al menos ${n}.`,
      max: n => `Debe ser como máximo ${n}.`,
      requeued: n => (n === 1 ? '1 reunión en espera se volverá a publicar.' : `${n} reuniones en espera se volverán a publicar.`),
      saving: 'Guardando…',
      jump: 'Ir a la sección'
    },
    llm: {
      title: 'Modelos',
      intro: 'El modelo que escribe las notas y los respaldos que Hermes prueba en orden cuando falla (límites, errores de conexión o de pago).',
      primary: 'Modelo principal', provider: 'Proveedor', model: 'Modelo', baseUrl: 'Endpoint propio (opcional)',
      timeout: 'Tiempo máximo por llamada (s)',
      providerHelp: '«auto» usa el modelo principal de Hermes.',
      effective: label => `En uso: ${label}`,
      fallbacks: 'Respaldos, en orden', noFallbacks: 'Sin respaldos: si el modelo principal falla, la reunión espera y se reintenta.',
      add: 'Añadir respaldo', remove: 'Quitar', up: 'Subir', down: 'Bajar',
      save: 'Guardar modelos', saved: 'Modelos guardados.',
      problems: 'Problemas', source: { 'hermes-config': 'Personalizado', 'plugin-default': 'Predeterminado' },
      row: n => `Respaldo ${n}`,
      needProvider: 'Cada respaldo necesita un proveedor.'
    }
  }
}

// ---------------------------------------------------------------------------------------------
// data
// ---------------------------------------------------------------------------------------------
/** `"404: {\"detail\":\"not found\"}"` (Desktop's transport shape) → `{status, message}`. */
export function parseError(error) {
  const raw = error instanceof Error ? error.message : String(error ?? '')
  const m = /^(\d{3}):\s*([\s\S]*)$/.exec(raw)
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

export function qs(params) {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
  return parts.length ? `?${parts.join('&')}` : ''
}

function rest(path, opts) {
  if (!CTX) return Promise.reject(new Error('meeting-scribe is not registered'))
  return CTX.rest(path, opts)
}

function useScope() {
  const connection = useValue(host.state.connectionId)
  const profile = useValue(host.state.profile)
  return `${connection || 'local'}|${profile || ''}`
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
// formatting
// ---------------------------------------------------------------------------------------------
function useLocale() {
  const { locale } = useI18n()
  return locale || 'en'
}

function fmtDate(value, locale) {
  if (!value) return ''
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return String(value)
  try {
    return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(d)
  } catch {
    return d.toLocaleString()
  }
}

function fmtEpoch(seconds, locale) {
  return typeof seconds === 'number' ? fmtDate(seconds * 1000, locale) : ''
}

export function fmtClock(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0))
  const hh = Math.floor(s / 3600)
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0')
  const ss = String(s % 60).padStart(2, '0')
  return hh ? `${hh}:${mm}:${ss}` : `${mm}:${ss}`
}

function minutesBetween(a, b) {
  if (!a || !b) return null
  const ms = new Date(b).getTime() - new Date(a).getTime()
  return Number.isFinite(ms) && ms > 0 ? Math.max(1, Math.round(ms / 60000)) : null
}

function stateTone(state) {
  if (state === 'done') return 'good'
  if (state === 'failed') return 'bad'
  if (state === 'recording') return 'warn'
  return 'muted'
}

function stateVariant(state) {
  if (state === 'done') return 'success'
  if (state === 'failed') return 'destructive'
  if (state === 'recording') return 'warn'
  return 'muted'
}

function sinkVariant(status) {
  if (status === 'delivered') return 'success'
  if (status === 'failed') return 'destructive'
  if (status === 'approved') return 'warn'
  return 'muted'
}

/** Translate `key`; when the bundle has no such key, return `fallback` instead of the raw key. */
function tOr(t, key, fallback, ...args) {
  const value = t(key, ...args)
  return value === key ? fallback : value
}

function meetingTitle(t, m) {
  return (m && (m.title || m.channel_name)) || t('library.untitled')
}

// ---------------------------------------------------------------------------------------------
// shared UI bits
// ---------------------------------------------------------------------------------------------
function Section({ title, id, actions, children }) {
  const headingId = id ? `${id}-title` : undefined
  return h('section', { className: 'ms-section', id, 'aria-labelledby': headingId },
    h('div', { className: 'ms-section-head' },
      h('h2', { className: 'ms-h2', id: headingId }, title),
      actions ? h('div', { className: 'ms-row' }, actions) : null),
    children)
}

function Loading() {
  const t = usePluginI18n(ID)
  return h('div', { className: 'ms-loading', role: 'status', 'aria-live': 'polite' }, t('common.loading'))
}

/** 404 on a list/status/settings read means the backend route is not mounted (plugin disabled or
 *  Hermes not restarted); on a meeting read (`missingIsMeeting`) it means the meeting is gone. */
export function failureKey(error, missingIsMeeting) {
  const { status } = parseError(error)
  if (status === 404) return missingIsMeeting ? 'error.notFound' : 'error.disabled'
  if (/bridge unavailable|not registered/i.test(parseError(error).message)) return 'error.disabled'
  return 'error.generic'
}

function Failure({ error, onRetry, missingIsMeeting = false }) {
  const t = usePluginI18n(ID)
  const { status, message } = parseError(error)
  const key = failureKey(error, missingIsMeeting)
  const description = key === 'error.generic' ? t(key, message || String(status)) : t(key)
  return h('div', { className: 'ms-failure', role: 'alert' },
    h(ErrorState, { title: t('error.title'), description },
      onRetry ? h(Button, { onClick: onRetry, variant: 'secondary', type: 'button' }, t('common.retry')) : null))
}

function Field({ label, htmlFor, help, error, children }) {
  return h('div', { className: 'ms-field' },
    h('label', { className: 'ms-label', htmlFor }, label),
    children,
    help ? h('p', { className: 'ms-help', id: htmlFor ? `${htmlFor}-help` : undefined }, help) : null,
    error ? h('p', { className: 'ms-error', role: 'alert', id: htmlFor ? `${htmlFor}-error` : undefined }, error) : null)
}

function describedBy(id, help, error) {
  return [help ? `${id}-help` : '', error ? `${id}-error` : ''].filter(Boolean).join(' ') || undefined
}

function ExternalLink({ href, children }) {
  if (!href || !/^https:\/\//i.test(href)) return null
  const open = event => {
    event.preventDefault()
    if (CTX?.os?.openExternal) CTX.os.openExternal(href)
  }
  return h('a', { className: 'ms-link', href, onClick: open, rel: 'noreferrer noopener', target: '_blank' }, children)
}

// ---------------------------------------------------------------------------------------------
// library
// ---------------------------------------------------------------------------------------------
const STATE_FILTERS = ['recording', 'processing', 'done', 'failed', 'empty']
const SOURCE_FILTERS = ['discord', 'google_meet']

export function LibraryView() {
  const t = usePluginI18n(ID)
  const locale = useLocale()
  const [text, setText] = useState('')
  const [q, setQ] = useState('')
  const [source, setSource] = useState('')
  const [state, setState] = useState('')
  const [since, setSince] = useState('')
  const [until, setUntil] = useState('')
  const [cursors, setCursors] = useState([''])

  useEffect(() => {
    const timer = setTimeout(() => setQ(text.trim()), 300)
    return () => clearTimeout(timer)
  }, [text])
  // Any filter change restarts from the newest page.
  useEffect(() => setCursors(['']), [q, source, state, since, until])

  const cursor = cursors[cursors.length - 1]
  const path = `/v1/meetings${qs({ q, source, state, since, until, cursor, limit: PAGE_SIZE })}`
  const query = useRest(path, { refetchInterval: 30_000 })
  const data = query.data
  const facets = data?.facets
  const filtered = Boolean(q || source || state || since || until)

  const clear = () => {
    setText('')
    setQ('')
    setSource('')
    setState('')
    setSince('')
    setUntil('')
  }

  const select = (id, label, value, onChange, options) =>
    h('div', { className: 'ms-filter' },
      h('label', { className: 'ms-label', htmlFor: id }, label),
      h('select', { className: 'ms-input', id, value, onChange: e => onChange(e.target.value) },
        options.map(([v, text]) => h('option', { key: v || 'all', value: v }, text))))

  const count = n => (typeof n === 'number' ? ` (${n})` : '')

  let body
  if (query.isLoading) body = h(Loading, null)
  else if (query.isError) body = h(Failure, { error: query.error, onRetry: () => query.refetch() })
  else if (!data?.items?.length) {
    body = filtered
      ? h(EmptyState, { title: t('library.noMatchTitle'), description: t('library.noMatchBody') })
      : h(EmptyState, { title: t('library.emptyTitle'), description: t('library.emptyBody') })
  } else {
    body = h(Fragment, null,
      h('ul', { className: 'ms-list', 'aria-label': t('tabs.library') },
        data.items.map(m => h(MeetingRow, { key: m.id, meeting: m, locale }))),
      h('nav', { className: 'ms-pager', 'aria-label': t('library.page', cursors.length) },
        h(Button, {
          type: 'button', variant: 'secondary', disabled: cursors.length <= 1,
          onClick: () => setCursors(c => c.slice(0, -1))
        }, t('library.prev')),
        h('span', { className: 'ms-muted' }, t('library.page', cursors.length)),
        h(Button, {
          type: 'button', variant: 'secondary', disabled: !data.next_cursor,
          onClick: () => setCursors(c => [...c, data.next_cursor])
        }, t('library.next'))))
  }

  return h('div', { className: 'ms-stack' },
    h('div', { className: 'ms-toolbar', role: 'search' },
      h('div', { className: 'ms-search' },
        h(SearchField, {
          'aria-label': t('library.searchLabel'), placeholder: t('library.search'), value: text,
          onChange: setText, loading: query.isFetching && Boolean(q), variant: 'box'
        })),
      select('ms-f-source', t('library.source'), source, setSource,
        [['', t('library.allSources')], ...SOURCE_FILTERS.map(s => [s, t(`source.${s}`) + count(facets?.sources?.[s])])]),
      select('ms-f-state', t('library.state'), state, setState,
        [['', t('library.allStates')],
          ...STATE_FILTERS.map(s => [s, t(`state.${s === 'processing' ? 'processing' : s}`) + count(facets?.states?.[s])])]),
      h('div', { className: 'ms-filter' },
        h('label', { className: 'ms-label', htmlFor: 'ms-f-since' }, t('library.since')),
        h('input', { className: 'ms-input', id: 'ms-f-since', type: 'date', value: since, max: until || undefined, onChange: e => setSince(e.target.value) })),
      h('div', { className: 'ms-filter' },
        h('label', { className: 'ms-label', htmlFor: 'ms-f-until' }, t('library.until')),
        h('input', { className: 'ms-input', id: 'ms-f-until', type: 'date', value: until, min: since || undefined, onChange: e => setUntil(e.target.value) })),
      filtered ? h(Button, { type: 'button', variant: 'text', onClick: clear }, t('library.clear')) : null),
    data?.items?.length && facets
      ? h('p', { className: 'ms-muted', 'aria-live': 'polite' }, t('library.count', data.items.length, facets.total))
      : null,
    body)
}

function MeetingRow({ meeting: m, locale }) {
  const t = usePluginI18n(ID)
  const minutes = minutesBetween(m.started_at, m.ended_at)
  const people = (m.speakers || []).filter(s => !s.is_bot).length
  const meta = [
    fmtDate(m.started_at, locale),
    minutes ? t('library.duration', minutes) : '',
    people ? t('library.speakers', people) : '',
    t(`source.${m.source || 'discord'}`),
    m.project || ''
  ].filter(Boolean).join(' · ')
  return h('li', null,
    h('button', {
      className: 'ms-rowbtn', type: 'button', onClick: () => $selected.set(m.id),
      'aria-label': `${meetingTitle(t, m)} — ${t(`state.${m.state}`)} — ${meta}`
    },
    h(StatusDot, { tone: stateTone(m.state), className: 'ms-dot' }),
    h('span', { className: 'ms-rowmain' },
      h('span', { className: 'ms-rowtitle' }, meetingTitle(t, m)),
      h('span', { className: 'ms-muted ms-small' }, meta)),
    h(Badge, { variant: stateVariant(m.state) }, t(`state.${m.state}`))))
}

// ---------------------------------------------------------------------------------------------
// detail
// ---------------------------------------------------------------------------------------------
export function DetailView({ id }) {
  const t = usePluginI18n(ID)
  const locale = useLocale()
  const [commandId, setCommandId] = useState(null)
  const query = useRest(`/v1/meetings/${encodeURIComponent(id)}`, {
    refetchInterval: q => {
      const d = q?.state?.data
      const busy = d && (['recording', 'captured', 'transcribing', 'transcribed', 'analyzing', 'analyzed', 'delivering']
        .includes(d.meeting?.state) || d.job?.state === 'running')
      return busy ? 10_000 : false
    }
  })
  const d = query.data
  const trackedCommand = commandId || (d?.command && ['queued', 'running', 'unknown'].includes(d.command.state) ? d.command.id : null)

  const back = h(Button, { type: 'button', variant: 'ghost', onClick: () => $selected.set(null) },
    h(Codicon, { name: 'arrow-left', size: '0.8rem' }), t('common.back'))

  if (query.isLoading) return h('div', { className: 'ms-stack' }, back, h(Loading, null))
  if (query.isError) return h('div', { className: 'ms-stack' }, back, h(Failure, { error: query.error, onRetry: () => query.refetch(), missingIsMeeting: true }))
  if (!d) return null

  const m = d.meeting
  const notes = d.notes
  const people = (m.speakers || []).filter(s => !s.is_bot).map(s => s.name).join(', ')
  const job = d.job

  return h('article', { className: 'ms-stack', 'aria-labelledby': 'ms-detail-title' },
    back,
    h('header', { className: 'ms-detail-head' },
      h('div', { className: 'ms-stack-sm' },
        h('h1', { className: 'ms-h1', id: 'ms-detail-title' }, meetingTitle(t, m)),
        h('div', { className: 'ms-row ms-wrap' },
          h(Badge, { variant: stateVariant(m.state) }, t(`state.${m.state}`)),
          h(Badge, { variant: 'outline' }, t(`source.${m.source || 'discord'}`)),
          m.project ? h(Badge, { variant: 'outline' }, m.project) : null),
        h('dl', { className: 'ms-meta' },
          h('dt', null, t('detail.startedAt')), h('dd', null, fmtDate(m.started_at, locale)),
          m.channel_name ? h('dt', null, t('detail.where')) : null,
          m.channel_name ? h('dd', null, [m.guild_name, m.channel_name].filter(Boolean).join(' › ')) : null,
          people ? h('dt', null, t('detail.people')) : null,
          people ? h('dd', null, people) : null)),
      m.state === 'empty' ? null : h(ReprocessControl, {
        meeting: m, job, commandId: trackedCommand,
        onSubmitted: rid => setCommandId(rid), onFinished: () => query.refetch()
      })),
    m.state === 'empty' ? h('p', { className: 'ms-note', role: 'status' }, t('detail.empty')) : null,
    m.partial ? h('p', { className: 'ms-note' }, t('detail.partial')) : null,
    job && job.state === 'failed'
      ? h('div', { className: 'ms-note ms-note-bad', role: 'status' },
        h('strong', null, t('detail.jobFailed', tOr(t, `detail.stage.${job.failed_stage || job.stage}`, job.failed_stage || job.stage))),
        job.error ? h('p', { className: 'ms-small ms-mono' }, job.error) : null,
        h('p', { className: 'ms-small ms-muted' }, t('detail.attempts', job.attempts)))
      : null,
    d.waiting_destination
      ? h('div', { className: 'ms-note ms-note-warn', role: 'status' },
        h('strong', null, t('detail.waiting')), h('p', null, t('detail.waitingHelp')),
        h('p', { className: 'ms-small ms-muted' }, d.waiting_destination))
      : null,
    d.dm_notes
      ? h('div', { className: 'ms-note', role: 'status' }, h('strong', null, t('detail.dmNotes')), h('p', null, t('detail.dmNotesHelp')))
      : null,
    m.state === 'empty' ? null : h(NotesBlock, { notes }),
    h(TasksBlock, { tasks: d.tasks || [] }),
    h(AudioBlock, { audio: d.audio || {} }),
    h(TranscriptBlock, { id: m.id, total: d.transcript_total || 0 }))
}

function NotesBlock({ notes }) {
  const t = usePluginI18n(ID)
  if (!notes) return h(Section, { title: t('detail.summary'), id: 'ms-summary' }, h('p', { className: 'ms-muted' }, t('detail.noNotes')))
  const list = (items, empty) => (items && items.length
    ? h('ul', { className: 'ms-bullets' }, items.map((x, i) => h('li', { key: i }, x)))
    : h('p', { className: 'ms-muted' }, empty))
  return h(Fragment, null,
    h(Section, { title: t('detail.summary'), id: 'ms-summary' },
      notes.tldr ? h('p', { className: 'ms-lead' }, notes.tldr) : null,
      notes.summary ? h('p', { className: 'ms-prose' }, notes.summary) : null,
      notes.topics?.length
        ? h('div', { className: 'ms-stack-sm' },
          h('h3', { className: 'ms-h3' }, t('detail.topics')),
          notes.topics.map((topic, i) => h('div', { key: i },
            h('strong', null, topic.title),
            topic.points?.length ? h('ul', { className: 'ms-bullets' }, topic.points.map((p, j) => h('li', { key: j }, p))) : null)))
        : null),
    h('div', { className: 'ms-grid2' },
      h(Section, { title: t('detail.decisions'), id: 'ms-decisions' }, list(notes.decisions, t('detail.noDecisions'))),
      h(Section, { title: t('detail.questions'), id: 'ms-questions' }, list(notes.open_questions, t('detail.noQuestions')))))
}

function SinkBadge({ label, status, url }) {
  const t = usePluginI18n(ID)
  return h('span', { className: 'ms-sink' },
    h('span', { className: 'ms-small ms-muted' }, `${label}:`),
    h(Badge, { variant: sinkVariant(status) }, tOr(t, `sink.${status}`, status)),
    url ? h(ExternalLink, { href: url }, t('detail.openLink')) : null)
}

function TasksBlock({ tasks }) {
  const t = usePluginI18n(ID)
  return h(Section, { title: `${t('detail.tasks')} (${tasks.length})`, id: 'ms-tasks' },
    tasks.length === 0
      ? h('p', { className: 'ms-muted' }, t('detail.noTasks'))
      : h('ul', { className: 'ms-tasks' }, tasks.map(task => h('li', { key: task.id, className: 'ms-task' },
        h('div', { className: 'ms-row ms-wrap ms-between' },
          h('strong', null, task.title),
          h(Badge, { variant: task.status === 'dismissed' ? 'muted' : task.status === 'delivered' ? 'success' : 'outline' },
            tOr(t, `sink.${task.status}`, task.status))),
        task.description ? h('p', { className: 'ms-small' }, task.description) : null,
        h('dl', { className: 'ms-meta ms-small' },
          h('dt', null, t('detail.owner')), h('dd', null, task.owner_name || t('common.none')),
          h('dt', null, t('detail.project')), h('dd', null, task.project || t('detail.noProject')),
          task.due ? h('dt', null, t('detail.due')) : null, task.due ? h('dd', null, task.due) : null),
        h('div', { className: 'ms-row ms-wrap', 'aria-label': t('detail.destinations') },
          task.discord ? h(SinkBadge, { label: t('detail.discord'), status: 'delivered', url: task.discord.url }) : null,
          h(SinkBadge, { label: t('detail.kanban'), status: task.sinks?.kanban?.status || 'pending', url: task.sinks?.kanban?.url }),
          h(SinkBadge, { label: t('detail.linear'), status: task.sinks?.linear?.status || 'pending', url: task.sinks?.linear?.url }))))))
}

/** Resolve one transport only; a remote path must never be opened on this machine. */
export function audioSources(audio, connectionId, profile, mode) {
  if (!audio || !audio.available || !audio.path) return []
  const file = encodeURIComponent(audio.path)
  if (mode === 'local') return [`hermes-media://stream/${file}`]
  // Registry kinds are local | remote | ssh | cloud: everything but local lives on another machine.
  if (!mode || !connectionId) return []
  const scope = [connectionId ? `connectionId=${encodeURIComponent(connectionId)}` : '',
    profile ? `profile=${encodeURIComponent(profile)}` : ''].filter(Boolean).join('&')
  return [`hermes-media://remote/${file}${scope ? `?${scope}` : ''}`]
}

function AudioBlock({ audio }) {
  const t = usePluginI18n(ID)
  const connectionId = useValue(host.state.connectionId)
  const profile = useValue(host.state.profile)
  // Public SDK registry supplies kind: local/remote. Unknown connections fail closed.
  const connection = useQuery({
    queryKey: [Q, 'audio-connection', connectionId],
    queryFn: async () => (await host.connections()).find(c => c.id === connectionId) || null,
    enabled: Boolean(connectionId) && Boolean(audio.available), retry: false
  })
  const sources = useMemo(() => audioSources(audio, connectionId, profile, connection.data?.kind),
    [audio?.path, audio?.available, connectionId, profile, connection.data?.kind])
  const [attempt, setAttempt] = useState(0)
  useEffect(() => setAttempt(0), [audio?.path, connectionId, profile, connection.data?.kind])
  let body
  if (!audio.available) body = h('p', { className: 'ms-muted' }, tOr(t, `audio.${audio.reason}`, t('audio.not_retained')))
  else if (connection.isLoading) body = h(Loading, null)
  else if (attempt >= sources.length) body = h('p', { className: 'ms-muted' }, t('audio.failed'))
  else {
    body = h(Fragment, null,
      h('p', { className: 'ms-small ms-muted' }, t('audio.mixed')),
      h('audio', {
        key: sources[attempt], className: 'ms-audio', controls: true, preload: 'metadata', src: sources[attempt],
        'aria-label': t('audio.label'), onError: () => setAttempt(n => n + 1)
      }))
  }
  return h(Section, { title: t('detail.audio'), id: 'ms-audio' }, body)
}

function TranscriptBlock({ id, total }) {
  const t = usePluginI18n(ID)
  const first = useRest(total ? `/v1/meetings/${encodeURIComponent(id)}/transcript${qs({ limit: TRANSCRIPT_PAGE })}` : '')
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

  let body
  if (!total) body = h('p', { className: 'ms-muted' }, t('transcript.empty'))
  else if (first.isLoading) body = h(Loading, null)
  else if (first.isError) body = h(Failure, { error: first.error, onRetry: () => first.refetch(), missingIsMeeting: true })
  else {
    body = h(Fragment, null,
      h('div', { className: 'ms-row ms-wrap ms-between' },
        h('div', { className: 'ms-search' },
          h(SearchField, { 'aria-label': t('transcript.searchLabel'), placeholder: t('transcript.search'), value: text, onChange: setText, variant: 'box' })),
        h('span', { className: 'ms-small ms-muted', 'aria-live': 'polite' },
          needle ? t('transcript.matches', shown.length) : t('transcript.lines', items.length, total))),
      needle && nextCursor ? h('p', { className: 'ms-small ms-muted' }, t('transcript.partialSearch')) : null,
      shown.length === 0 && needle
        ? h('p', { className: 'ms-muted' }, t('transcript.noMatch'))
        : h('ol', { className: 'ms-transcript' }, shown.map(u => h('li', { key: u.id, className: 'ms-utt' },
          h('span', { className: 'ms-mono ms-small ms-muted ms-time' }, fmtClock(u.t0)),
          h('span', { className: 'ms-speaker' }, u.speaker || u.speaker_id),
          h('span', { className: 'ms-utt-text' }, highlight(u.text, needle))))),
      extra.error ? h('p', { className: 'ms-error', role: 'alert' }, parseError(extra.error).message) : null,
      nextCursor
        ? h('div', { className: 'ms-row' },
          h(Button, { type: 'button', variant: 'secondary', disabled: extra.busy, onClick: () => load(false) }, extra.busy ? t('common.loading') : t('transcript.more')),
          h(Button, { type: 'button', variant: 'ghost', disabled: extra.busy, onClick: () => load(true) }, t('transcript.all')))
        : null)
  }
  return h(Section, { title: t('detail.transcript'), id: 'ms-transcript' }, body)
}

function highlight(text, needle) {
  const value = String(text || '')
  if (!needle) return value
  const lower = value.toLowerCase()
  const out = []
  let from = 0
  let at = lower.indexOf(needle, from)
  while (at >= 0) {
    if (at > from) out.push(value.slice(from, at))
    out.push(h('mark', { key: at, className: 'ms-mark' }, value.slice(at, at + needle.length)))
    from = at + needle.length
    at = lower.indexOf(needle, from)
  }
  if (from < value.length) out.push(value.slice(from))
  return out.length === 1 ? out[0] : h(Fragment, null, out)
}

// ---------------------------------------------------------------------------------------------
// reprocess (queued command + polling)
// ---------------------------------------------------------------------------------------------
const STAGES = ['transcribe', 'analyze', 'deliver']

function ReprocessControl({ meeting, job, commandId, onSubmitted, onFinished }) {
  const t = usePluginI18n(ID)
  const invalidate = useInvalidate()
  const [open, setOpen] = useState(false)
  const [stage, setStage] = useState('analyze')
  const command = useRest(commandId ? `/v1/commands/${encodeURIComponent(commandId)}` : '', {
    staleTime: 0,
    // Stop on client errors (401/404: session or command gone); keep polling only while work is pending.
    refetchInterval: q => {
      if (q?.state?.error && parseError(q.state.error).status < 500) return false
      const state = q?.state?.data?.state
      return !q?.state?.data || ['queued', 'running'].includes(state) ? POLL_MS : false
    }
  })
  const status = useRest('/v1/status', { staleTime: 15_000 })
  const cmd = command.data
  const settled = cmd && ['done', 'failed', 'unknown', 'acknowledged'].includes(cmd.state)
  const commandError = command.error ? parseError(command.error) : null
  const reported = useRef(null)
  useEffect(() => {
    if (settled && reported.current !== cmd.id) {
      reported.current = cmd.id
      invalidate()
      onFinished?.()
    }
  }, [settled, cmd?.id])

  const recording = meeting.state === 'recording'
  const running = job?.state === 'running'
  const pending = cmd && ['queued', 'running', 'unknown'].includes(cmd.state)
  const [ackError, setAckError] = useState('')
  const acknowledge = async () => {
    setAckError('')
    try {
      await rest(`/v1/commands/${encodeURIComponent(cmd.id)}/acknowledge`, { method: 'POST', body: { confirm: true } })
      command.refetch?.()
      invalidate()
    } catch (error) {
      setAckError(parseError(error).message)
    }
  }
  const disabled = recording || running || pending
  const stages = meeting.source === 'google_meet' ? STAGES.filter(s => s !== 'transcribe') : STAGES

  const submit = async () => {
    const rid = newRequestId()
    try {
      await rest(`/v1/meetings/${encodeURIComponent(meeting.id)}/commands`, {
        method: 'POST', body: { request_id: rid, action: 'reprocess', stage, confirm: true }
      })
    } catch (error) {
      throw new Error(parseError(error).message)
    }
    onSubmitted(rid)
  }

  let line = null
  if (cmd) {
    if (cmd.state === 'queued') line = t('reprocess.queued')
    else if (cmd.state === 'running') line = t('reprocess.running')
    else if (cmd.state === 'done') line = t('reprocess.done')
    else if (cmd.state === 'failed') line = t('reprocess.failed', cmd.error || '')
    else if (cmd.state === 'unknown') line = t('reprocess.unknown')
    else if (cmd.state === 'acknowledged') line = t('reprocess.acknowledged')
    if (cmd.state === 'running' && cmd.stalled) line = t('reprocess.stalled')
  } else if (commandError) {
    line = commandError.status === 404 ? t('reprocess.gone') : t('reprocess.lost', commandError.message)
  }
  const hint = recording ? t('reprocess.recording') : running ? t('reprocess.busyNow') : null
  const stale = status.data?.worker?.state && status.data.worker.state !== 'recent'

  return h('div', { className: 'ms-stack-sm ms-reprocess' },
    h(Button, { type: 'button', variant: 'secondary', disabled, onClick: () => setOpen(true) },
      h(Codicon, { name: 'refresh', size: '0.8rem' }), t('reprocess.button')),
    hint ? h('p', { className: 'ms-small ms-muted' }, hint) : null,
    line ? h('p', { className: `ms-small ${cmd.state === 'failed' || cmd.state === 'unknown' ? 'ms-error' : 'ms-muted'}`, role: 'status', 'aria-live': 'polite' }, line) : null,
    pending && stale ? h('p', { className: 'ms-small ms-muted' }, t('reprocess.workerStale')) : null,
    cmd?.state === 'unknown' ? h(Button, { type: 'button', variant: 'secondary', onClick: acknowledge }, t('reprocess.acknowledge')) : null,
    ackError ? h('p', { className: 'ms-small ms-error', role: 'alert' }, ackError) : null,
    h(ConfirmDialog, {
      open, onClose: () => setOpen(false), onConfirm: submit, title: t('reprocess.title'),
      description: t('reprocess.description'), confirmLabel: t('reprocess.confirm'), busyLabel: t('reprocess.busy'),
      cancelLabel: t('common.cancel')
    },
    h('fieldset', { className: 'ms-fieldset' },
      h('legend', { className: 'ms-label' }, t('reprocess.from')),
      stages.map(s => h('label', { key: s, className: 'ms-radio' },
        h('input', { type: 'radio', name: 'ms-reprocess-stage', value: s, checked: stage === s, onChange: () => setStage(s) }),
        h('span', null, h('strong', null, t(`reprocess.${s}`)), h('span', { className: 'ms-small ms-muted ms-block' }, t(`reprocess.${s}Help`))))))))
}

// ---------------------------------------------------------------------------------------------
// status + diagnostics
// ---------------------------------------------------------------------------------------------
export function StatusView() {
  const t = usePluginI18n(ID)
  const locale = useLocale()
  const query = useRest('/v1/status', { refetchInterval: 15_000 })
  const s = query.data
  if (query.isLoading) return h(Loading, null)
  if (query.isError) return h(Failure, { error: query.error, onRetry: () => query.refetch() })
  if (!s) return null
  const worker = s.worker || {}
  const open = id => h(Button, { type: 'button', variant: 'link', onClick: () => { $selected.set(id); $tab.set('library') } }, t('status.openMeeting'))
  return h('div', { className: 'ms-stack' },
    h(Section, { title: t('status.title'), id: 'ms-processing', actions: h(Button, { type: 'button', variant: 'ghost', onClick: () => query.refetch() }, t('common.refresh')) },
      h('p', { className: 'ms-row' }, h(StatusDot, { tone: worker.state === 'recent' ? 'good' : worker.state === 'stale' ? 'bad' : 'warn' }),
        tOr(t, `status.worker.${worker.state}`, t('status.worker.unknown'))),
      worker.last_seen ? h('p', { className: 'ms-small ms-muted' }, t('status.lastSeen', fmtEpoch(worker.last_seen, locale))) : null,
      h('div', { className: 'ms-counters' },
        ['running', 'queued', 'failed'].map(k => h('div', { key: k, className: 'ms-counter' },
          h('span', { className: 'ms-counter-n' }, String(s.counts?.[k] ?? 0)), h('span', { className: 'ms-small ms-muted' }, t(`status.${k}`))))),
      h('h3', { className: 'ms-h3' }, t('status.jobs')),
      s.jobs?.length
        ? h('ul', { className: 'ms-plain' }, s.jobs.map(j => h('li', { key: j.meeting_id, className: 'ms-row ms-wrap ms-between' },
          h('span', null, h('strong', null, j.title || j.meeting_id), ' · ', tOr(t, `detail.stage.${j.stage}`, j.stage || ''),
            j.state === 'failed' && j.error ? h('span', { className: 'ms-small ms-error ms-block' }, j.error) : null),
          h('span', { className: 'ms-row' }, h(Badge, { variant: j.state === 'failed' ? 'destructive' : j.state === 'running' ? 'warn' : 'muted' }, t(`status.${j.state}`)), open(j.meeting_id)))))
        : h('p', { className: 'ms-muted' }, t('status.noJobs')),
      h('h3', { className: 'ms-h3' }, t('status.waiting')),
      s.waiting_destination?.length
        ? h('ul', { className: 'ms-plain' }, s.waiting_destination.map(w => h('li', { key: w.meeting_id, className: 'ms-row ms-wrap ms-between' },
          h('span', null, h('strong', null, w.title), h('span', { className: 'ms-small ms-muted ms-block' }, w.detail)), open(w.meeting_id))))
        : h('p', { className: 'ms-muted' }, t('status.noWaiting')),
      s.dm_notes?.length
        ? h(Fragment, null, h('h3', { className: 'ms-h3' }, t('status.dmNotes')),
          h('ul', { className: 'ms-plain' }, s.dm_notes.map(w => h('li', { key: w.meeting_id, className: 'ms-row ms-wrap ms-between' }, h('strong', null, w.title), open(w.meeting_id)))))
        : null,
      s.settings_warnings?.length
        ? h('div', { className: 'ms-note ms-note-warn' }, h('strong', null, t('status.warnings')), h('p', { className: 'ms-small' }, t('status.warningsHelp')),
          h('ul', { className: 'ms-bullets ms-mono ms-small' }, s.settings_warnings.map((w, i) => h('li', { key: i }, w))))
        : null,
      h('h3', { className: 'ms-h3' }, t('status.commands')),
      s.commands?.length
        ? h('ul', { className: 'ms-plain' }, s.commands.map(c => h('li', { key: c.id, className: 'ms-row ms-wrap ms-between' },
          h('span', { className: 'ms-small' }, fmtEpoch(c.created_at, locale), c.error ? h('span', { className: 'ms-error ms-block' }, c.error) : null),
          h('span', { className: 'ms-row' }, h(Badge, { variant: c.state === 'done' ? 'success' : c.state === 'failed' ? 'destructive' : 'muted' }, tOr(t, `status.cmd.${c.state}`, c.state)), open(c.meeting_id)))))
        : h('p', { className: 'ms-muted' }, t('status.noCommands'))),
    h(GoogleBlock, { google: s.google || {}, locale }),
    h(DoctorBlock, null))
}

function GoogleBlock({ google: g, locale }) {
  const t = usePluginI18n(ID)
  const line = g.connected ? (g.enabled ? t('status.googleConnected') : t('status.googleConnectedOff'))
    : g.revoked ? t('status.googleRevoked') : t('status.googleNot')
  const commands = g.commands || {}
  const steps = g.connected ? (g.enabled ? [] : [commands.enable]) : [commands.connect, g.enabled ? '' : commands.enable, commands.status]
  return h(Section, { title: t('status.google'), id: 'ms-google' },
    h('p', { className: 'ms-row' }, h(StatusDot, { tone: g.connected && g.enabled ? 'good' : g.connected ? 'warn' : 'muted' }), line),
    g.last_poll_at ? h('p', { className: 'ms-small ms-muted' }, t('status.googleLastPoll', fmtDate(g.last_poll_at, locale))) : null,
    g.last_import_at ? h('p', { className: 'ms-small ms-muted' }, t('status.googleLastImport', fmtDate(g.last_import_at, locale))) : null,
    g.last_error ? h('p', { className: 'ms-small ms-error' }, t('status.googleError', g.last_error)) : null,
    steps.filter(Boolean).length
      ? h('div', { className: 'ms-stack-sm' }, h('p', { className: 'ms-small' }, t('status.googleGuide')),
        h('pre', { className: 'ms-code' }, steps.filter(Boolean).join('\n')))
      : null)
}

function DoctorBlock() {
  const t = usePluginI18n(ID)
  const [asked, setAsked] = useState(false)
  const query = useRest(asked ? '/v1/doctor' : '', { staleTime: 60_000 })
  const r = query.data
  const failed = r ? r.checks.filter(c => c.status !== 'ok').length : 0
  const action = h(Button, { type: 'button', variant: 'secondary', disabled: query.isFetching, onClick: () => (asked ? query.refetch() : setAsked(true)) },
    query.isFetching ? t('common.loading') : asked ? t('status.doctorAgain') : t('status.doctorRun'))
  let body = h('p', { className: 'ms-muted' }, t('status.doctorHelp'))
  if (asked && query.isLoading) body = h(Loading, null)
  else if (query.isError) body = h(Failure, { error: query.error, onRetry: () => query.refetch() })
  else if (r) {
    body = h(Fragment, null,
      h('p', { role: 'status' }, failed ? t('status.doctorIssues', failed) : t('status.doctorOk')),
      h('ul', { className: 'ms-plain' }, r.checks.map(c => h('li', { key: c.name, className: 'ms-check' },
        h(Badge, { variant: c.status === 'ok' ? 'success' : c.status === 'warn' ? 'warn' : 'destructive' }, tOr(t, `status.check.${c.status}`, c.status)),
        h('span', null, h('strong', null, c.name), c.detail ? h('span', { className: 'ms-small ms-muted ms-block' }, c.detail) : null)))))
  }
  return h(Section, { title: t('status.doctor'), id: 'ms-doctor', actions: action }, body)
}

// ---------------------------------------------------------------------------------------------
// settings (form generated from the schema) + models
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
  return v === '' || v === null || v === undefined ? t('settings.empty') : String(v)
}

export function SettingsView() {
  const t = usePluginI18n(ID)
  const locale = useLocale()
  const lang = String(locale).toLowerCase().startsWith('es') ? 'es' : 'en'
  const query = useRest(`/v1/settings${qs({ lang })}`, { staleTime: 30_000 })
  const d = query.data
  if (query.isLoading) return h(Loading, null)
  if (query.isError) return h(Failure, { error: query.error, onRetry: () => query.refetch() })
  if (!d) return null
  const groups = d.schema?.groups || []
  const fields = d.schema?.fields || []
  return h('div', { className: 'ms-stack' },
    h('p', { className: 'ms-muted' }, t('settings.intro')),
    h('nav', { className: 'ms-row ms-wrap', 'aria-label': t('settings.jump') },
      groups.map(g => h('a', { key: g.key, className: 'ms-chip', href: `#ms-g-${g.key}`, onClick: e => { e.preventDefault(); document.getElementById(`ms-g-${g.key}`)?.scrollIntoView({ block: 'start' }) } }, g.label))),
    groups.map(g => (g.key === 'llm'
      ? h(ModelsSection, { key: g.key, id: `ms-g-${g.key}`, title: g.label, llm: d.llm || {}, onSaved: () => query.refetch() })
      : h(Section, { key: g.key, title: g.label, id: `ms-g-${g.key}` },
        h('div', { className: 'ms-form' },
          fields.filter(f => f.group === g.key && f.storage !== 'hermes').map(f =>
            h(SettingField, { key: f.key, field: f, current: d.values?.[f.key] || {}, onSaved: () => query.refetch() })))))))
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
      const out = await rest(`/v1/settings/${encodeURIComponent(field.key)}`, { method: 'PUT', body: { value: checked.value } })
      setState('saved')
      setNote(out?.requeued ? t('settings.requeued', out.requeued) : '')
      onSaved?.()
    } catch (e) {
      setState('idle')
      setError(t('settings.saveError', parseError(e).message.replace(new RegExp(`^${field.key}:\\s*`), '')))
    }
  }

  const help = [field.help, field.type === 'list' ? t('settings.listHelp') : '', t('settings.defaultIs', showDefault(field, t))].filter(Boolean).join(' ')
  const aria = { 'aria-describedby': describedBy(id, help, error), 'aria-invalid': error ? true : undefined }
  let control
  if (field.type === 'bool') {
    control = h('div', { className: 'ms-row' },
      h(Switch, { id, checked: Boolean(draft), disabled: state === 'saving', onCheckedChange: v => { setDraft(v); save(v) }, ...aria }),
      h('span', { className: 'ms-small' }, draft ? t('settings.yes') : t('settings.no')))
  } else if (field.choices?.length) {
    control = h('select', { id, className: 'ms-input', value: String(draft), onChange: e => { setDraft(e.target.value); save(e.target.value) }, ...aria },
      field.choices.map(c => h('option', { key: String(c), value: String(c) }, tOr(t, `choice.${field.key}.${c}`, String(c)))))
  } else if (field.type === 'list') {
    control = h('textarea', { id, className: 'ms-input ms-textarea', rows: Math.min(6, Math.max(2, String(draft).split('\n').length + 1)), value: draft, onChange: e => { setDraft(e.target.value); setState('idle') }, ...aria })
  } else {
    const numeric = field.type === 'int' || field.type === 'float'
    control = h('input', {
      id, className: 'ms-input', type: numeric ? 'number' : 'text', value: draft,
      step: field.type === 'float' ? 'any' : numeric ? 1 : undefined, min: field.minimum, max: field.maximum,
      onChange: e => { setDraft(e.target.value); setState('idle') },
      onKeyDown: e => { if (e.key === 'Enter' && dirty) { e.preventDefault(); save(draft) } },
      ...aria
    })
  }
  const needsButton = field.type !== 'bool' && !field.choices?.length
  const origin = current.origin || 'default'
  return h('div', { className: 'ms-setting' },
    h('div', { className: 'ms-row ms-between' },
      h('label', { className: 'ms-label', htmlFor: id }, field.label),
      h(Badge, { variant: origin === 'invalid' ? 'destructive' : origin === 'configured' ? 'default' : 'muted', size: 'xs' }, tOr(t, `settings.origin.${origin}`, origin))),
    h('div', { className: 'ms-row ms-align-start' },
      h('div', { className: 'ms-grow' }, control),
      needsButton
        ? h(Button, { type: 'button', variant: dirty ? 'default' : 'secondary', disabled: !dirty || state === 'saving', onClick: () => save(draft) },
          state === 'saving' ? t('settings.saving') : state === 'saved' && !dirty ? t('common.saved') : t('common.save'))
        : null),
    h('p', { className: 'ms-help', id: `${id}-help` }, help),
    error ? h('p', { className: 'ms-error', role: 'alert', id: `${id}-error` }, error) : null,
    note ? h('p', { className: 'ms-small ms-muted', role: 'status' }, note) : null)
}

export function ModelsSection({ id, title, llm, onSaved }) {
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

  const input = (fid, label, value, onChange, extra = {}) =>
    h(Field, { label, htmlFor: fid, help: extra.help },
      h('input', { id: fid, className: 'ms-input', value, onChange: e => onChange(e.target.value), type: extra.type || 'text', min: extra.min, 'aria-describedby': extra.help ? `${fid}-help` : undefined }))

  const sources = llm.sources || {}
  const effective = llm.effective ? [llm.effective.provider, llm.effective.model].filter(Boolean).join(' / ') : ''
  return h(Section, { title: title || t('llm.title'), id },
    h('p', { className: 'ms-muted' }, t('llm.intro')),
    effective ? h('p', { className: 'ms-small' }, t('llm.effective', effective)) : null,
    h('fieldset', { className: 'ms-fieldset' },
      h('legend', { className: 'ms-h3' }, t('llm.primary'), sources.provider ? h(Badge, { variant: 'muted', size: 'xs', className: 'ms-ml' }, tOr(t, `llm.source.${sources.provider}`, sources.provider)) : null),
      h('div', { className: 'ms-form' },
        input('ms-llm-provider', t('llm.provider'), form.provider, v => set({ provider: v }), { help: t('llm.providerHelp') }),
        input('ms-llm-model', t('llm.model'), form.model, v => set({ model: v })),
        input('ms-llm-base', t('llm.baseUrl'), form.base_url, v => set({ base_url: v })),
        input('ms-llm-timeout', t('llm.timeout'), form.timeout, v => set({ timeout: v }), { type: 'number', min: 1 }))),
    h('fieldset', { className: 'ms-fieldset' },
      h('legend', { className: 'ms-h3' }, t('llm.fallbacks')),
      form.chain.length === 0 ? h('p', { className: 'ms-muted' }, t('llm.noFallbacks')) : null,
      h('ol', { className: 'ms-plain' }, form.chain.map((r, i) => h('li', { key: r._k, className: 'ms-chain-row', 'aria-label': t('llm.row', i + 1) },
        h('span', { className: 'ms-chain-n', 'aria-hidden': 'true' }, String(i + 1)),
        h('input', { className: 'ms-input', 'aria-label': `${t('llm.row', i + 1)} — ${t('llm.provider')}`, placeholder: t('llm.provider'), value: r.provider, onChange: e => setRow(i, { provider: e.target.value }) }),
        h('input', { className: 'ms-input', 'aria-label': `${t('llm.row', i + 1)} — ${t('llm.model')}`, placeholder: t('llm.model'), value: r.model, onChange: e => setRow(i, { model: e.target.value }) }),
        h('input', { className: 'ms-input', 'aria-label': `${t('llm.row', i + 1)} — ${t('llm.baseUrl')}`, placeholder: t('llm.baseUrl'), value: r.base_url, onChange: e => setRow(i, { base_url: e.target.value }) }),
        h('span', { className: 'ms-row' },
          h(Button, { type: 'button', variant: 'ghost', size: 'xs', disabled: i === 0, onClick: () => move(i, -1), 'aria-label': `${t('llm.up')} — ${t('llm.row', i + 1)}` }, h(Codicon, { name: 'arrow-up', size: '0.75rem' })),
          h(Button, { type: 'button', variant: 'ghost', size: 'xs', disabled: i === form.chain.length - 1, onClick: () => move(i, 1), 'aria-label': `${t('llm.down')} — ${t('llm.row', i + 1)}` }, h(Codicon, { name: 'arrow-down', size: '0.75rem' })),
          h(Button, { type: 'button', variant: 'ghost', size: 'xs', onClick: () => remove(i), 'aria-label': `${t('llm.remove')} — ${t('llm.row', i + 1)}` }, h(Codicon, { name: 'trash', size: '0.75rem' })))))),
      h(Button, { type: 'button', variant: 'secondary', onClick: add, disabled: form.chain.length >= 10 }, h(Codicon, { name: 'add', size: '0.75rem' }), t('llm.add'))),
    llm.problems?.length
      ? h('div', { className: 'ms-note ms-note-warn' }, h('strong', null, t('llm.problems')), h('ul', { className: 'ms-bullets ms-small' }, llm.problems.map((p, i) => h('li', { key: i }, p))))
      : null,
    error ? h('p', { className: 'ms-error', role: 'alert' }, error) : null,
    h('div', { className: 'ms-row' },
      h(Button, { type: 'button', onClick: save, disabled: state === 'saving' }, state === 'saving' ? t('settings.saving') : t('llm.save')),
      state === 'saved' ? h('span', { className: 'ms-small ms-muted', role: 'status' }, t('llm.saved')) : null))
}

// ---------------------------------------------------------------------------------------------
// page
// ---------------------------------------------------------------------------------------------
const TABS = ['library', 'status', 'settings']

export function MeetingsPage() {
  const t = usePluginI18n(ID)
  const tab = useValue($tab)
  const selected = useValue($selected)
  const scope = useScope()
  const firstScope = useRef(scope)
  // A connection/profile switch drops the open meeting: it belongs to the previous profile.
  useEffect(() => {
    if (firstScope.current !== scope) {
      firstScope.current = scope
      $selected.set(null)
    }
  }, [scope])

  const onTabKey = event => {
    const i = TABS.indexOf(tab)
    let next = null
    if (event.key === 'ArrowRight') next = TABS[(i + 1) % TABS.length]
    else if (event.key === 'ArrowLeft') next = TABS[(i + TABS.length - 1) % TABS.length]
    else if (event.key === 'Home') next = TABS[0]
    else if (event.key === 'End') next = TABS[TABS.length - 1]
    if (next) {
      event.preventDefault()
      $tab.set(next)
      document.getElementById(`ms-tab-${next}`)?.focus()
    }
  }

  let panel
  if (tab === 'library') panel = selected ? h(DetailView, { id: selected }) : h(LibraryView, null)
  else if (tab === 'status') panel = h(StatusView, null)
  else panel = h(SettingsView, null)

  return h('div', { className: 'ms-page' },
    h('header', { className: 'ms-page-head' },
      h('div', null,
        h('h1', { className: 'ms-h1' }, t('title')),
        h('p', { className: 'ms-muted ms-small' }, t('subtitle'))),
      h('div', { className: 'ms-tabs', role: 'tablist', 'aria-label': t('title'), onKeyDown: onTabKey },
        TABS.map(k => h('button', {
          key: k, id: `ms-tab-${k}`, type: 'button', role: 'tab', 'aria-selected': tab === k,
          'aria-controls': 'ms-panel', tabIndex: tab === k ? 0 : -1, className: 'ms-tab',
          onClick: () => { if (k === 'library' && tab === 'library') $selected.set(null); $tab.set(k) }
        }, t(`tabs.${k}`))))),
    h('main', { className: 'ms-panel', id: 'ms-panel', role: 'tabpanel', 'aria-labelledby': `ms-tab-${tab}`, tabIndex: -1 }, panel))
}

// Only host CSS variables: the page follows the active theme; no Tailwind class is assumed to exist.
export const CSS = `
.ms-page{display:flex;flex-direction:column;gap:16px;height:100%;overflow:auto;padding:20px 24px 40px;color:var(--ui-text-primary);font-size:13px;line-height:1.5}
.ms-page-head{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px}
.ms-h1{font-size:18px;font-weight:600;margin:0}
.ms-h2{font-size:14px;font-weight:600;margin:0}
.ms-h3{font-size:12px;font-weight:600;margin:8px 0 4px;color:var(--ui-text-secondary)}
.ms-tabs{display:inline-flex;gap:2px;padding:2px;border-radius:6px;background:var(--ui-bg-tertiary)}
.ms-tab{padding:4px 12px;border-radius:4px;font-size:12px;font-weight:500;color:var(--ui-text-secondary);background:transparent;border:0;cursor:pointer}
.ms-tab[aria-selected=true]{background:var(--ui-base,var(--background));color:var(--ui-text-primary);box-shadow:0 1px 2px rgba(0,0,0,.12)}
.ms-tab:focus-visible,.ms-rowbtn:focus-visible,.ms-chip:focus-visible,.ms-link:focus-visible,.ms-input:focus-visible,.ms-panel:focus-visible{outline:2px solid var(--ui-accent);outline-offset:1px}
.ms-panel{outline:none}
.ms-stack{display:flex;flex-direction:column;gap:16px}
.ms-stack-sm{display:flex;flex-direction:column;gap:6px}
.ms-row{display:flex;align-items:center;gap:8px}
.ms-wrap{flex-wrap:wrap}
.ms-between{justify-content:space-between}
.ms-align-start{align-items:flex-start}
.ms-grow{flex:1;min-width:0}
.ms-block{display:block}
.ms-ml{margin-left:6px}
.ms-muted{color:var(--ui-text-tertiary)}
.ms-small{font-size:12px}
.ms-mono{font-family:var(--font-mono,ui-monospace,monospace)}
.ms-lead{font-size:14px;font-weight:500;margin:0 0 6px}
.ms-prose{white-space:pre-wrap;margin:0}
.ms-section{display:flex;flex-direction:column;gap:8px;padding:14px 16px;border-radius:8px;border:1px solid var(--ui-stroke-tertiary);background:var(--ui-bg-elevated,transparent)}
.ms-section-head{display:flex;align-items:center;justify-content:space-between;gap:8px}
.ms-grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
.ms-toolbar{display:flex;flex-wrap:wrap;align-items:flex-end;gap:10px}
.ms-search{flex:1 1 260px;min-width:200px}
.ms-filter{display:flex;flex-direction:column;gap:2px}
.ms-label{font-size:11px;font-weight:600;color:var(--ui-text-secondary)}
.ms-input{min-height:28px;padding:4px 8px;border-radius:5px;border:1px solid var(--ui-stroke-secondary);background:var(--ui-bg-quinary,transparent);color:var(--ui-text-primary);font:inherit;font-size:12px;width:100%;box-sizing:border-box}
.ms-filter .ms-input{width:auto;min-width:140px}
.ms-input[aria-invalid=true]{border-color:var(--ui-red,#d33)}
.ms-textarea{resize:vertical;font-family:var(--font-mono,ui-monospace,monospace)}
.ms-list,.ms-plain,.ms-tasks,.ms-transcript{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:4px}
.ms-rowbtn{display:flex;align-items:center;gap:10px;width:100%;text-align:left;padding:8px 10px;border-radius:6px;border:0;background:transparent;color:inherit;cursor:pointer}
.ms-rowbtn:hover{background:var(--chrome-action-hover,var(--ui-row-hover-background))}
.ms-rowmain{display:flex;flex-direction:column;min-width:0;flex:1}
.ms-rowtitle{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ms-dot{flex-shrink:0;width:8px;height:8px}
.ms-pager{display:flex;align-items:center;justify-content:center;gap:12px;padding-top:4px}
.ms-loading{padding:32px;text-align:center;color:var(--ui-text-tertiary)}
.ms-failure{padding:24px 0}
.ms-detail-head{display:flex;flex-wrap:wrap;justify-content:space-between;gap:16px}
.ms-meta{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:4px 0 0}
.ms-meta dt{color:var(--ui-text-tertiary)}
.ms-meta dd{margin:0}
.ms-note{padding:10px 12px;border-radius:6px;border-left:3px solid var(--ui-stroke-secondary);background:var(--ui-bg-tertiary)}
.ms-note p{margin:4px 0 0}
.ms-note-warn{border-left-color:var(--ui-yellow,#c90)}
.ms-note-bad{border-left-color:var(--ui-red,#d33)}
.ms-bullets{margin:0;padding-left:18px}
.ms-task{padding:10px 0;border-top:1px solid var(--ui-stroke-tertiary);display:flex;flex-direction:column;gap:4px}
.ms-task:first-child{border-top:0;padding-top:0}
.ms-task p{margin:0}
.ms-sink{display:inline-flex;align-items:center;gap:4px}
.ms-link{color:var(--ui-accent);text-decoration:underline;text-underline-offset:2px;font-size:12px}
.ms-audio{width:100%;max-width:560px}
.ms-utt{display:grid;grid-template-columns:52px 140px 1fr;gap:8px;padding:2px 0}
.ms-time{padding-top:1px}
.ms-speaker{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ms-utt-text{white-space:pre-wrap}
.ms-mark{background:color-mix(in srgb,var(--ui-yellow,#fc0) 45%,transparent);color:inherit;border-radius:2px}
.ms-error{color:var(--ui-red,#d33);font-size:12px;margin:0}
.ms-help{color:var(--ui-text-tertiary);font-size:11px;margin:0}
.ms-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px 20px}
.ms-setting,.ms-field{display:flex;flex-direction:column;gap:4px}
.ms-fieldset{border:0;padding:0;margin:0;display:flex;flex-direction:column;gap:8px}
.ms-radio{display:flex;gap:8px;align-items:flex-start;padding:6px 0;cursor:pointer}
.ms-radio input{margin-top:3px}
.ms-chip{padding:2px 8px;border-radius:999px;background:var(--ui-bg-tertiary);color:var(--ui-text-secondary);font-size:11px;text-decoration:none}
.ms-chip:hover{color:var(--ui-text-primary)}
.ms-counters{display:flex;gap:12px;flex-wrap:wrap}
.ms-counter{display:flex;flex-direction:column;min-width:90px;padding:8px 12px;border-radius:6px;background:var(--ui-bg-tertiary)}
.ms-counter-n{font-size:18px;font-weight:600;font-variant-numeric:tabular-nums}
.ms-check{display:flex;gap:10px;align-items:flex-start;padding:4px 0}
.ms-code{margin:0;padding:8px 10px;border-radius:5px;background:var(--ui-inline-code-background,var(--ui-bg-tertiary));font-family:var(--font-mono,ui-monospace,monospace);font-size:12px;white-space:pre-wrap;user-select:text}
.ms-chain-row{display:grid;grid-template-columns:20px 1fr 1fr 1.3fr auto;gap:6px;align-items:center}
.ms-chain-n{color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums;text-align:right}
.ms-reprocess{align-items:flex-end;max-width:320px;text-align:right}
@media (max-width:720px){.ms-utt{grid-template-columns:48px 1fr}.ms-utt-text{grid-column:1/-1}.ms-chain-row{grid-template-columns:20px 1fr}.ms-reprocess{align-items:flex-start;text-align:left}}
`

const plugin = {
  id: ID,
  name: 'Meetings',
  description: 'Meeting library, notes, tasks, transcripts, processing status and settings for meeting-scribe.',
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
