// Module-resolution hooks for the Node tests of desktop/plugin.js (registered by run.mjs).
// `@hermes/plugin-sdk` → sdk-stub.mjs; `react*` → the React found in MS_REACT_DIR (a node_modules
// dir, e.g. a Hermes checkout's) or, when none is available, a tiny structural stub.
import { pathToFileURL } from 'node:url'

let reactDir = null
let fallback = null

export async function initialize(data) {
  reactDir = data?.reactDir || null
  fallback = data?.fallback || null
}

export async function resolve(specifier, context, next) {
  if (specifier === '@hermes/plugin-sdk') {
    return { url: new URL('./sdk-stub.mjs', import.meta.url).href, shortCircuit: true }
  }
  if (specifier === 'react' || specifier.startsWith('react/') || specifier.startsWith('react-dom')) {
    if (reactDir) {
      return next(specifier, { ...context, parentURL: pathToFileURL(`${reactDir}/_resolver.js`).href })
    }
    if (fallback) return { url: new URL('./react-stub.mjs', import.meta.url).href, shortCircuit: true }
  }
  return next(specifier, context)
}
