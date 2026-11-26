import { useState, useEffect, useRef, useCallback } from 'react'
import * as api from '../api.js'
import { explainLogLine } from '../logExplain.js'

// The program keeps the last 500 log lines (INFO, WARN, ERROR); the page
// keeps as many, plus the last other live events (status, clipboard).
const MAX_LOG = 500
const MAX_EVENTS = 200

const LEVEL_FILTERS = [
  { value: 'info', label: 'Info, warnings and errors', levels: ['INFO', 'WARN', 'ERROR'] },
  { value: 'warn', label: 'Warnings and errors', levels: ['WARN', 'ERROR'] },
  { value: 'error', label: 'Errors only', levels: ['ERROR'] },
  { value: 'all', label: 'Everything (also internal events)', levels: null },
]

function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`
}

function logRow(entry) {
  return {
    key: `log-${entry.seq}`,
    seq: entry.seq,
    level: String(entry.level || 'INFO').toUpperCase(),
    time: Number(entry.time) * 1000,
    text: String(entry.text || ''),
    explain: explainLogLine(entry.text),
  }
}

export default function EventLog() {
  const [logs, setLogs] = useState([])
  const [events, setEvents] = useState([])
  const [paused, setPaused] = useState(false)
  const [filter, setFilter] = useState('')
  const [level, setLevel] = useState('info')
  const [folder, setFolder] = useState({ log_folder: '', log_file: 'flowshift.log' })
  const [folderMsg, setFolderMsg] = useState('')
  const [loadError, setLoadError] = useState('')
  const listRef = useRef(null)
  const lastSeq = useRef(0)
  const eventCount = useRef(0)

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
      const row = { key: `ev-${eventCount.current}`, level: 'EVENT', time: Date.now(), event: ev }
      setEvents((prev) => {
        const next = [...prev, row]
        return next.length > MAX_EVENTS ? next.slice(-MAX_EVENTS) : next
      })
    }, loadLogs)
    return unsub
  }, [paused, loadLogs, addLogs])

  const levels = LEVEL_FILTERS.find((f) => f.value === level)?.levels
  const rows = (levels ? logs.filter((r) => levels.includes(r.level)) : [...logs, ...events]
    .sort((a, b) => a.time - b.time))
  const needle = filter.toLowerCase()
  const filtered = needle
    ? rows.filter((r) => (r.event ? JSON.stringify(r.event) : `${r.level} ${r.text} ${r.explain || ''}`)
      .toLowerCase().includes(needle))
    : rows

  useEffect(() => {
    if (listRef.current && !paused) {
      listRef.current.scrollTop = listRef.current.scrollHeight
    }
  }, [filtered.length, paused])

  const warnCount = logs.filter((r) => r.level === 'WARN').length
  const errorCount = logs.filter((r) => r.level === 'ERROR').length

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

  const fmtTime = (ts) => new Date(ts).toLocaleTimeString()
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
      </div>

      <div className="event-log-folder" data-testid="log-folder">
        {logPath ? <>Full log file: <code>{logPath}</code></> : 'Full log file: flowshift.log in the FlowShift data folder'}
        {' '}
        <button className="btn btn-ghost btn-sm" onClick={openFolder}>
          <i className="fas fa-folder-open" /> Open log folder
        </button>
        {folderMsg && <div role="status">{folderMsg}</div>}
      </div>

      <div style={{ display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap', alignItems: 'center' }}>
        <div className="clipboard-search" style={{ flex: 1, minWidth: 160 }}>
          <i className="fas fa-search" style={{ color: 'var(--text-muted)' }} />
          <input type="text" className="input-bare" aria-label="Filter events" placeholder="Filter events…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        </div>
        <select className="input" aria-label="Show level" value={level} onChange={(e) => setLevel(e.target.value)}>
          {LEVEL_FILTERS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
        </select>
        <button className={`btn btn-sm ${paused ? 'btn-primary' : 'btn-ghost'}`} onClick={() => setPaused(!paused)}>
          <i className={`fas ${paused ? 'fa-play' : 'fa-pause'}`} /> {paused ? 'Resume' : 'Pause'}
        </button>
        <button className="btn btn-ghost btn-sm" onClick={clearLog}>
          <i className="fas fa-trash-can" /> Clear
        </button>
      </div>

      {loadError && <div className="event-log-folder" role="alert">Earlier entries could not be loaded: {loadError}</div>}

      <div className="event-log-list" ref={listRef}>
        {filtered.length === 0 && (
          <div style={{ padding: 20, textAlign: 'center', color: 'var(--text-muted)', fontSize: '.85rem' }}>
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
          <div key={r.key} className={`event-row event-row--${r.level.toLowerCase()}`} data-testid="log-row" data-level={r.level}>
            <span className="event-time">{fmtTime(r.time)}</span>
            <span className="event-level">{r.level}</span>
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
