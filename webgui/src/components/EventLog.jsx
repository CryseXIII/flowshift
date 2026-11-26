import { useEffect, useMemo, useRef, useState } from 'react'
import * as api from '../api.js'
import ButtonIcon from './ButtonIcon.jsx'
import {
  ANY_DEVICE, KINDS, LEVELS, NO_DEVICE, RANGES,
  defaultFilter, devicesOf, filterEntries, isDefaultFilter, loadFilter, saveFilter,
} from '../eventLogFilter.js'

// Event Log (Viktor 26.11.: "einen gescheiten filter"): runtime log lines of
// this PC with level, kind and device (event_log.py, GET /api/event-log),
// filtered here by level, kind, device, text and time range. The filter is
// kept across reloads; the runtime keeps the last 1000 lines.
const MAX_LOG = 1000
const POLL_MS = 2000

const KIND_LABEL = Object.fromEntries(KINDS.map((k) => [k.id, k.label]))
const LEVEL_LABEL = Object.fromEntries(LEVELS.map((l) => [l.id, l.label]))

function fmtTime(ts) {
  const d = new Date(ts * 1000)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString()
}

function toggle(list, id) {
  return list.includes(id) ? list.filter((x) => x !== id) : [...list, id]
}

export default function EventLog() {
  const [entries, setEntries] = useState([])
  const [filter, setFilter] = useState(() => loadFilter())
  const [paused, setPaused] = useState(false)
  const [clearedUpTo, setClearedUpTo] = useState(0)
  const [error, setError] = useState(null)
  const lastIdRef = useRef(0)
  const pausedRef = useRef(false)
  const listRef = useRef(null)

  useEffect(() => { saveFilter(filter) }, [filter])
  useEffect(() => { pausedRef.current = paused }, [paused])

  useEffect(() => {
    let alive = true
    let loading = false
    const load = async () => {
      // One request at a time: two with the same `after` would add rows twice.
      if (pausedRef.current || loading) return
      loading = true
      try {
        const data = await api.getEventLog(lastIdRef.current)
        if (!alive || !data) return
        const fresh = Array.isArray(data.entries) ? data.entries : []
        if (typeof data.last_id === 'number' && data.last_id < lastIdRef.current) {
          // FlowShift restarted: its ids start again at 1; the next poll loads all.
          lastIdRef.current = 0
          setClearedUpTo(0)
          setEntries([])
          return
        }
        if (fresh.length) {
          setEntries((prev) => [...prev, ...fresh].slice(-MAX_LOG))
        }
        if (fresh.length) lastIdRef.current = fresh[fresh.length - 1].id
        setError(null)
      } catch (err) {
        if (alive) setError(err.message)
      } finally {
        loading = false
      }
    }
    load()
    const timer = window.setInterval(load, POLL_MS)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  const visible = useMemo(() => entries.filter((e) => e.id > clearedUpTo), [entries, clearedUpTo])
  const filtered = useMemo(() => filterEntries(visible, filter), [visible, filter])
  const devices = useMemo(() => {
    const names = devicesOf(visible)
    if (filter.device && filter.device !== NO_DEVICE && !names.includes(filter.device)) names.push(filter.device)
    return names
  }, [visible, filter.device])

  useEffect(() => {
    if (listRef.current && !paused) listRef.current.scrollTop = listRef.current.scrollHeight
  }, [filtered.length, paused])

  const change = (patch) => setFilter((current) => ({ ...current, ...patch }))
  const clearLog = () => setClearedUpTo(lastIdRef.current)

  return (
    <div className="event-log">
      <div className="page-title">
        <i className="fas fa-list" /> Event Log
        <span className="sub" data-testid="event-log-count">
          {filtered.length === visible.length ? `${visible.length} events` : `${filtered.length} of ${visible.length} events`}
        </span>
      </div>

      <div className="event-log-filters" role="group" aria-label="Event Log filter">
        <div className="clipboard-search event-log-search">
          <input type="search" className="input-bare" aria-label="Search events" placeholder="Search text..."
            value={filter.text} onChange={(e) => change({ text: e.target.value })} />
        </div>

        <fieldset className="event-log-chips">
          <legend>Level</legend>
          {LEVELS.map((l) => (
            <label key={l.id} className={`event-chip level-${l.id}`}>
              <input type="checkbox" checked={filter.levels.includes(l.id)}
                onChange={() => change({ levels: toggle(filter.levels, l.id) })} />
              {l.label}
            </label>
          ))}
        </fieldset>

        <fieldset className="event-log-chips">
          <legend>Kind</legend>
          {KINDS.map((k) => (
            <label key={k.id} className="event-chip">
              <input type="checkbox" checked={filter.kinds.includes(k.id)}
                onChange={() => change({ kinds: toggle(filter.kinds, k.id) })} />
              {k.label}
            </label>
          ))}
        </fieldset>

        <label className="event-log-select">
          <span>Device</span>
          <select value={filter.device} onChange={(e) => change({ device: e.target.value })} data-testid="event-log-device">
            <option value={ANY_DEVICE}>All devices</option>
            <option value={NO_DEVICE}>Not about a device</option>
            {devices.map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
        </label>

        <label className="event-log-select">
          <span>Time</span>
          <select value={filter.range} onChange={(e) => change({ range: e.target.value })} data-testid="event-log-range">
            {RANGES.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
          </select>
        </label>
        {filter.range === 'custom' && (
          <>
            <label className="event-log-select">
              <span>From</span>
              <input type="datetime-local" value={filter.from} onChange={(e) => change({ from: e.target.value })} />
            </label>
            <label className="event-log-select">
              <span>To</span>
              <input type="datetime-local" value={filter.to} onChange={(e) => change({ to: e.target.value })} />
            </label>
          </>
        )}

        <div className="event-log-actions">
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setFilter(defaultFilter())}
            disabled={isDefaultFilter(filter)} title="Show all events again">
            <ButtonIcon name="xmark" /> Reset filter
          </button>
          <button type="button" className={`btn btn-sm ${paused ? 'btn-primary' : 'btn-ghost'}`} onClick={() => setPaused(!paused)}
            title={paused ? 'Show new events again' : 'Stop adding new events while you read'}>
            {paused ? 'Resume' : 'Pause'}
          </button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={clearLog} title="Hide the events shown so far">
            <ButtonIcon name="trash" /> Clear
          </button>
        </div>
      </div>

      {error && <div className="msg-box error">Could not load the Event Log: {error}</div>}

      <div className="event-log-list" ref={listRef} data-testid="event-log-list">
        {filtered.length === 0 && (
          <div className="event-log-empty">
            {visible.length === 0 ? 'Waiting for events...' : 'No events match the filter.'}
          </div>
        )}
        {filtered.map((ev) => (
          <div key={ev.id} className={`event-row level-${ev.level}`} data-testid="event-row">
            <span className="event-time">{fmtTime(ev.ts)}</span>
            <span className={`event-level level-${ev.level}`}>{LEVEL_LABEL[ev.level] || ev.level}</span>
            <span className="event-type">{KIND_LABEL[ev.kind] || 'Other'}</span>
            <span className="event-device">{ev.device || ''}</span>
            <span className="event-data">
              {ev.text}
              {ev.detail && (
                <details>
                  <summary>Details</summary>
                  <pre>{ev.detail}</pre>
                </details>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
