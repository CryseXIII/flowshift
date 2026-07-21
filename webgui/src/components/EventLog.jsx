import { useState, useEffect, useRef } from 'react'
import * as api from '../api.js'

const MAX_LOG = 500

export default function EventLog() {
  const [events, setEvents] = useState([])
  const [paused, setPaused] = useState(false)
  const [filter, setFilter] = useState('')
  const listRef = useRef(null)

  useEffect(() => {
    const unsub = api.subscribeSSE((ev) => {
      if (!paused) {
        setEvents((prev) => {
          const next = [...prev, { ...ev, _time: Date.now() }]
          return next.length > MAX_LOG ? next.slice(-MAX_LOG) : next
        })
      }
    })
    return unsub
  }, [paused])

  useEffect(() => {
    if (listRef.current && !paused) {
      listRef.current.scrollTop = listRef.current.scrollHeight
    }
  }, [events, paused])

  const filtered = filter
    ? events.filter((ev) => JSON.stringify(ev).toLowerCase().includes(filter.toLowerCase()))
    : events

  const clearLog = () => setEvents([])

  const fmtTime = (ts) => {
    const d = new Date(ts)
    return d.toLocaleTimeString()
  }

  return (
    <div className="event-log">
      <div className="page-title">
        <i className="fas fa-list" /> Event Log
        <span className="sub">{events.length} events</span>
      </div>

      <div style={{ display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap', alignItems: 'center' }}>
        <div className="clipboard-search" style={{ flex: 1, minWidth: 160 }}>
          <i className="fas fa-search" style={{ color: 'var(--text-muted)' }} />
          <input type="text" placeholder="Filter events…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        </div>
        <button className={`btn btn-sm ${paused ? 'btn-primary' : 'btn-ghost'}`} onClick={() => setPaused(!paused)}>
          <i className={`fas ${paused ? 'fa-play' : 'fa-pause'}`} /> {paused ? 'Resume' : 'Pause'}
        </button>
        <button className="btn btn-ghost btn-sm" onClick={clearLog}>
          <i className="fas fa-trash-can" /> Clear
        </button>
      </div>

      <div className="event-log-list" ref={listRef}>
        {filtered.length === 0 && (
          <div style={{ padding: 20, textAlign: 'center', color: 'var(--text-muted)', fontSize: '.85rem' }}>
            {events.length === 0 ? 'Waiting for events…' : 'No events match filter.'}
          </div>
        )}
        {filtered.map((ev, i) => (
          <div key={i} className="event-row">
            <span className="event-time">{fmtTime(ev._time)}</span>
            <span className="event-type">{ev.type || '?'}</span>
            <span className="event-data">{JSON.stringify(omitMeta(ev))}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function omitMeta(ev) {
  const { _time, ...rest } = ev
  return rest
}
