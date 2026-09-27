// Entry point: `node tests/desktop/run.mjs` (Node >= 20.6).
// React for the render tests comes from MS_REACT_DIR, else a Hermes checkout's node_modules
// ($HERMES_SRC or ~/.hermes/hermes-agent); without it only the structural tests run.
import { existsSync } from 'node:fs'
import { register } from 'node:module'
import { homedir } from 'node:os'
import { join } from 'node:path'

const candidates = [
  process.env.MS_REACT_DIR,
  process.env.HERMES_SRC && join(process.env.HERMES_SRC, 'node_modules'),
  join(homedir(), '.hermes', 'hermes-agent', 'node_modules')
].filter(Boolean)
const reactDir = candidates.find(dir => existsSync(join(dir, 'react', 'package.json')) && existsSync(join(dir, 'react-dom', 'server.js'))) || null

register('./hooks.mjs', import.meta.url, { data: { reactDir, fallback: !reactDir } })
process.env.MS_HAS_REACT = reactDir ? '1' : ''
if (!reactDir) console.log('# no React found: render tests skipped (set MS_REACT_DIR to a node_modules with react + react-dom)')
await import('./plugin.test.mjs')
