// Filters of the Event Log page (Viktor 26.11.): level, kind, device, free
// text and time range, all combinable; the choice is kept in localStorage so
// it survives a reload. Pure functions, tested in EventLog.test.jsx.
// Entries come from GET /api/logs and SSE "log" events (event_log.py):
// { seq, time, level: INFO|WARN|ERROR, kind, device, text }.

export const STORAGE_KEY = 'flowshift.eventLog.filter.v1'

export const LEVELS = [
  { id: 'info', label: 'Info', level: 'INFO' },
  { id: 'warning', label: 'Warning', level: 'WARN' },
  { id: 'error', label: 'Error', level: 'ERROR' },
]

// 'WARN' -> 'warning'; anything unknown counts as info.
export function levelId(level) {
  const name = String(level || '').toUpperCase()
  return LEVELS.find((l) => l.level === name)?.id || 'info'
}

export const KINDS = [
  { id: 'connection', label: 'Connection' },
  { id: 'edge', label: 'Edge' },
  { id: 'clipboard', label: 'Clipboard' },
  { id: 'update', label: 'Update' },
  { id: 'web_api', label: 'Web API' },
  { id: 'other', label: 'Other' },
]

export const RANGES = [
  { id: 'all', label: 'All time' },
  { id: '15m', label: 'Last 15 minutes', seconds: 15 * 60 },
  { id: '1h', label: 'Last hour', seconds: 60 * 60 },
  { id: '24h', label: 'Last 24 hours', seconds: 24 * 60 * 60 },
  { id: 'custom', label: 'From - to' },
]

// Device filter values besides a device name.
export const ANY_DEVICE = ''
export const NO_DEVICE = '__none__'

export function defaultFilter() {
  return {
    levels: LEVELS.map((l) => l.id),
    kinds: KINDS.map((k) => k.id),
    device: ANY_DEVICE,
    text: '',
    range: 'all',
    from: '',
    to: '',
    internal: false,
  }
}

function onlyKnown(values, known) {
  if (!Array.isArray(values)) return null
  const ids = new Set(known.map((k) => k.id))
  return values.filter((v) => ids.has(v))
}

// Stored JSON -> complete filter; anything unknown or broken falls back to the default.
export function normalizeFilter(raw) {
  const base = defaultFilter()
  if (!raw || typeof raw !== 'object') return base
  const levels = onlyKnown(raw.levels, LEVELS)
  const kinds = onlyKnown(raw.kinds, KINDS)
  return {
    levels: levels ?? base.levels,
    kinds: kinds ?? base.kinds,
    device: typeof raw.device === 'string' ? raw.device : base.device,
    text: typeof raw.text === 'string' ? raw.text : base.text,
    range: RANGES.some((r) => r.id === raw.range) ? raw.range : base.range,
    from: typeof raw.from === 'string' ? raw.from : '',
    to: typeof raw.to === 'string' ? raw.to : '',
    internal: raw.internal === true,
  }
}

export function loadFilter(storage = globalThis.localStorage) {
  try {
    return normalizeFilter(JSON.parse(storage?.getItem(STORAGE_KEY) || 'null'))
  } catch {
    return defaultFilter()
  }
}

export function saveFilter(filter, storage = globalThis.localStorage) {
  try {
    storage?.setItem(STORAGE_KEY, JSON.stringify(filter))
  } catch {
    // Private mode or full storage: the filter still works until reload.
  }
}

export function isDefaultFilter(filter) {
  return JSON.stringify(normalizeFilter(filter)) === JSON.stringify(defaultFilter())
}

// datetime-local value ("2026-11-26T10:59") -> epoch seconds, local time.
function localSeconds(value) {
  if (!value) return null
  const ms = new Date(value).getTime()
  return Number.isNaN(ms) ? null : ms / 1000
}

export function timeBounds(filter, nowSeconds) {
  const range = RANGES.find((r) => r.id === filter.range)
  if (range?.seconds) return { from: nowSeconds - range.seconds, to: null }
  if (filter.range === 'custom') return { from: localSeconds(filter.from), to: localSeconds(filter.to) }
  return { from: null, to: null }
}

// Internal events (status, clipboard; no level, kind or device) only with
// "Also internal events" and then only through the text and time filters.
export function filterEntries(entries, filter, nowSeconds = Date.now() / 1000) {
  const f = normalizeFilter(filter)
  const levels = new Set(f.levels)
  const kinds = new Set(f.kinds)
  const words = f.text.toLowerCase().split(/\s+/).filter(Boolean)
  const { from, to } = timeBounds(f, nowSeconds)
  return entries.filter((entry) => {
    if (entry.event) {
      if (!f.internal) return false
    } else {
      if (!levels.has(levelId(entry.level))) return false
      if (!kinds.has(KINDS.some((k) => k.id === entry.kind) ? entry.kind : 'other')) return false
      if (f.device === NO_DEVICE ? Boolean(entry.device) : f.device && entry.device !== f.device) return false
    }
    if (from !== null && entry.time < from) return false
    if (to !== null && entry.time > to) return false
    if (words.length) {
      const haystack = (entry.event
        ? JSON.stringify(entry.event)
        : `${entry.level} ${entry.text} ${entry.explain || ''} ${entry.device || ''}`).toLowerCase()
      if (!words.every((w) => haystack.includes(w))) return false
    }
    return true
  })
}

export function devicesOf(entries) {
  return [...new Set(entries.map((e) => e.device).filter(Boolean))].sort((a, b) => a.localeCompare(b))
}
