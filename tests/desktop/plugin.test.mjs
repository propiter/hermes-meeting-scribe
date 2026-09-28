// Tests for desktop/plugin.js with node:test. Run through run.mjs (it installs the module hooks).
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const HAS_REACT = process.env.MS_HAS_REACT === '1'
const SOURCE = readFileSync(new URL('../../desktop/plugin.js', import.meta.url), 'utf8')
const mod = await import('../../desktop/plugin.js')
const plugin = mod.default
globalThis.__MS_LOCALES = mod.LOCALES

function fakeCtx() {
  const registered = []
  const disposers = []
  let bundles = null
  const ctx = {
    registered, disposers,
    get bundles() { return bundles },
    register: c => { registered.push(c); return () => registered.splice(registered.indexOf(c), 1) },
    registerMany: cs => { cs.forEach(c => registered.push(c)); return () => cs.forEach(c => registered.splice(registered.indexOf(c), 1)) },
    onDispose: fn => disposers.push(fn),
    rest: async (path, opts) => { ctx.calls.push([path, opts]); return {} },
    calls: [],
    os: { openExternal: async () => true },
    i18n: { register: b => { bundles = b; return () => {} }, t: key => key, onLocaleChange: () => () => {} }
  }
  return ctx
}

// -- structure ----------------------------------------------------------------------------------
test('default export is a HermesPlugin', () => {
  assert.equal(plugin.id, 'meeting-scribe')
  assert.equal(typeof plugin.register, 'function')
  assert.equal(plugin.defaultEnabled, false)
})

test('imports only what the Desktop runtime loader maps', () => {
  const specs = [...SOURCE.matchAll(/^\s*import\s[^'"]*from\s+['"]([^'"]+)['"]/gm)].map(m => m[1])
  assert.ok(specs.length > 0)
  for (const s of specs) assert.ok(['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'].includes(s), s)
  assert.doesNotMatch(SOURCE, /\bimport\(/)
  assert.doesNotMatch(SOURCE, /\beval\(|new Function\(|createElement\(\s*['"]script/)
})

test('register() contributes the page, sidebar row and palette command', () => {
  const ctx = fakeCtx()
  plugin.register(ctx)
  const byArea = Object.fromEntries(ctx.registered.map(c => [c.area, c]))
  assert.deepEqual(byArea.routes.data, { path: '/meeting-scribe' })
  assert.equal(typeof byArea.routes.render, 'function')
  assert.equal(byArea['sidebar.nav'].data.path, '/meeting-scribe')
  assert.equal(byArea['sidebar.nav'].data.codicon, 'mic')
  assert.equal(byArea.palette.data.id, 'meeting-scribe.open')
  assert.ok(ctx.bundles.en && ctx.bundles.es)
  ctx.disposers.forEach(d => d())
})

test('en and es bundles have the same keys', () => {
  const keys = (node, prefix = '') => Object.entries(node).flatMap(([k, v]) =>
    v && typeof v === 'object' ? keys(v, `${prefix}${k}.`) : [`${prefix}${k}`])
  assert.deepEqual(keys(mod.LOCALES.es).sort(), keys(mod.LOCALES.en).sort())
})

test('every literal t() key used in the UI exists in the en bundle', () => {
  const resolve = key => key.split('.').reduce((n, s) => (n && typeof n === 'object' ? n[s] : undefined), mod.LOCALES.en)
  const used = new Set([...SOURCE.matchAll(/\bt\('([a-zA-Z0-9_.]+)'/g)].map(m => m[1]))
  const missing = [...used].filter(k => resolve(k) === undefined)
  assert.deepEqual(missing, [])
})

// -- pure helpers -------------------------------------------------------------------------------
test('parseError reads Desktop transport errors', () => {
  assert.deepEqual(mod.parseError(new Error('404: {"detail":"not found"}')), { status: 404, message: 'not found' })
  assert.deepEqual(mod.parseError(new Error('400: {"detail":"transcribe_beam_size: must be <= 10"}')).message, 'transcribe_beam_size: must be <= 10')
  assert.deepEqual(mod.parseError(new Error('boom')), { status: 0, message: 'boom' })
  // What Electron actually throws: the IPC wrapper prefixes the backend answer.
  const ipc = new Error(`Error invoking remote method 'hermes:api': Error: 404: {"detail":"Plugin not found"}`)
  assert.deepEqual(mod.parseError(ipc), { status: 404, message: 'Plugin not found' })
  assert.equal(mod.isPluginMissing(ipc), true)
  assert.equal(mod.isPluginMissing(new Error(`Error invoking remote method 'hermes:api': Error: 404: {"detail":"meeting not found"}`)), false)
  assert.equal(mod.isPluginMissing(new Error('500: x')), false)
})

test('qs drops empty values and encodes the rest', () => {
  assert.equal(mod.qs({ q: 'a b', source: '', cursor: null, limit: 30 }), '?q=a%20b&limit=30')
  assert.equal(mod.qs({}), '')
})

test('fmtClock', () => {
  assert.equal(mod.fmtClock(0), '00:00')
  assert.equal(mod.fmtClock(75.4), '01:15')
  assert.equal(mod.fmtClock(3725), '1:02:05')
})

test('validateDraft mirrors schema bounds', () => {
  const t = (k, ...a) => `${k}${a.length ? `:${a.join(',')}` : ''}`
  const int = { type: 'int', minimum: 1, maximum: 10 }
  assert.deepEqual(mod.validateDraft(int, '5', t), { value: 5 })
  assert.deepEqual(mod.validateDraft(int, '0', t), { error: 'settings.min:1' })
  assert.deepEqual(mod.validateDraft(int, '11', t), { error: 'settings.max:10' })
  assert.deepEqual(mod.validateDraft(int, '2.5', t), { error: 'settings.invalidInteger' })
  assert.deepEqual(mod.validateDraft(int, 'x', t), { error: 'settings.invalidNumber' })
  assert.deepEqual(mod.validateDraft({ type: 'float', minimum: 0, maximum: 1 }, '0.7', t), { value: 0.7 })
  assert.deepEqual(mod.validateDraft({ type: 'list' }, ' a \n\n b ', t), { value: ['a', 'b'] })
  assert.deepEqual(mod.validateDraft({ type: 'str' }, 'x', t), { value: 'x' })
})

test('audioSources select only the explicitly resolved connection mode', () => {
  assert.deepEqual(mod.audioSources({ available: false, reason: 'imported' }), [])
  const audio = { available: true, path: '/data/m 1/recording.ogg' }
  assert.deepEqual(mod.audioSources(audio, 'c1', 'work', 'remote'),
    ['hermes-media://remote/%2Fdata%2Fm%201%2Frecording.ogg?connectionId=c1&profile=work'])
  assert.deepEqual(mod.audioSources(audio, 'local', 'work', 'local'),
    ['hermes-media://stream/%2Fdata%2Fm%201%2Frecording.ogg'])
  assert.deepEqual(mod.audioSources(audio, 'h1', '', 'ssh'),
    ['hermes-media://remote/%2Fdata%2Fm%201%2Frecording.ogg?connectionId=h1'])
  assert.deepEqual(mod.audioSources(audio, 'c1', 'work', undefined), [])
  assert.deepEqual(mod.audioSources(audio, null, 'work'), [])
})

test('pure helpers: state tone, row subtitle, date range, speaker tone, matches, sinks', () => {
  const t = (k, ...a) => `${k}${a.length ? `:${a.join(',')}` : ''}`
  assert.equal(mod.stateTone('done'), 'good')
  assert.equal(mod.stateTone('failed'), 'bad')
  assert.equal(mod.stateTone('recording'), 'live')
  assert.equal(mod.stateTone('empty'), 'muted')
  assert.equal(mod.stateTone('transcribing'), 'busy')
  assert.equal(mod.rowSubtitle(t, { state: 'done', people: 4, task_count: 6 }), 'library.people:4 · library.tasks:6')
  assert.equal(mod.rowSubtitle(t, { state: 'done', people: 0, task_count: 0 }), 'library.noTasks')
  assert.equal(mod.rowSubtitle(t, { state: 'transcribing' }), 'state.transcribing')
  assert.equal(mod.rowSubtitle(t, { state: 'failed' }), 'state.failed')
  assert.equal(mod.rowSubtitle(t, { state: 'empty' }), 'state.empty')
  assert.equal(mod.rowSubtitle(t, { state: 'empty', missing_audio: ['Ana'] }), 'state.emptyUnheard')
  const now = new Date('2026-09-27T12:00:00Z')
  assert.deepEqual(mod.dateRange('today', now), { since: '2026-09-27', until: '2026-09-27' })
  assert.deepEqual(mod.dateRange('week', now), { since: '2026-09-21', until: '2026-09-27' })
  assert.deepEqual(mod.dateRange('x', now), { since: '', until: '' })
  assert.equal(mod.speakerTone('ana'), mod.speakerTone('ana'))
  assert.ok(mod.SPEAKER_TONES.some(tone => mod.speakerTone('ana') === `var(${tone})`))
  assert.deepEqual(mod.splitMatches('Ship it, ship', 'ship').map(p => p.match), [true, false, true])
  assert.deepEqual(mod.sinkView('delivered', 'auto'), { tone: 'good', key: 'delivered' })
  assert.deepEqual(mod.sinkView(undefined, 'off'), { tone: 'muted', key: 'off' })
  assert.deepEqual(mod.sinkView(undefined, 'approve'), { tone: 'warn', key: 'pending' })
  assert.equal(mod.minutesBetween('2026-09-26T15:04:00Z', '2026-09-26T15:34:00Z'), 30)
})

test('pollWhile stops on client errors and when nothing is in progress', () => {
  const poll = mod.pollWhile(d => d.busy, 1000)
  assert.equal(poll({ state: { data: { busy: true } } }), 1000)
  assert.equal(poll({ state: { data: { busy: false } } }), false)
  assert.equal(poll({ state: { data: { busy: true }, error: new Error('404: {"detail":"Plugin not found"}') } }), false)
  assert.equal(poll({ state: { data: { busy: true }, error: new Error('503: x') } }), 1000)
})

test('project channel rows round-trip and validate', () => {
  const t = (k, ...a) => `${k}${a.length ? `:${a.join(',')}` : ''}`
  const rows = mod.parseProjectChannels(['Proyecto Alfa=111111111111111111', 'broken'])
  assert.deepEqual(rows, [{ project: 'Proyecto Alfa', channel: '111111111111111111' }, { project: 'broken', channel: '' }])
  assert.deepEqual(mod.checkProjectChannels([{ project: ' Alfa ', channel: '<#123456789>' }, { project: '', channel: '' }], t), { value: ['Alfa=123456789'] })
  assert.deepEqual(mod.checkProjectChannels([{ project: 'Alfa', channel: 'general' }], t), { errors: { 0: 'projects.needChannel' } })
  assert.deepEqual(mod.checkProjectChannels([{ project: '', channel: '123456789' }], t), { errors: { 0: 'projects.needName' } })
  assert.deepEqual(mod.checkProjectChannels([{ project: 'A', channel: '123456789' }, { project: 'a', channel: '223456789' }], t), { errors: { 1: 'projects.duplicate:a' } })
})

// -- render (real React + react-dom/server, fake SDK) -------------------------------------------
const MEETING = {
  id: 'k3v7q2ab', title: 'Revisión de producto', channel_name: 'producto', guild_name: 'Acme', state: 'done', source: 'discord',
  started_at: '2026-09-26T15:04:00+00:00', ended_at: '2026-09-26T15:34:00+00:00', partial: false, people: 4, task_count: 6,
  speakers: [{ user_id: '1', name: 'Ana', is_bot: false }, { user_id: '2', name: 'Bot', is_bot: true }]
}
const BUSY = { ...MEETING, id: 'b1', title: 'Seguimiento técnico', state: 'transcribing' }
const LIST = {
  items: [MEETING, BUSY], next_cursor: 'abc', total: 3,
  facets: { total: 3, states: { recording: 0, processing: 1, done: 2, failed: 0 }, sources: { discord: 3, google_meet: 0 },
    channels: [{ id: '9', name: 'producto' }], projects: [{ name: 'Proyecto Alfa', count: 2 }] }
}
const DETAIL = {
  meeting: MEETING, projects: ['Proyecto Alfa'],
  notes: { tldr: 'Shipped the thing.', summary: 'Long summary', topics: [{ title: 'Launch', points: ['date set'] }],
    decisions: ['Ship on Friday'], open_questions: ['Who writes the post?'], action_items: [] },
  tasks: [{ id: 't1', title: 'Write the post', description: '', owner_name: 'Ana', project: 'Proyecto Alfa', status: 'approved', due: '2026-10-01',
    sinks: { kanban: { status: 'delivered', url: '' }, linear: { status: 'delivered', url: 'https://linear.app/acme/issue/A-1' } },
    discord: { channel_id: '9', url: 'https://discord.com/channels/1/2/3' } }],
  destinations: { discord: true, kanban: 'auto', linear: 'approve' },
  transcript_total: 2, job: { state: 'done', stage: 'archive', attempts: 1, failed_stage: null, error: '' },
  history: [{ kind: 'started', at: 1790000000 }, { kind: 'processed', at: 1790000900, stage: 'archive' }],
  waiting_destination: 'no channel', dm_notes: null,
  audio: { available: false, reason: 'multitrack', can_prepare: true, original: true }, command: null
}
const TRANSCRIPT = { items: [{ id: 1, t0: 1.5, t1: 3, speaker_id: '1', speaker: 'Ana', text: 'Hello team' },
  { id: 2, t0: 65, t1: 70, speaker_id: '1', speaker: 'Ana', text: 'Ship it' }], total: 2, next_cursor: null }
const STATUS = {
  worker: { state: 'recent', last_seen: 1790000000 }, counts: { running: 1, queued: 0, failed: 1 },
  jobs: [{ meeting_id: 'k3v7q2ab', title: 'Daily Sync', state: 'failed', stage: 'analyze', error: 'RuntimeError: quota', problem: 'llm' }],
  waiting_destination: [], dm_notes: [],
  commands: [{ id: 'c1', meeting_id: 'k3v7q2ab', title: 'Daily Sync', action: 'prepare_audio', stage: null, state: 'done', error: '', created_at: 1790000000, updated_at: 1790000001 }],
  settings_warnings: ['autojoin_min_humans=0: must be >= 1'],
  google: { enabled: false, connected: false, revoked: false, commands: { connect: 'hermes meeting-scribe google connect --client-secret <client.json>', status: 'hermes meeting-scribe google status', enable: 'hermes meeting-scribe config set google_meet_enabled true' } }
}
const SETTINGS = {
  schema: {
    groups: [{ key: 'capture', label: 'Captura' }, { key: 'projects', label: 'Proyectos' }, { key: 'llm', label: 'Modelos y respaldos' }],
    fields: [
      { key: 'autojoin_enabled', type: 'bool', group: 'capture', label: 'Unirse automáticamente', help: 'h', default: true, storage: 'plugin' },
      { key: 'autojoin_min_humans', type: 'int', group: 'capture', label: 'Personas para unirse', help: 'h', default: 2, minimum: 1, storage: 'plugin' },
      { key: 'transcribe_device', type: 'str', group: 'capture', label: 'Dispositivo', help: 'h', default: 'auto', choices: ['auto', 'cpu', 'cuda'], storage: 'plugin' },
      { key: 'autojoin_channels', type: 'list', group: 'capture', label: 'Solo estos canales', help: 'h', default: [], storage: 'plugin' },
      { key: 'project_channels', type: 'list', group: 'projects', label: 'Canales', help: 'h', default: [], storage: 'plugin', format: 'project_channel' },
      { key: 'llm_provider', type: 'str', group: 'llm', label: 'Proveedor', storage: 'hermes' }
    ]
  },
  values: {
    autojoin_enabled: { value: true, origin: 'default' }, autojoin_min_humans: { value: 2, origin: 'invalid' },
    transcribe_device: { value: 'cpu', origin: 'configured' }, autojoin_channels: { value: ['a', 'b'], origin: 'configured' },
    project_channels: { value: ['Proyecto Alfa=111111111111111111'], origin: 'configured' }
  },
  warnings: [],
  llm: { provider: 'openrouter', model: 'm1', base_url: '', timeout: 600, effective: { provider: 'openrouter', model: 'm1' },
    fallback_chain: [{ provider: 'nous', model: 'm2' }], sources: { provider: 'hermes-config' }, problems: [] }
}

async function render(node, fixtures, locale = 'en') {
  const { renderToStaticMarkup } = await import('react-dom/server')
  globalThis.__MS_FIXTURES = fixtures
  globalThis.__MS_LOCALE = locale
  globalThis.__MS_QUERIED = []
  return renderToStaticMarkup(node)
}

const opts = { skip: !HAS_REACT && 'React not available' }
const LIST_PATH = '/v1/meetings?limit=40'

test('page: header views, master list with status rows and the detail next to it', opts, async () => {
  const { createElement } = await import('react')
  mod.$view.set('library'); mod.$selected.set('k3v7q2ab'); mod.$tab.set('summary')
  const html = await render(createElement(mod.MeetingsPage), {
    '/v1/status': STATUS, [LIST_PATH]: LIST, '/v1/meetings/k3v7q2ab': DETAIL
  }, 'es')
  assert.match(html, /data-sdk="segmented"/)
  assert.match(html, /Ajustes/)
  assert.match(html, /class="ms-md has-selection"/)
  assert.match(html, /aria-selected="true"[^>]*data-meeting="k3v7q2ab"|data-meeting="k3v7q2ab"[^>]*/)
  assert.match(html, /4 personas · 6 tareas/)
  assert.match(html, /Transcribiendo/)
  assert.match(html, /2 de 3 reuniones/)
  assert.match(html, /Revisión de producto/)
  assert.match(html, /#producto · 30 min · 4 personas/)
  assert.match(html, /role="tab"[^>]*>Resumen/)
  assert.match(html, /Procesamiento/)
})

test('page: nothing selected shows the pick-a-meeting pane; empty library and errors', opts, async () => {
  const { createElement } = await import('react')
  mod.$view.set('library'); mod.$selected.set(null)
  let html = await render(createElement(mod.MeetingsPage), { '/v1/status': STATUS, [LIST_PATH]: LIST })
  assert.match(html, /Pick a meeting/)
  html = await render(createElement(mod.MeetingsPage), { '/v1/status': STATUS, [LIST_PATH]: { items: [], next_cursor: null, total: 0, facets: { total: 0 } } })
  assert.match(html, /No meetings yet/)
  html = await render(createElement(mod.MeetingsPage), { '/v1/status': STATUS, [LIST_PATH]: { items: [], next_cursor: null, total: 0, facets: { total: 5 } } })
  assert.match(html, /Nothing matches/)
  html = await render(createElement(mod.MeetingsPage), { '/v1/status': STATUS })
  assert.match(html, /data-sdk="skeleton"/)
  html = await render(createElement(mod.MeetingsPage), { '/v1/status': STATUS, [LIST_PATH]: new Error('500: {"detail":"boom"}') })
  assert.match(html, /Could not load this/)
  assert.match(html, /boom/)
})

test('a backend launched without the plugin gets plain guidance, no commands to type', opts, async () => {
  const { createElement } = await import('react')
  mod.$view.set('library'); mod.$selected.set(null)
  const missing = new Error(`Error invoking remote method 'hermes:api': Error: 404: {"detail":"Plugin not found"}`)
  const html = await render(createElement(mod.MeetingsPage), { '/v1/status': missing }, 'es')
  assert.match(html, /Reuniones no está disponible en esta ventana/)
  assert.match(html, /Abre Hermes con un perfil que tenga Reuniones activado/)
  assert.match(html, /no arranca un segundo bot/)
  assert.doesNotMatch(html, /plugins enable|gateway restart/)
  assert.doesNotMatch(html, /data-sdk="segmented"/)
  assert.doesNotMatch(html, /No se pudo cargar/)
})

test('summary tab: lead, decisions, questions, pending tasks and prepare-audio', opts, async () => {
  const { createElement } = await import('react')
  mod.$tab.set('summary')
  const html = await render(createElement(mod.MeetingDetail, { id: 'k3v7q2ab' }), { '/v1/meetings/k3v7q2ab': DETAIL, '/v1/status': STATUS }, 'es')
  assert.match(html, /Shipped the thing\./)
  assert.match(html, /Decisiones/)
  assert.match(html, /Ship on Friday/)
  assert.match(html, /Who writes the post\?/)
  assert.match(html, /Tareas pendientes/)
  assert.match(html, /Ana · Proyecto Alfa · 2026-10-01/)
  assert.match(html, /Preparar audio/)
  assert.match(html, /Esperando un lugar donde publicar/)
})

test('summary and processing tabs say whose audio was not captured', opts, async () => {
  const { createElement } = await import('react')
  const d = { ...DETAIL, meeting: { ...MEETING, missing_audio_names: ['Luis', 'Eve'] } }
  for (const tab of ['summary', 'processing']) {
    mod.$tab.set(tab)
    const html = await render(createElement(mod.MeetingDetail, { id: 'k3v7q2ab' }), { '/v1/meetings/k3v7q2ab': d, '/v1/status': STATUS }, 'es')
    assert.match(html, /No se pudo capturar el audio de: Luis, Eve/)
  }
  mod.$tab.set('summary')
  const html = await render(createElement(mod.MeetingDetail, { id: 'k3v7q2ab' }), { '/v1/meetings/k3v7q2ab': DETAIL, '/v1/status': STATUS })
  assert.doesNotMatch(html, /Could not capture the audio/)
})

test('summary tab plays the listening copy through the media protocol when available', opts, async () => {
  const { createElement } = await import('react')
  mod.$tab.set('summary')
  const d = { ...DETAIL, audio: { available: true, reason: 'ready', path: '/data/m/playback.ogg', original: true } }
  const html = await render(createElement(mod.MeetingDetail, { id: 'k3v7q2ab' }), { '/v1/meetings/k3v7q2ab': d, '/v1/status': STATUS })
  assert.match(html, /<audio[^>]*src="hermes-media:\/\/stream\/%2Fdata%2Fm%2Fplayback\.ogg"/)
  assert.match(html, /one track per person, is kept/)
})

test('transcript tab: speakers, clickable times when there is audio, search highlight', opts, async () => {
  const { createElement } = await import('react')
  mod.$tab.set('transcript')
  const d = { ...DETAIL, audio: { available: true, path: '/x/playback.ogg' } }
  const html = await render(createElement(mod.MeetingDetail, { id: 'k3v7q2ab' }), {
    '/v1/meetings/k3v7q2ab': d, '/v1/meetings/k3v7q2ab/transcript?limit=300': TRANSCRIPT
  })
  assert.match(html, /class="ms-speaker"[^>]*>Ana/)
  assert.match(html, />AN?</)
  assert.match(html, /aria-label="Play from 00:01"/)
  assert.match(html, /Hello team/)
  assert.match(html, /2 lines/)
})

test('tasks tab: owner, project, due and per-destination status with links', opts, async () => {
  const { createElement } = await import('react')
  mod.$tab.set('tasks')
  const html = await render(createElement(mod.MeetingDetail, { id: 'k3v7q2ab' }), { '/v1/meetings/k3v7q2ab': DETAIL }, 'es')
  assert.match(html, /Write the post/)
  assert.match(html, /Responsable/)
  assert.match(html, /2026-10-01/)
  assert.match(html, /href="https:\/\/discord\.com\/channels\/1\/2\/3"/)
  assert.match(html, /href="https:\/\/linear\.app\/acme\/issue\/A-1"/)
  assert.match(html, /Abrir el tablero/)
  assert.match(html, /Publicada/)
  assert.match(html, /Enviada/)
})

test('processing tab: plain-language problem, details, history and reprocess', opts, async () => {
  const { createElement } = await import('react')
  mod.$tab.set('processing')
  const failed = { ...DETAIL, meeting: { ...MEETING, state: 'failed' },
    job: { state: 'failed', stage: 'analyze', failed_stage: 'analyze', attempts: 3, error: 'RuntimeError: 429 quota', problem: 'llm' },
    history: [{ kind: 'started', at: 1790000000 }, { kind: 'failed', at: 1790000100, stage: 'analyze', problem: 'llm' },
      { kind: 'command', at: 1790000200, action: 'reprocess', stage: 'deliver', state: 'unknown' }] }
  const html = await render(createElement(mod.MeetingDetail, { id: 'k3v7q2ab' }), { '/v1/meetings/k3v7q2ab': failed, '/v1/status': STATUS })
  assert.match(html, /did not answer correctly/)
  assert.match(html, /Technical details/)
  assert.match(html, /RuntimeError: 429 quota/)
  assert.match(html, /Recording started/)
  assert.match(html, /Reprocess from «Publishing»/)
  assert.match(html, /Reprocess…/)
})

test('detail of a missing meeting says so', opts, async () => {
  const { createElement } = await import('react')
  const html = await render(createElement(mod.MeetingDetail, { id: 'gone' }), { '/v1/meetings/gone': new Error('404: {"detail":"not found"}') })
  assert.match(html, /no longer exists/)
})

test('status shows worker, queue problems, warnings, Google guide and diagnostics button', opts, async () => {
  const { createElement } = await import('react')
  const html = await render(createElement(mod.StatusView), { '/v1/status': STATUS })
  assert.match(html, /Online and processing/)
  assert.match(html, /did not answer correctly/)
  assert.doesNotMatch(html, /RuntimeError/)
  assert.match(html, /autojoin_min_humans=0/)
  assert.match(html, /google connect --client-secret &lt;client\.json&gt;/)
  assert.match(html, /Run diagnostics/)
  // a finished action says what was done and on which meeting, not a bare state
  assert.match(html, /Prepare the audio to listen to/)
  assert.match(html, /Daily Sync · [^<]* · Finished/)
  // a zero counter is neutral; only a non-zero one carries a colour
  assert.match(html, /ms-counter ms-counter-queued is-zero/)
  assert.match(html, /ms-counter ms-counter-failed"/)
  assert.match(html, /Not recording right now/)
})

test('status names the meeting being recorded now and a recording whose capture is gone', opts, async () => {
  const { createElement } = await import('react')
  const recording = [{ meeting_id: 'r1', title: 'Weekly planning', started_at: '2026-01-01T10:00:00+00:00', live: true },
    { meeting_id: 'r2', title: 'Old call', started_at: '2026-01-01T09:00:00+00:00', live: false }]
  const html = await render(createElement(mod.StatusView), { '/v1/status': { ...STATUS, recording } })
  assert.match(html, /Recording now — restarting the gateway would cut it/)
  assert.match(html, /Weekly planning/)
  assert.doesNotMatch(html, /Not recording right now/)
  assert.match(html, /Old call[^]*the process capturing it is gone/)
})

test('the page styles use only host tokens: no literal colours or accent-tinted surfaces', () => {
  assert.doesNotMatch(mod.CSS, /#[0-9a-fA-F]{3,8}\b/)
  assert.doesNotMatch(mod.CSS, /\b(?:hsl|rgb)a?\(/)
  assert.doesNotMatch(mod.CSS, /--ui-bg-(?:primary|card|sidebar|quinary)\b/)
  assert.match(mod.CSS, /\.ms-page\{[^}]*background:var\(--ui-surface-background\)/)
})

test('commandLabel describes each page action in plain words', () => {
  const t = (key, ...args) => ({ 'processing.actions.reprocess': `from ${args[0]}`, 'reprocess.analyze': 'Summary', 'processing.actions.prepare_audio': 'Prepare', 'processing.actions.unknown': 'Something' })[key] ?? key
  assert.equal(mod.commandLabel({ action: 'reprocess', stage: 'analyze' }, t), 'from Summary')
  assert.equal(mod.commandLabel({ action: 'prepare_audio' }, t), 'Prepare')
  assert.equal(mod.commandLabel({ action: 'mystery' }, t), 'Something')
})

test('settings: section nav, fields from the schema, origins', opts, async () => {
  const { createElement } = await import('react')
  const html = await render(createElement(mod.SettingsView), { '/v1/settings?lang=es': SETTINGS }, 'es')
  assert.match(html, /aria-current="page"[^>]*>Captura/)
  assert.match(html, /Unirse automáticamente/)
  assert.match(html, /role="switch"/)
  assert.match(html, /type="number"[^>]*min="1"/)
  assert.match(html, /No válido/)
  assert.match(html, /Personalizado/)
  assert.match(html, /Procesador \(CPU\)/)
  assert.match(html, /<textarea[^>]*>a\nb<\/textarea>/)
  assert.match(html, /for="ms-set-autojoin_min_humans"/)
})

test('project–channel editor renders one validated row per association', opts, async () => {
  const { createElement } = await import('react')
  const html = await render(createElement(mod.ProjectChannelsEditor, { value: ['Proyecto Alfa=111111111111111111', 'Proyecto Beta=222222222222222222'] }), {}, 'es')
  assert.match(html, /Canales por proyecto/)
  assert.match(html, /value="Proyecto Alfa"/)
  assert.match(html, /value="222222222222222222"/)
  assert.match(html, /Añadir proyecto/)
  assert.match(html, /aria-label="Quitar — Proyecto 2"/)
})

test('models section: primary model and ordered backups', opts, async () => {
  const { createElement } = await import('react')
  const html = await render(createElement(mod.ModelsSection, { llm: SETTINGS.llm }), {}, 'es')
  assert.match(html, /Modelo principal/)
  assert.match(html, /value="openrouter"/)
  assert.match(html, /value="nous"/)
  assert.match(html, /aria-label="Respaldo 1"/)
  assert.match(html, /En uso: openrouter \/ m1/)
})

test('a discarded recording (no audio) is its own filter and state, not a failure', opts, async () => {
  const { createElement } = await import('react')
  mod.$tab.set('summary')
  const empty = { ...MEETING, id: 'e1', title: 'Sala vacía', state: 'empty' }
  const html = await render(createElement(mod.MeetingDetail, { id: 'e1' }), {
    '/v1/meetings/e1': { ...DETAIL, meeting: empty, notes: null, tasks: [], transcript_total: 0, waiting_destination: null,
      audio: { available: false, reason: 'empty' } }
  }, 'es')
  assert.match(html, /Descartada: sin audio/)
  assert.match(html, /No se captó audio/)
})

test('a private meeting shows a lock in the list and a "private" pill in the detail', opts, async () => {
  const { createElement } = await import('react')
  mod.$view.set('library'); mod.$selected.set('k3v7q2ab'); mod.$tab.set('summary')
  const secret = { ...MEETING, private: true }
  const html = await render(createElement(mod.MeetingsPage), {
    '/v1/status': STATUS, [LIST_PATH]: { ...LIST, items: [secret, BUSY] }, '/v1/meetings/k3v7q2ab': { ...DETAIL, meeting: secret }
  }, 'es')
  assert.match(html, /class="ms-row-meta"><span>[^<]*<\/span><i data-codicon="lock"><\/i>/)
  assert.match(html, /ms-pill ms-tone-warn[^>]*title="Reunión privada: [^"]+"[^>]*>.*?Privada/)
  const plain = await render(createElement(mod.MeetingsPage), {
    '/v1/status': STATUS, [LIST_PATH]: LIST, '/v1/meetings/k3v7q2ab': DETAIL
  }, 'es')
  assert.doesNotMatch(plain, /Privada/)
})

// -- meeting rules editor (DESIGN §19.3) ---------------------------------------------------------
const CHANNELS = [
  { id: '900', name: 'Board', type: 'category', parent_id: '', parent_name: '', public: true },
  { id: '300', name: 'Leadership', type: 'voice', parent_id: '900', parent_name: 'Board', public: true },
  { id: '501', name: 'orion', type: 'text', parent_id: '', parent_name: '', public: true },
  { id: '700', name: 'board-notes', type: 'text', parent_id: '900', parent_name: 'Board', public: false },
  { id: '502', name: 'nebula', type: 'forum', parent_id: '', parent_name: '', public: true }
]

test('rule helpers: grouped targets, origin options, dm body, public warning', () => {
  const groups = mod.groupTargets(CHANNELS, 'none')
  assert.deepEqual(groups.map(g => [g.name, g.items.map(c => c.id)]), [['none', ['501', '502']], ['Board', ['700']]])
  assert.deepEqual(mod.originOptions(CHANNELS, 'voice').map(c => c.id), ['300'])
  assert.deepEqual(mod.originOptions(CHANNELS, 'category').map(c => c.id), ['900'])
  assert.deepEqual(mod.ruleBody({ kind: 'voice', origin: ' 300 ', target: '501', mode: 'dm' }), { origin_kind: 'voice', origin: '300', mode: 'dm' })
  assert.deepEqual(mod.ruleBody({ kind: 'meet', origin: 'retro-*', target: '700', mode: 'private' }), { origin_kind: 'meet', origin: 'retro-*', mode: 'private', target: '700' })
  assert.equal(mod.ruleReady({ kind: 'voice', origin: '300', target: '', mode: 'dm' }), true)
  assert.equal(mod.ruleReady({ kind: 'voice', origin: '300', target: '', mode: 'normal' }), false)
  assert.equal(mod.ruleReady({ kind: 'voice', origin: '', target: '501', mode: 'normal' }), false)
  assert.equal(mod.privateWarning('private', CHANNELS[2]), true)
  assert.equal(mod.privateWarning('private', CHANNELS[3]), false)
  assert.equal(mod.privateWarning('normal', CHANNELS[2]), false)
  assert.deepEqual(mod.RULE_MODES, ['normal', 'private', 'dm'])
})

test('the rule editor only uses SDK components and host tokens', () => {
  const body = SOURCE.slice(SOURCE.indexOf('export function RoutesEditor'), SOURCE.indexOf('export function ModelsSection'))
  assert.doesNotMatch(body, /h\('(select|input|textarea|option)'/)
  assert.doesNotMatch(body, /#[0-9a-f]{3,6}\b/i)
})

const RULES = {
  space: '', scope: 'global', catalog_seen_at: 1,
  items: [
    { position: 1, text: '300=700:private', mode: 'private', status: 'ok', sentence: 'Canal de voz «Leadership» → canal «board-notes» · Privada', target_check: { name: 'board-notes' } },
    { position: 2, text: 'category:900=501:private', mode: 'private', status: 'ok', warning: 'x', sentence: 'Categoría «Board» → canal «orion» · Privada', target_check: { name: 'orion' } },
    { position: 3, text: 'meet:retro-*=:dm', mode: 'dm', status: 'ok', sentence: 'Google Meet «retro-*» → mensajes directos a cada participante · Solo mensajes directos' },
    { position: 4, text: 'Old=gone', mode: 'normal', status: 'problem', detail: 'no text/forum/media channel named \'gone\'', sentence: 'Canal de voz «Old» → canal «gone» · Normal' }
  ]
}

test('rule editor lists readable rules with status, warnings and move/remove buttons', opts, async () => {
  const { createElement } = await import('react')
  const html = await render(createElement(mod.RoutesEditor, { lang: 'es' }), {
    '/v1/spaces': { items: [{ slug: 'main', name: 'Main' }] },
    '/v1/routes?lang=es': RULES,
    '/v1/discord/channels': { items: CHANNELS, seen_at: 1 }
  }, 'es')
  assert.match(html, /Reglas por reunión/)
  assert.match(html, /Canal de voz «Leadership» → canal «board-notes» · Privada/)
  assert.match(html, /Solo mensajes directos/)
  assert.match(html, /#orion lo ve todo el servidor/)
  assert.match(html, /no text\/forum\/media channel named/)
  assert.match(html, /aria-label="Subir — Regla 1"[^>]*disabled|disabled=""[^>]*aria-label="Subir — Regla 1"/)
  assert.match(html, /aria-label="Quitar regla — Regla 4"/)
  assert.match(html, /Añadir regla/)
  assert.ok(globalThis.__MS_QUERIED.includes('/v1/discord/channels'))
})

test('rule editor with several spaces asks for the space and scopes every query', opts, async () => {
  const { createElement } = await import('react')
  globalThis.__MS_QUERIED = []
  const html = await render(createElement(mod.RoutesEditor, { lang: 'en' }), {
    '/v1/spaces': { items: [{ slug: 'alpha', name: 'Alpha' }, { slug: 'beta', name: 'Beta' }] },
    '/v1/routes?space=alpha&lang=en': { ...RULES, items: [] },
    '/v1/discord/channels?space=alpha': { items: [], seen_at: null }
  }, 'en')
  assert.match(html, /id="ms-routes-space"/)
  assert.match(html, /No rules yet/)
  assert.ok(globalThis.__MS_QUERIED.includes('/v1/routes?space=alpha&lang=en'))
})

test('rule form: pickers from the catalog, lock on private channels, mode help and public warning', opts, async () => {
  const { createElement } = await import('react')
  const base = { channels: CHANNELS, known: true, busy: false, setDraft() {}, onAdd() {}, onCancel() {} }
  let html = await render(createElement(mod.RuleForm, { ...base, draft: { kind: 'voice', origin: '300', target: '501', mode: 'private' } }), {}, 'es')
  assert.match(html, /data-value="300"[^>]*>Leadership · Board/)
  assert.match(html, />Board</)  // targets grouped by category
  assert.match(html, /data-value="700"><i data-codicon="lock"><\/i> #board-notes/)
  assert.match(html, /data-value="501"> #orion/)
  assert.match(html, /Todo se queda en ese canal/)
  assert.match(html, /#orion lo ve todo el servidor/)
  html = await render(createElement(mod.RuleForm, { ...base, draft: { kind: 'meet', origin: 'retro-*', target: '', mode: 'dm' } }), {}, 'en')
  assert.doesNotMatch(html, /id="ms-rule-target"/)
  assert.match(html, /Nothing is posted in any channel/)
  assert.match(html, /value="retro-\*"/)
  html = await render(createElement(mod.RuleForm, { ...base, channels: [], known: false, draft: { kind: 'voice', origin: '', target: '', mode: 'normal' } }), {}, 'en')
  assert.match(html, /has not reported its channels yet/)
  assert.match(html, /<input[^>]*id="ms-rule-target"/)
})
