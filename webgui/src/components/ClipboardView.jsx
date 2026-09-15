import { useState, useEffect, useCallback, useLayoutEffect, useMemo, useRef } from 'react'
import * as api from '../api.js'
import { fmtSize, mergeTransferProgress, progressLine } from '../clipboardFormat.js'

const KIND_CONFIG = {
  text:     { icon: 'fa-file-lines',      label: 'Text' },
  html:     { icon: 'fa-code',            label: 'HTML' },
  image:    { icon: 'fa-image',           label: 'Image' },
  gif:      { icon: 'fa-image',           label: 'GIF' },
  file:     { icon: 'fa-file',            label: 'File' },
  file_batch: { icon: 'fa-files',         label: 'Files' },
  audio:    { icon: 'fa-music',           label: 'Audio' },
  binary:   { icon: 'fa-file',            label: 'Binary' },
}

function fmtTime(ts) {
  if (!ts) return ''
  const d = typeof ts === 'string' ? new Date(ts) : new Date(ts)
  const now = Date.now()
  const diff = now - d.getTime()
  if (diff < 60000) return 'just now'
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`
  if (diff < 604800000) return `${Math.floor(diff / 86400000)}d ago`
  return d.toLocaleDateString()
}

export default function ClipboardView({ status, onRefresh }) {
  const [items, setItems] = useState([])
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [progress, setProgress] = useState({})
  const [detail, setDetail] = useState(null)
  const [actionLoading, setActionLoading] = useState(null)
  const [thumbnails, setThumbnails] = useState({})
  const [profile, setProfile] = useState('')
  const [profiles, setProfiles] = useState([])
  const listRef = useRef(null)
  const scrollRef = useRef(0)
  const initialLoadRef = useRef(true)
  const alive = useRef(true)
  const itemsRequestRef = useRef(0)
  const progressRequestRef = useRef(0)

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  const rememberScroll = () => {
    if (listRef.current) scrollRef.current = listRef.current.scrollTop
  }

  useEffect(() => {
    const ps = (status?.peers || []).map((p) => ({
      identity: p.identity,
      label: `${p.name} (${p.host})`,
      connected: p.connected,
    }))
    setProfiles(ps)
    if (!profile && ps.length > 0) {
      setProfile(ps[0].identity)
    }
  }, [status, profile])

  const fetchItems = useCallback(async () => {
    if (!profile) return
    const request = ++itemsRequestRef.current
    try {
      if (initialLoadRef.current) setLoading(true)
      const d = await api.getClipboardItems(profile)
      if (!alive.current || request !== itemsRequestRef.current) return
      rememberScroll()
      setItems(Array.isArray(d.items) ? d.items : [])
      setError(null)
    } catch (e) {
      if (alive.current && request === itemsRequestRef.current) setError(e.message)
    } finally {
      if (alive.current && request === itemsRequestRef.current) {
        setLoading(false)
        setLoaded(true)
        initialLoadRef.current = false
      }
    }
  }, [profile])

  useEffect(() => {
    fetchItems()
    const poll = setInterval(fetchItems, 4000)
    const unsub = api.subscribeSSE((ev) => {
      if (ev.type === 'clipboard_update') {
        if (!profile || (ev.profiles && ev.profiles.includes(profile))) {
          fetchItems()
        }
      }
    })
    return () => {
      clearInterval(poll)
      unsub()
    }
  }, [fetchItems, profile])

  const fetchProgress = useCallback(async () => {
    const request = ++progressRequestRef.current
    try {
      const [legacy, clipboardStatus] = await Promise.all([
        api.getClipboardProgress().catch(() => ({})),
        profile ? api.getClipboardStatus(profile).catch(() => null) : Promise.resolve(null),
      ])
      if (!alive.current || request !== progressRequestRef.current) return
      rememberScroll()
      setProgress(mergeTransferProgress(legacy, clipboardStatus?.diagnostics?.stream_v2))
    } catch { }
  }, [profile])

  useEffect(() => {
    fetchProgress()
    const id = setInterval(fetchProgress, 600)
    return () => clearInterval(id)
  }, [fetchProgress])

  useLayoutEffect(() => {
    const node = listRef.current
    if (node && node.scrollTop !== scrollRef.current) node.scrollTop = scrollRef.current
  }, [items, progress])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return items
    return items.filter((it) => (it.preview_text || it.display_name || '').toLowerCase().includes(q))
  }, [items, search])

  const selectProfile = (identity) => {
    rememberScroll()
    itemsRequestRef.current += 1
    progressRequestRef.current += 1
    scrollRef.current = 0
    initialLoadRef.current = true
    setLoaded(false)
    setItems([])
    setProgress({})
    setProfile(identity)
    setSelectedId(null)
    setDetail(null)
    setThumbnails({})
  }

  const handleSelect = async (itemId) => {
    setSelectedId(itemId)
    setDetail(null)
    try {
      const d = await api.getClipboardItem(profile, itemId)
      setDetail(d)
      if (d.item?.kind === 'image' && d.image_b64) {
        setThumbnails((prev) => ({ ...prev, [itemId]: d.image_b64 }))
      }
    } catch (e) {
      setDetail({ error: e.message })
    }
  }

  const doAction = async (action, itemId, extra) => {
    setActionLoading(itemId)
    try {
      if (action === 'paste') {
        await api.pasteItem(profile, itemId)
      } else if (action === 'delete') {
        await api.deleteItem(profile, itemId)
        if (selectedId === itemId) { setSelectedId(null); setDetail(null) }
        fetchItems()
      } else if (action === 'pin') {
        await api.pinItem(profile, itemId, extra)
        fetchItems()
      } else if (action === 'request') {
        await api.requestItem(profile, itemId)
      } else if (action === 'sync') {
        await api.syncClipboard(profile)
        fetchItems()
      }
    } catch (e) {
      alert(e.message)
    } finally {
      setActionLoading(null)
    }
  }

  const loadThumb = async (itemId) => {
    if (thumbnails[itemId]) return
    try {
      const d = await api.getThumbnail(profile, itemId)
      if (d.ppm_b64) setThumbnails((prev) => ({ ...prev, [itemId]: d.ppm_b64 }))
    } catch { }
  }

  const handleClear = async () => {
    if (!confirm('Delete all clipboard history for this profile?')) return
    try {
      await api.clearClipboard(profile)
      setItems([]); setSelectedId(null); setDetail(null)
    } catch (e) { alert(e.message) }
  }

  return (
    <div className="clipboard-view">
      <div className="page-title">
        <i className="fas fa-clipboard-list" /> Clipboard
        <span className="sub">
          {profile && profiles.find((p) => p.identity === profile)
            ? profiles.find((p) => p.identity === profile).label
            : 'Select a profile'}
        </span>
      </div>

      {/* ── Toolbar ── */}
      <div className="clipboard-toolbar">
        <div className="clipboard-toolbar-left">
          <select
            className="profile-select"
            aria-label="Clipboard profile"
            value={profile}
            onChange={(e) => selectProfile(e.target.value)}
          >
            {profiles.length === 0 && <option value="">No profiles available</option>}
            {profiles.map((p) => (
              <option key={p.identity} value={p.identity}>
                {p.label} {p.connected ? '🟢' : '⚫'}
              </option>
            ))}
          </select>
        </div>
        <div className="clipboard-toolbar-actions">
          <button className="btn btn-ghost btn-sm" onClick={fetchItems} disabled={!loaded && loading}>
            <i className={`fas ${(!loaded && loading) ? 'fa-spinner fa-spin' : 'fa-rotate'}`} /> Refresh
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => doAction('sync')}>
            <i className="fas fa-cloud-arrow-down" /> Sync
          </button>
          <button className="btn btn-ghost-danger btn-sm" onClick={handleClear}>
            <i className="fas fa-trash-can" /> Clear
          </button>
        </div>
      </div>

      {/* ── Empty: no profile ── */}
      {!profile && (
        <div className="empty-state">
          <div className="big-icon"><i className="fas fa-plug-circle-xmark" /></div>
          <p>No profile selected. Choose a profile above or activate forwarding to see clipboard history.</p>
        </div>
      )}

      {/* ── Main layout ── */}
      {profile && (
        <div className="clipboard-layout">
          {/* ── List ── */}
          <div className="clipboard-list-panel">
            <div className="clipboard-list-header">
              <div className="clipboard-search">
                <i className="fas fa-search" style={{ color: 'var(--text-muted)' }} />
                <input
                  type="text"
                  placeholder="Search clipboard…"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </div>
              <span className="item-count-badge">{filtered.length}</span>
            </div>

            <div className="item-list" ref={listRef} data-testid="clipboard-item-list">
              {error && <p style={{ color: 'var(--red)', padding: 12, fontSize: '.85rem' }}>{error}</p>}
              {!loaded && loading && <p style={{ color: 'var(--text-muted)', padding: 12, fontSize: '.85rem' }}>Loading…</p>}
              {loaded && !error && filtered.length === 0 && (
                <p style={{ color: 'var(--text-muted)', padding: 12, fontSize: '.85rem' }}>
                  {items.length ? 'No matches.' : 'No clipboard items yet. Copy something on the remote machine.'}
                </p>
              )}
              {filtered.map((it) => {
                const kind = it.kind || 'text'
                const cfg = KIND_CONFIG[kind] || KIND_CONFIG.binary
                const isImage = kind === 'image' || kind === 'gif'
                return (
                  <div
                    key={it.item_id}
                    className={`item-row ${selectedId === it.item_id ? 'selected' : ''} ${!it.available ? 'unavailable' : ''}`}
                    onClick={() => handleSelect(it.item_id)}
                    onMouseEnter={() => isImage && loadThumb(it.item_id)}
                  >
                    <div className="item-icon">
                      {isImage && thumbnails[it.item_id] ? (
                        <img src={`data:image/x-portable-pixmap;base64,${thumbnails[it.item_id]}`} alt="" className="item-thumb" />
                      ) : (
                        <i className={`fas ${cfg.icon}`} />
                      )}
                    </div>
                    <div className="item-body">
                      <div className="item-preview">{it.preview_text || it.display_name || `(${cfg.label})`}</div>
                      <div className="item-meta">
                        <span className="kind-tag">{cfg.label}</span>
                        {it.pinned && <i className="fas fa-thumbtack pin-icon" style={{ fontSize: '.65rem' }} />}
                        {!it.available && <span className="dl-badge">DL</span>}
                        {fmtSize(it.size) && <span className="size-tag">{fmtSize(it.size)}</span>}
                        <span style={{ color: 'var(--text-muted)', fontSize: '.65rem' }}>{fmtTime(it.created_at || it.seq)}</span>
                      </div>
                      <TransferProgress progress={progress[it.item_id]} />
                    </div>
                  </div>
                )
              })}
            </div>
          </div>

          {/* ── Detail ── */}
          <div className="clipboard-detail-panel">
            {!selectedId && (
              <div className="empty-detail">
                <div className="big-icon"><i className="fas fa-hand-pointer" /></div>
                <p>Select an item</p>
              </div>
            )}
            {detail && detail.error && (
              <div style={{ padding: 18, color: 'var(--red)' }}><i className="fas fa-exclamation-circle" /> {detail.error}</div>
            )}
            {detail && detail.item && (
              <ItemDetail
                item={detail.item}
                kind={detail.kind}
                text={detail.text}
                htmlB64={detail.html_b64}
                imageB64={detail.image_b64}
                onPaste={() => doAction('paste', detail.item.item_id)}
                onDelete={() => doAction('delete', detail.item.item_id)}
                onPin={(v) => doAction('pin', detail.item.item_id, v)}
                onRequest={() => doAction('request', detail.item.item_id)}
                busy={actionLoading === detail.item.item_id}
                progress={progress[detail.item.item_id]}
              />
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function ItemDetail({ item, kind, text, htmlB64, imageB64, onPaste, onDelete, onPin, onRequest, busy, progress }) {
  const cfg = KIND_CONFIG[kind] || KIND_CONFIG.binary
  const isFile = kind === 'file' || kind === 'file_batch'
  const isImage = kind === 'image' || kind === 'gif'
  const isHtml = kind === 'html'
  const needsDl = !item.available

  return (
    <>
      <div className="detail-header">
        <h2>{item.display_name || item.preview_text || `(${cfg.label})`}</h2>
        <div className="detail-kind">
          <i className={`fas ${cfg.icon}`} /> {cfg.label}
          {item.mime && <> &middot; {item.mime}</>}
        </div>
      </div>

      <div className="detail-body">
        <div className="detail-actions">
          {needsDl ? (
            <button className="btn btn-primary" onClick={onRequest} disabled={busy}>
              <i className={`fas ${busy ? 'fa-spinner fa-spin' : 'fa-cloud-arrow-down'}`} />
              {busy ? 'Downloading…' : 'Download'}
            </button>
          ) : (
            <button className="btn btn-primary" onClick={onPaste} disabled={busy}>
              <i className={`fas ${busy ? 'fa-spinner fa-spin' : 'fa-paste'}`} />
              {busy ? 'Pasting…' : 'Paste to Clipboard'}
            </button>
          )}
          <button className="btn btn-ghost" onClick={() => onPin(!item.pinned)}>
            <i className={`fas fa-thumbtack ${item.pinned ? '' : 'fa-rotate-90'}`} />
            {item.pinned ? 'Unpin' : 'Pin'}
          </button>
          <button className="btn btn-ghost-danger" onClick={onDelete} disabled={busy}>
            <i className="fas fa-trash-can" /> Delete
          </button>
        </div>

        <div className="detail-meta-grid">
          <span className="meta-label">Size</span><span>{fmtSize(item.size) || '–'}</span>
          {item.files && <><span className="meta-label">Files</span><span>{item.files.length}</span></>}
          {item.file_count && <><span className="meta-label">Files</span><span>{item.file_count}</span></>}
          <span className="meta-label">Created</span><span>{item.created_at ? new Date(item.created_at).toLocaleString() : '–'}</span>
          <span className="meta-label">Available</span>
          <span>{item.available
            ? <i className="fas fa-circle-check" style={{ color: 'var(--green)' }} />
            : <i className="fas fa-circle-xmark" style={{ color: 'var(--red)' }} />}
          </span>
          <span className="meta-label">Pinned</span><span>{item.pinned ? 'Yes' : 'No'}</span>
          <span className="meta-label">ID</span><span><code>{item.item_id}</code></span>
        </div>

        {isFile && text && (
          <div className="detail-section">
            <h3><i className="fas fa-files" /> Files</h3>
            <pre className="file-list">{text}</pre>
          </div>
        )}

        {isImage && imageB64 && (
          <div className="detail-section">
            <h3><i className="fas fa-image" /> Preview</h3>
            <img src={`data:image/png;base64,${imageB64}`} alt="" className="detail-image" />
          </div>
        )}

        {isHtml && htmlB64 && (
          <div className="detail-section">
            <h3><i className="fas fa-code" /> HTML Preview</h3>
            <iframe srcDoc={atob(htmlB64)} className="html-frame" title="HTML" sandbox="allow-same-origin" />
          </div>
        )}

        {kind === 'text' && text && (
          <div className="detail-section">
            <h3><i className="fas fa-align-left" /> Content</h3>
            <pre className="text-content">{text}</pre>
          </div>
        )}

        <TransferProgress progress={progress} />
      </div>
    </>
  )
}

function TransferProgress({ progress }) {
  const line = progressLine(progress)
  if (!line) return null
  if (line.percent == null) {
    return <div className={`transfer-status ${line.kind}`} title={progress?.error || ''}>{line.text}</div>
  }

  return (
    <div className="transfer-progress">
      <div className="transfer-bar-track">
        <div className="transfer-bar-fill" style={{ width: `${line.percent}%` }} />
      </div>
      <div className="transfer-label">{line.text}</div>
    </div>
  )
}
