// Minimal stand-in for `@hermes/plugin-sdk`, enough to import desktop/plugin.js in Node and render
// it with react-dom/server. Data hooks answer from `globalThis.__MS_FIXTURES` (path → data | Error).
import { createElement } from 'react'

const el = tag => ({ children, className, ...props }) => createElement(tag, { className, 'data-sdk': tag, ...clean(props) }, children)
function clean(props) {
  const out = {}
  for (const [k, v] of Object.entries(props)) {
    if (typeof v === 'function' && !k.startsWith('on')) continue
    if (['variant', 'size', 'loading', 'hints', 'tone', 'checked', 'onCheckedChange', 'onConfirm', 'onClose', 'open',
      'confirmLabel', 'busyLabel', 'cancelLabel', 'description', 'title', 'placeholder', 'value', 'onChange', 'name'].includes(k)) continue
    out[k] = v
  }
  return out
}

export function atom(initial) {
  let value = initial
  const listeners = new Set()
  return {
    get: () => value,
    set: next => { value = next; listeners.forEach(l => l(next)) },
    subscribe: l => { listeners.add(l); l(value); return () => listeners.delete(l) },
    listen: l => { listeners.add(l); return () => listeners.delete(l) }
  }
}

export const host = {
  state: { connectionId: atom(null), profile: atom('default') },
  navigate: path => { globalThis.__MS_NAVIGATED = path }
}

export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const PALETTE_AREA = 'palette'

export const useValue = a => a.get()
export const useI18n = () => ({ locale: globalThis.__MS_LOCALE || 'en' })

function resolve(bundle, key) {
  return key.split('.').reduce((node, seg) => (node && typeof node === 'object' ? node[seg] : undefined), bundle)
}
export function translator(locale) {
  return (key, ...args) => {
    for (const l of [locale, 'en']) {
      const v = resolve(globalThis.__MS_LOCALES?.[l], key)
      if (typeof v === 'string') return v
      if (typeof v === 'function') return v(...args)
    }
    return key
  }
}
export const usePluginI18n = () => translator(globalThis.__MS_LOCALE || 'en')

export function useQuery({ queryKey, enabled }) {
  const path = queryKey[2]
  if (enabled === false) return { isLoading: false, isError: false, data: undefined, refetch() {}, isFetching: false }
  const fx = globalThis.__MS_FIXTURES || {}
  globalThis.__MS_QUERIED = [...(globalThis.__MS_QUERIED || []), path]
  if (!(path in fx)) return { isLoading: true, isError: false, data: undefined, refetch() {}, isFetching: true }
  const value = fx[path]
  if (value instanceof Error) return { isLoading: false, isError: true, error: value, data: undefined, refetch() {}, isFetching: false }
  return { isLoading: false, isError: false, data: value, refetch() {}, isFetching: false }
}
export const useQueryClient = () => ({ invalidateQueries() {} })

export const Badge = el('span')
export const Button = el('button')
export const Codicon = ({ name }) => createElement('i', { 'data-codicon': name })
export const StatusDot = el('span')
export const Switch = ({ id, checked }) => createElement('button', { id, role: 'switch', 'aria-checked': Boolean(checked) })
export const SegmentedControl = ({ options, value }) => createElement('div', { 'data-sdk': 'segmented', 'data-value': value }, options.map(o => createElement('button', { key: o.id, 'aria-pressed': o.id === value }, o.label)))
export const Select = ({ children }) => createElement('div', { 'data-sdk': 'select' }, children)
export const SelectTrigger = ({ id, children }) => createElement('button', { id, role: 'combobox' }, children)
export const SelectValue = () => null
export const SelectContent = ({ children }) => createElement('div', { role: 'listbox' }, children)
export const SelectItem = ({ value, children }) => createElement('div', { role: 'option', 'data-value': value }, children)
export const Input = ({ value, onChange, onKeyDown, prefix, suffix, containerClassName, ...rest }) => createElement('input', { ...rest, defaultValue: value })
export const Textarea = props => createElement('textarea', { id: props.id, defaultValue: props.value })
export const SearchField = props => createElement('input', { 'aria-label': props['aria-label'], placeholder: props.placeholder, defaultValue: props.value })
export const Skeleton = ({ className }) => createElement('span', { 'data-sdk': 'skeleton', className })
export const Tabs = ({ value, children, className }) => createElement('div', { 'data-sdk': 'tabs', 'data-value': value, className }, children)
export const TabsList = ({ children, className, ...rest }) => createElement('div', { role: 'tablist', className, 'aria-label': rest['aria-label'] }, children)
export const TabsTrigger = ({ value, children, className }) => createElement('button', { role: 'tab', 'data-value': value, className }, children)
export const ConfirmDialog = ({ open, title, children }) => (open ? createElement('div', { role: 'dialog' }, title, children) : null)
