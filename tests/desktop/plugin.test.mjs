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
  assert.equal(mod.failureKey(new Error('404: {"detail":"Not Found"}'), false), 'error.disabled')
  assert.equal(mod.failureKey(new Error('404: {"detail":"not found"}'), true), 'error.notFound')
  assert.equal(mod.failureKey(new Error('500: x'), true), 'error.generic')
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

// -- render (real React + react-dom/server, fake SDK) -------------------------------------------
const MEETING = {
  id: 'k3v7q2ab', title: 'Daily Sync', channel_name: 'daily', guild_name: 'Acme', state: 'done', source: 'discord',
  started_at: '2026-09-26T15:04:00+00:00', ended_at: '2026-09-26T15:34:00+00:00', partial: false, project: 'Alpha',
  speakers: [{ user_id: '1', name: 'Ana', is_bot: false }, { user_id: '2', name: 'Bot', is_bot: true }]
}
const LIST = {
  items: [MEETING], next_cursor: 'abc',
  facets: { total: 3, states: { recording: 0, processing: 1, done: 2, failed: 0 }, sources: { discord: 3, google_meet: 0 } }
}
const DETAIL = {
  meeting: MEETING,
  notes: { tldr: 'Shipped the thing.', summary: 'Long summary', topics: [{ title: 'Launch', points: ['date set'] }],
    decisions: ['Ship on Friday'], open_questions: ['Who writes the post?'], action_items: [] },
  tasks: [{ id: 't1', title: 'Write the post', description: '', owner_name: 'Ana', project: 'Alpha', status: 'approved', due: null,
    sinks: { kanban: { status: 'delivered', url: 'https://kanban.example/t1' }, linear: { status: 'pending', url: '' } },
    discord: { channel_id: '9', url: 'https://discord.com/channels/1/2/3' } }],
  transcript_total: 2, job: { state: 'done', stage: 'archive', attempts: 0, failed_stage: null, error: '' },
  waiting_destination: 'no channel', dm_notes: null,
  audio: { available: false, reason: 'multitrack' }, command: null
}
const TRANSCRIPT = { items: [{ id: 1, t0: 1.5, t1: 3, speaker_id: '1', speaker: 'Ana', text: 'Hello team' },
  { id: 2, t0: 65, t1: 70, speaker_id: '1', speaker: 'Ana', text: 'Ship it' }], total: 2, next_cursor: null }
const STATUS = {
  worker: { state: 'recent', last_seen: 1790000000 }, counts: { running: 1, queued: 0, failed: 1 },
  jobs: [{ meeting_id: 'k3v7q2ab', title: 'Daily Sync', state: 'failed', stage: 'analyze', error: 'RuntimeError: quota' }],
  waiting_destination: [], dm_notes: [], commands: [], settings_warnings: ['autojoin_min_humans=0: must be >= 1'],
  google: { enabled: false, connected: false, revoked: false, commands: { connect: 'hermes meeting-scribe google connect --client-secret <client.json>', status: 'hermes meeting-scribe google status', enable: 'hermes meeting-scribe config set google_meet_enabled true' } }
}
const SETTINGS = {
  schema: {
    groups: [{ key: 'capture', label: 'Captura' }, { key: 'llm', label: 'Modelos y respaldos' }],
    fields: [
      { key: 'autojoin_enabled', type: 'bool', group: 'capture', label: 'Unirse automáticamente', help: 'h', default: true, storage: 'plugin' },
      { key: 'autojoin_min_humans', type: 'int', group: 'capture', label: 'Personas para unirse', help: 'h', default: 2, minimum: 1, storage: 'plugin' },
      { key: 'transcribe_device', type: 'str', group: 'capture', label: 'Dispositivo', help: 'h', default: 'auto', choices: ['auto', 'cpu', 'cuda'], storage: 'plugin' },
      { key: 'autojoin_channels', type: 'list', group: 'capture', label: 'Solo estos canales', help: 'h', default: [], storage: 'plugin' },
      { key: 'llm_provider', type: 'str', group: 'llm', label: 'Proveedor', storage: 'hermes' }
    ]
  },
  values: {
    autojoin_enabled: { value: true, origin: 'default' }, autojoin_min_humans: { value: 2, origin: 'invalid' },
    transcribe_device: { value: 'cpu', origin: 'configured' }, autojoin_channels: { value: ['a', 'b'], origin: 'configured' }
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

test('page renders the library with rows, filters and pager', opts, async () => {
  const { createElement } = await import('react')
  mod.$tab.set('library'); mod.$selected.set(null)
  const html = await render(createElement(mod.MeetingsPage), { '/v1/meetings?limit=30': LIST })
  assert.match(html, /role="tablist"/)
  assert.match(html, /Daily Sync/)
  assert.match(html, /Google Meet \(0\)/)
  assert.match(html, /Older/)
  assert.match(html, /1 shown · 3 in total/)
})

test('library empty, loading and error states', opts, async () => {
  const { createElement } = await import('react')
  mod.$tab.set('library'); mod.$selected.set(null)
  let html = await render(createElement(mod.MeetingsPage), { '/v1/meetings?limit=30': { items: [], next_cursor: null, facets: LIST.facets } })
  assert.match(html, /No meetings yet/)
  html = await render(createElement(mod.MeetingsPage), {})
  assert.match(html, /Loading/)
  html = await render(createElement(mod.MeetingsPage), { '/v1/meetings?limit=30': new Error('404: {"detail":"Not Found"}') })
  assert.match(html, /not reachable/)
})

test('detail renders notes, tasks with destinations, audio reason and transcript', opts, async () => {
  const { createElement } = await import('react')
  const html = await render(createElement(mod.DetailView, { id: 'k3v7q2ab' }), {
    '/v1/meetings/k3v7q2ab': DETAIL, '/v1/meetings/k3v7q2ab/transcript?limit=200': TRANSCRIPT, '/v1/status': STATUS
  }, 'es')
  assert.match(html, /Shipped the thing\./)
  assert.match(html, /Ship on Friday/)
  assert.match(html, /Who writes the post\?/)
  assert.match(html, /Write the post/)
  assert.match(html, /href="https:\/\/kanban\.example\/t1"/)
  assert.match(html, /Enviada/)
  assert.match(html, /una pista por persona/)
  assert.match(html, /Hello team/)
  assert.match(html, /01:05/)
  assert.match(html, /Esperando un lugar donde publicar/)
  assert.match(html, /Reprocesar…/)
  assert.doesNotMatch(html, /Bot<\/dd>/)
})

test('detail of a missing meeting says so', opts, async () => {
  const { createElement } = await import('react')
  const html = await render(createElement(mod.DetailView, { id: 'gone' }), { '/v1/meetings/gone': new Error('404: {"detail":"not found"}') })
  assert.match(html, /no longer exists/)
})

test('status shows worker, queue, warnings, Google guide and diagnostics button', opts, async () => {
  const { createElement } = await import('react')
  const html = await render(createElement(mod.StatusView), { '/v1/status': STATUS })
  assert.match(html, /online and processing/)
  assert.match(html, /RuntimeError: quota/)
  assert.match(html, /autojoin_min_humans=0/)
  assert.match(html, /google connect --client-secret &lt;client\.json&gt;/)
  assert.match(html, /Run diagnostics/)
})

test('settings form is generated from the schema with origins and models', opts, async () => {
  const { createElement } = await import('react')
  const html = await render(createElement(mod.SettingsView), { '/v1/settings?lang=es': SETTINGS }, 'es')
  assert.match(html, /Unirse automáticamente/)
  assert.match(html, /role="switch"/)
  assert.match(html, /type="number"[^>]*min="1"/)
  assert.match(html, /No válido, se usa el predeterminado/)
  assert.match(html, /Personalizado/)
  assert.match(html, /Procesador \(CPU\)/)
  assert.match(html, /<textarea[^>]*>a\nb<\/textarea>/)
  assert.match(html, /Respaldo 1/)
  assert.match(html, /value="nous"/)
  assert.doesNotMatch(html, /ms-set-llm_provider/)
  assert.match(html, /for="ms-set-autojoin_min_humans"/)
})
