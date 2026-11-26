import { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import * as api from '../api.js'
import { explainLogLine } from '../logExplain.js'
import ButtonIcon from './ButtonIcon.jsx'
import {
  ANY_DEVICE, KINDS, LEVELS, NO_DEVICE, RANGES,
  defaultFilter, devicesOf, filterEntries, isDefaultFilter, levelId, loadFilter, saveFilter,
} from '../eventLogFilter.js'

// The program keeps the last 500 log lines (INFO, WARN, ERROR) with kind and
// device (event_log.py); the page keeps as many, plus the last other live
// events (status, clipboard). Viktor 26.11.: "einen gescheiten filter":
// level, kind, device, text and time range, combinable, kept across reloads.
const MAX_LOG = 500
const MAX_EVENTS = 200

const KIND_LABEL = Object.fromEntries(KINDS.map((k) => [k.id, k.label]))

function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`
}

function toggle(list, id) {
  return list.includes(id) ? list.filter((x) => x !== id) : [...list, id]
}

function logRow(entry) {
  return {
    key: `log-${entry.seq}`,
    seq: entry.seq,
    level: String(entry.level || 'INFO').toUpperCase(),
    kind: String(entry.kind || 'other'),
    device: String(entry.device || ''),
    time: Number(entry.time),
    text: String(entry.text || ''),
    explain: explainLogLine(entry.text),
  }
}

export default function EventLog() {
  const [logs, setLogs] = useState([])
  const [events, setEvents] = useState([])
  const [paused, setPaused] = useState(false)
  const [filter, setFilter] = useState(() => loadFilter())
  const [folder, setFolder] = useState({ log_folder: '', log_file: 'flowshift.log' })
  const [folderMsg, setFolderMsg] = useState('')
  const [loadError, setLoadError] = useState('')
  const listRef = useRef(null)
  const lastSeq = useRef(0)
  const eventCount = useRef(0)

  useEffect(() => { saveFilter(filter) }, [filter])

  const addLogs = useCallback((entries) => {
    const fresh = entries.filter((e) => Number(e.seq) > lastSeq.current)
    if (!fresh.length) return
    lastSeq.current = Math.max(...fresh.map((e) => Number(e.seq)))
    setLogs((prev) => {
      const next = [...prev, ...fresh.map(logRow)]
      return next.length > MAX_LOG ? next.slice(-MAX_LOG) : next
    })
  }, [])

  // Lines from before the page was opened, and what was missed while paused
  // or while the live connection was down.
  const loadLogs = useCallback(() => {
    api.getLogs(lastSeq.current).then((res) => {
      setLoadError('')
      setFolder({ log_folder: res.log_folder || '', log_file: res.log_file || 'flowshift.log' })
      addLogs(Array.isArray(res.entries) ? res.entries : [])
    }).catch((e) => setLoadError(e.message || 'FlowShift is not reachable'))
  }, [addLogs])

  useEffect(() => {
    if (paused) return undefined
    loadLogs()
    const unsub = api.subscribeSSE((ev) => {
      if (ev.type === 'log') {
        addLogs([ev])
        return
      }
      eventCount.current += 1
      const row = { key: `ev-${eventCount.current}`, level: 'EVENT', time: Date.now() / 1000, event: ev }
      setEvents((prev) => {
        const next = [...prev, row]
        return next.length > MAX_EVENTS ? next.slice(-MAX_EVENTS) : next
      })
    }, loadLogs)
    return unsub
  }, [paused, loadLogs, addLogs])

  const rows = useMemo(() => (filter.internal
    ? [...logs, ...events].sort((a, b) => a.time - b.time)
    : logs), [logs, events, filter.internal])
  const filtered = useMemo(() => filterEntries(rows, filter), [rows, filter])
  const devices = useMemo(() => {
    const names = devicesOf(logs)
    if (filter.device && filter.device !== NO_DEVICE && !names.includes(filter.device)) names.push(filter.device)
    return names
  }, [logs, filter.device])

  useEffect(() => {
    if (listRef.current && !paused) {
      listRef.current.scrollTop = listRef.current.scrollHeight
    }
  }, [filtered.length, paused])

  const warnCount = logs.filter((r) => r.level === 'WARN').length
  const errorCount = logs.filter((r) => r.level === 'ERROR').length

  const change = (patch) => setFilter((current) => ({ ...current, ...patch }))

  const clearLog = () => {
    setLogs([])
    setEvents([])
  }

  const openFolder = () => {
    setFolderMsg('')
    api.openLogFolder().then((res) => {
      if (res.log_folder) setFolder((f) => ({ ...f, log_folder: res.log_folder }))
      setFolderMsg(res.opened
        ? 'The log folder was opened in Explorer.'
        : 'The folder could not be opened from here. Open the path shown above in Explorer.')
    }).catch((e) => setFolderMsg(`The folder could not be opened: ${e.message}`))
  }

  const fmtTime = (seconds) => {
    const d = new Date(seconds * 1000)
    return Number.isNaN(d.getTime()) ? '' : d.toLocaleString()
  }
  const logPath = folder.log_folder
    ? `${folder.log_folder}${/[\\/]$/.test(folder.log_folder) ? '' : '\\'}${folder.log_file}`
    : ''

  return (
    <div className="event-log">
      <div className="page-title">
        <i className="fas fa-list" /> Event Log
        <span className="sub">
          {`${logs.length} entries · ${plural(warnCount, 'warning')} · ${plural(errorCount, 'error')}`}
        </span>
        <span className="sub" data-testid="event-log-count">
          {filtered.length === rows.length ? '' : `${filtered.length} of ${rows.length} shown`}
        </span>
      </div>

      <div className="event-log-folder" data-testid="log-folder">
        {logPath ? <>Full log file: <code>{logPath}</code></> : 'Full log file: flowshift.log in the FlowShift data folder'}
        {' '}
        <button type="button" className="btn btn-ghost btn-sm" onClick={openFolder}>
          <i className="fas fa-folder-open" /> Open log folder
        </button>
        {folderMsg && <div role="status">{folderMsg}</div>}
      </div>

      <div className="event-log-filters" role="group" aria-label="Event Log filter">
        <div className="clipboard-search event-log-search">
          <i className="fas fa-search" style={{ color: 'var(--text-muted)' }} />
          <input type="search" className="input-bare" aria-label="Search events" placeholder="Search text…"
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

        <label className="event-chip">
          <input type="checkbox" checked={filter.internal} onChange={() => change({ internal: !filter.internal })} />
          Also internal events
        </label>

        <div className="event-log-actions">
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setFilter(defaultFilter())}
            disabled={isDefaultFilter(filter)} title="Show all entries again">
            <ButtonIcon name="xmark" /> Reset filter
          </button>
          <button type="button" className={`btn btn-sm ${paused ? 'btn-primary' : 'btn-ghost'}`} onClick={() => setPaused(!paused)}
            title={paused ? 'Show new entries again' : 'Stop adding new entries while you read'}>
            {paused ? 'Resume' : 'Pause'}
          </button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={clearLog} title="Hide the entries shown so far">
            <ButtonIcon name="trash" /> Clear
          </button>
        </div>
      </div>

      {loadError && <div className="event-log-folder" role="alert">Earlier entries could not be loaded: {loadError}</div>}

      <div className="event-log-list" ref={listRef} data-testid="event-log-list">
        {filtered.length === 0 && (
          <div className="event-log-empty">
            {rows.length === 0 ? 'No entries yet. Warnings and errors appear here as soon as they happen.' : 'No entries match the filter.'}
          </div>
        )}
        {filtered.map((r) => (r.event ? (
          <div key={r.key} className="event-row">
            <span className="event-time">{fmtTime(r.time)}</span>
            <span className="event-type">{r.event.type || '?'}</span>
            <span className="event-data">{JSON.stringify(r.event)}</span>
          </div>
        ) : (
          <div key={r.key} className={`event-row event-row--${r.level.toLowerCase()} level-${levelId(r.level)}`}
            data-testid="log-row" data-level={r.level}>
            <span className="event-time">{fmtTime(r.time)}</span>
            <span className="event-level">{r.level}</span>
            <span className="event-type">{KIND_LABEL[r.kind] || 'Other'}</span>
            <span className="event-device">{r.device}</span>
            <div className="event-body">
              {r.explain && <div className="event-explain">{r.explain}</div>}
              <div className="event-data">{r.text}</div>
            </div>
          </div>
        )))}
      </div>
    </div>
  )
}
