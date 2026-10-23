import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import * as api from '../api.js'
import { mergeTransferProgress } from '../clipboardFormat.js'
import ClipboardCard from '../components/ClipboardCard.jsx'
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
    typeof data?.profile === 'string' && data.profile ? data.profile : (profiles[0]?.identity || 'local')
  ))
  const [items, setItems] = useState([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState(null)
  const [search, setSearch] = useState('')
  const [view, setView] = useState('recent')
  const [progress, setProgress] = useState({})
  const [busy, setBusy] = useState(null)
  const [notice, setNotice] = useState(null)
  const listRef = useRef(null)
  const scrollRef = useRef(0)
  const alive = useRef(true)
  const busyRef = useRef(false)

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
  }, [items, progress, view])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return items.filter((it) => {
      if (Boolean(it.pinned) !== (view === 'pinned')) return false
      if (!q) return true
      const card = it.file_card || {}
      return [it.preview_text, it.display_name, ...(card.root_names || []),
        ...(card.file_names || []), ...(card.relative_paths || [])]
        .some((value) => String(value || '').toLowerCase().includes(q))
    })
  }, [items, search, view])

  const counts = useMemo(() => ({
    recent: items.filter((item) => !item.pinned).length,
    pinned: items.filter((item) => item.pinned).length,
  }), [items])

  const act = async (kind, item, field) => {
    if (busyRef.current) return
    busyRef.current = true
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
      } else if (kind === 'copy') {
        const result = await api.copyItemCardText(profile, item.item_id, field)
        setNotice({ ok: true, text: `Copied ${result.count} item${result.count === 1 ? '' : 's'}` })
      }
      if (kind !== 'set') fetchItems()
    } catch (e) {
      setNotice({ ok: false, text: e.message })
    } finally {
      busyRef.current = false
      if (alive.current) setBusy(null)
    }
  }

  const sync = async () => {
    if (busyRef.current) return
    busyRef.current = true
    setBusy('sync')
    setNotice(null)
    try {
      await api.syncClipboard(profile)
      fetchItems()
    } catch (e) {
      setNotice({ ok: false, text: e.message })
    } finally {
      busyRef.current = false
      if (alive.current) setBusy(null)
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
        <button type="button" className="clip-btn" disabled={busy !== null} onClick={sync} title="Sync history" aria-label="Sync">⟳</button>
      </header>

      <nav className="clip-tabs" aria-label="Clipboard views">
        <button type="button" className={view === 'recent' ? 'active' : ''} onClick={() => { scrollRef.current = 0; setView('recent') }}>
          Recent <span>{counts.recent}</span>
        </button>
        <button type="button" className={view === 'pinned' ? 'active' : ''} onClick={() => { scrollRef.current = 0; setView('pinned') }}>
          Pinned <span>{counts.pinned}</span>
        </button>
      </nav>

      <div className="clip-list" ref={listRef} data-testid="clipboard-list">
        {!profile && <p className="clip-empty">No clipboard profile available.</p>}
        {profile && error && <p className="clip-empty clip-error">{error}</p>}
        {profile && !error && loaded && filtered.length === 0 && (
          <p className="clip-empty">{items.length ? 'No matches' : 'No clipboard items yet'}</p>
        )}
        {filtered.map((it) => {
          const primary = it.available ? 'set' : 'request'
          return (
            <ClipboardCard
              key={it.item_id}
              item={it}
              progress={progress[it.item_id]}
              busy={busy !== null}
              testId="clip-row"
              onPrimary={() => act(primary, it)}
              onPin={() => act('pin', it)}
              onDelete={() => act('delete', it)}
              onCopy={(field) => act('copy', it, field)}
            />
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
