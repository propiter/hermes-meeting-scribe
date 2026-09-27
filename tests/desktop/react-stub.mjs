// Structural stand-in for `react` / `react/jsx-runtime` when no real React is available: enough to
// import desktop/plugin.js and exercise register(); render tests are skipped in that mode.
export const Fragment = Symbol.for('react.fragment')
export const jsx = (type, props, key) => ({ type, props, key })
export const jsxs = jsx
export const createElement = (type, props, ...children) => ({ type, props: { ...props, children } })
export const useState = v => [typeof v === 'function' ? v() : v, () => {}]
export const useEffect = () => {}
export const useMemo = f => f()
export const useRef = v => ({ current: v })
export default { Fragment, createElement, useState, useEffect, useMemo, useRef }
