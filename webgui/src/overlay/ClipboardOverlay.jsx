import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import * as api from '../api.js'
import { fmtSize, mergeTransferProgress, progressLine } from '../clipboardFormat.js'

const KIND_LABEL = {
  text: 'Text', html: 'HTML', image: 'Image', gif: 'GIF', file: 'File',
  file_batch: 'Files', audio: 'Audio', binary: 'Binary',
}
const ITEM_POLL_MS = 3000
const PROGRESS_POLL_MS = 700

function normalizeProfiles(data) {
  const list = Array.isArray(data?.profiles) ? data.profiles : []
  return list
    .filter((p) => p && typeof p.identity === 'string' && p.identity)
    .map((p) => ({ identity: p.identity, label: String(p.label || p.identity), connected: p.connected === true }))
}

// Static clipboard panel spawned at the cursor. Polls asynchronously, never
// remounts the list container, and restores the scroll offset after every
// data refresh so a fixed-height list keeps its position.
export default function ClipboardOverlay({ data, onClose }) {
  const profiles = useMemo(() => normalizeProfiles(data), [data])
  const [profile, setProfile] = useState(() => (
    typeof data?.profile === 'string' && data.profile ? data.profile : (profiles[0]?.identity || '')
  ))
  const [items, setItems] = useState([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState(null)
  const [search, setSearch] = useState('')
  const [progress, setProgress] = useState({})
  const [busy, setBusy] = useState(null)
  const [notice, setNotice] = useState(null)
  const listRef = useRef(null)
  const scrollRef = useRef(0)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  const rememberScroll = () => {
    if (listRef.current) scrollRef.current = listRef.current.scrollTop
  }

  const fetchItems = useCallback(async () => {
    if (!profile) return
    try {
      const d = await api.getClipboardItems(profile)
      if (!alive.current) return
      rememberScroll()
      setItems(Array.isArray(d.items) ? d.items : [])
      setError(null)
    } catch (e) {
      if (alive.current) setError(e.message)
    } finally {
      if (alive.current) setLoaded(true)
    }
  }, [profile])

  const fetchProgress = useCallback(async () => {
    try {
      const [legacy, status] = await Promise.all([
        api.getClipboardProgress().catch(() => ({})),
        profile ? api.getClipboardStatus(profile).catch(() => null) : Promise.resolve(null),
      ])
      if (!alive.current) return
      const v2 = status?.diagnostics?.stream_v2
      rememberScroll()
      setProgress(mergeTransferProgress(legacy, v2))
    } catch { /* progress is best effort */ }
  }, [profile])

  useEffect(() => {
    fetchItems()
    const id = setInterval(fetchItems, ITEM_POLL_MS)
    const unsub = api.subscribeSSE((ev) => {
      if (ev.type === 'clipboard_update' && (!ev.profiles || ev.profiles.includes(profile))) fetchItems()
    })
    return () => { clearInterval(id); unsub() }
  }, [fetchItems, profile])

  useEffect(() => {
    fetchProgress()
    const id = setInterval(fetchProgress, PROGRESS_POLL_MS)
    return () => clearInterval(id)
  }, [fetchProgress])

  // Restore the scroll offset after React committed the refreshed rows.
  useLayoutEffect(() => {
    const node = listRef.current
    if (node && node.scrollTop !== scrollRef.current) node.scrollTop = scrollRef.current
  }, [items, progress])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return items
    return items.filter((it) => (it.preview_text || it.display_name || '').toLowerCase().includes(q))
  }, [items, search])

  const act = async (kind, item) => {
    if (busy) return
    setBusy(item.item_id)
    setNotice(null)
    try {
      if (kind === 'set') {
        await api.pasteItem(profile, item.item_id)
        setNotice({ ok: true, text: 'Placed on the Windows clipboard' })
        onClose?.()
      } else if (kind === 'request') {
        await api.requestItem(profile, item.item_id)
        setNotice({ ok: true, text: 'Download requested' })
      } else if (kind === 'pin') {
        await api.pinItem(profile, item.item_id, !item.pinned)
      } else if (kind === 'delete') {
        await api.deleteItem(profile, item.item_id)
      }
      if (kind !== 'set') fetchItems()
    } catch (e) {
      setNotice({ ok: false, text: e.message })
    } finally {
      if (alive.current) setBusy(null)
    }
  }

  const sync = async () => {
    setNotice(null)
    try {
      await api.syncClipboard(profile)
      fetchItems()
    } catch (e) {
      setNotice({ ok: false, text: e.message })
    }
  }

  return (
    <section className="clip-panel" data-testid="clipboard-overlay" aria-label="FlowShift clipboard">
      <header className="clip-head">
        {profiles.length > 1 ? (
          <select
            className="clip-profile"
            value={profile}
            aria-label="Profile"
            onChange={(e) => { rememberScroll(); scrollRef.current = 0; setLoaded(false); setProfile(e.target.value) }}
          >
            {profiles.map((p) => (
              <option key={p.identity} value={p.identity}>{p.connected ? '● ' : '○ '}{p.label}</option>
            ))}
          </select>
        ) : (
          <span className="clip-profile-label">{profiles[0]?.label || profile || 'No profile'}</span>
        )}
        <input
          className="clip-search"
          type="search"
          placeholder="Search…"
          value={search}
          onChange={(e) => { scrollRef.current = 0; setSearch(e.target.value) }}
          aria-label="Search clipboard"
        />
        <button type="button" className="clip-btn" onClick={sync} title="Sync history" aria-label="Sync">⟳</button>
      </header>

      <div className="clip-list" ref={listRef} data-testid="clipboard-list">
        {!profile && <p className="clip-empty">No profile. Add a peer first.</p>}
        {profile && error && <p className="clip-empty clip-error">{error}</p>}
        {profile && !error && loaded && filtered.length === 0 && (
          <p className="clip-empty">{items.length ? 'No matches' : 'No clipboard items yet'}</p>
        )}
        {filtered.map((it) => {
          const p = progressLine(progress[it.item_id])
          const primary = it.available ? 'set' : 'request'
          return (
            <div
              key={it.item_id}
              className={`clip-row ${it.available ? '' : 'clip-row--missing'} ${busy === it.item_id ? 'clip-row--busy' : ''}`}
              data-testid="clip-row"
              data-item={it.item_id}
              onDoubleClick={() => act(primary, it)}
            >
              <div className="clip-row-main">
                <span className="clip-kind">{KIND_LABEL[it.kind] || it.kind || '?'}</span>
                <span className="clip-preview" title={it.preview_text || it.display_name || ''}>
                  {it.preview_text || it.display_name || `(${KIND_LABEL[it.kind] || 'item'})`}
                </span>
                {it.pinned && <span className="clip-pin" aria-label="pinned">📌</span>}
                {fmtSize(it.size) && <span className="clip-size">{fmtSize(it.size)}</span>}
              </div>
              {p && (
                <div className={`clip-progress clip-progress--${p.kind}`} data-testid="clip-progress">
                  {p.percent != null && (
                    <div className="clip-bar"><div className="clip-bar-fill" style={{ width: `${p.percent}%` }} /></div>
                  )}
                  <span className="clip-progress-text">{p.text}</span>
                </div>
              )}
              <div className="clip-actions">
                <button type="button" className="clip-btn clip-btn--primary" disabled={busy === it.item_id}
                  onClick={() => act(primary, it)} aria-label={it.available ? 'Set clipboard' : 'Download'}>
                  {it.available ? 'Set' : 'Get'}
                </button>
                <button type="button" className="clip-btn" onClick={() => act('pin', it)} aria-label={it.pinned ? 'Unpin' : 'Pin'}>
                  {it.pinned ? 'Unpin' : 'Pin'}
                </button>
                <button type="button" className="clip-btn clip-btn--danger" onClick={() => act('delete', it)} aria-label="Delete">
                  ✕
                </button>
              </div>
            </div>
          )
        })}
      </div>

      <footer className="clip-foot">
        {notice ? (
          <span className={notice.ok ? 'clip-ok' : 'clip-error'} role="status">{notice.text}</span>
        ) : (
          <span>{filtered.length} item{filtered.length === 1 ? '' : 's'}</span>
        )}
        <kbd>Esc</kbd>
      </footer>
    </section>
  )
}
