import { useState, useEffect, useRef } from 'react'
import * as api from '../api.js'

const DIRECTIONS = [
  { id: 'north', label: 'North', icon: 'fa-arrow-up' },
  { id: 'west', label: 'West', icon: 'fa-arrow-left' },
  { id: 'east', label: 'East', icon: 'fa-arrow-right' },
  { id: 'south', label: 'South', icon: 'fa-arrow-down' },
]

const DEFAULT_LAYOUT = {
  enabled: true,
  threshold_px: 3,
  inset_px: 24,
  cooldown_ms: 600,
  return_cooldown_ms: 400,
  edges: { north: null, south: null, east: null, west: null },
}

const OPPOSITE = { north: 'south', south: 'north', east: 'west', west: 'east' }

function normalizePeerIdentity(value, peers) {
  const ident = String(value || '').trim()
  if (!ident) return ''
  const peerByIdentity = new Map(peers.map((p) => [p.identity, p]))
  const peerByName = new Map(peers.map((p) => [p.name, p]))
  if (peerByIdentity.has(ident)) return ident
  if (peerByName.has(ident)) return peerByName.get(ident).identity || ident
  return ident
}

function normalizeLayout(layout, peers = []) {
  const raw = layout && typeof layout === 'object' ? layout : {}
  const peersList = Array.isArray(peers) ? peers : []
  const out = { ...DEFAULT_LAYOUT, ...raw }
  out.enabled = raw.enabled !== undefined ? Boolean(raw.enabled) : true
  for (const key of ['threshold_px', 'inset_px', 'cooldown_ms', 'return_cooldown_ms']) {
    const fallback = DEFAULT_LAYOUT[key]
    const n = Number.parseInt(raw[key], 10)
    out[key] = Number.isFinite(n) && n >= 0 ? n : fallback
  }
  const edges = { ...DEFAULT_LAYOUT.edges }
  const sourceEdges = raw.edges && typeof raw.edges === 'object' ? raw.edges : null
  for (const dir of DIRECTIONS.map((d) => d.id)) {
    const entry = sourceEdges ? sourceEdges[dir] : raw[dir]
    if (!entry) {
      edges[dir] = null
      continue
    }
    const peerIdentity = normalizePeerIdentity(
      typeof entry === 'string' ? entry : entry.peer_identity,
      peersList,
    )
    edges[dir] = peerIdentity
      ? { peer_identity: peerIdentity, target_entry_edge: entry.target_entry_edge || OPPOSITE[dir] }
      : null
  }
  out.edges = edges
  return out
}

function emptyWarnings(status) {
  return Array.isArray(status?.edge_switching?.warnings) ? status.edge_switching.warnings : []
}

export default function DisplayConfig({ status, onRefresh }) {
  const [layout, setLayout] = useState(DEFAULT_LAYOUT)
  const [peers, setPeers] = useState([])
  const [warnings, setWarnings] = useState([])
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState(null)
  const [editing, setEditing] = useState(null)
  const initialized = useRef(false)

  useEffect(() => {
    if (initialized.current) return
    if (status?.display_layout) {
      setLayout(normalizeLayout(status.display_layout, status?.peers || []))
      setWarnings(emptyWarnings(status))
      initialized.current = true
    }
    if (status?.peers) {
      setPeers(status.peers)
    } else {
      api.getDisplayLayout().then((d) => {
        setLayout(normalizeLayout(d.layout || {}, d.peers || []))
        setPeers(d.peers || [])
        setWarnings(d.warnings || [])
        initialized.current = true
      }).catch(() => {})
    }
  }, [status])

  const assignPeer = (direction, peerIdentity) => {
    setLayout((prev) => ({
      ...prev,
      edges: {
        ...prev.edges,
        [direction]: peerIdentity
          ? { peer_identity: peerIdentity, target_entry_edge: OPPOSITE[direction] }
          : null,
      },
    }))
    setEditing(null)
  }

  const handleSave = async () => {
    setSaving(true)
    setMsg(null)
    try {
      const res = await api.saveDisplayLayout(layout)
      setLayout(normalizeLayout(res.layout || layout, res.peers || peers))
      setWarnings(res.warnings || [])
      setMsg({ type: 'success', text: 'Display layout saved.' })
      await onRefresh()
    } catch (e) {
      setMsg({ type: 'error', text: e.message })
    } finally {
      setSaving(false)
    }
  }

  const activeIdent = status?.active_peer_identity
  const activePeer = status?.forwarding_target

  return (
    <div>
      <div className="page-title">
        <i className="fas fa-table-cells" /> Display Layout
        <span className="sub">Configure edge switching and peer targets</span>
      </div>

      {msg && (
        <div className={`msg-box ${msg.type}`}>
          <i className={`fas ${msg.type === 'error' ? 'fa-circle-exclamation' : 'fa-circle-check'}`} />
          {msg.text}
        </div>
      )}

      {warnings.length > 0 && (
        <div className="msg-box error">
          <i className="fas fa-triangle-exclamation" />
          {warnings.join(' | ')}
        </div>
      )}

      <div className="display-layout-row">
        <div className="settings-group">
          <h3><i className="fas fa-sliders" /> Edge Settings</h3>
          <div className="setting-row"><div className="setting-label">Enabled</div>
            <label className="toggle-switch">
              <input type="checkbox" checked={layout.enabled} onChange={(e) => setLayout((prev) => ({ ...prev, enabled: e.target.checked }))} />
              <span className="toggle-slider" />
            </label>
          </div>
          <SettingNumber label="Threshold PX" value={layout.threshold_px} onChange={(v) => setLayout((prev) => ({ ...prev, threshold_px: v }))} />
          <SettingNumber label="Inset PX" value={layout.inset_px} onChange={(v) => setLayout((prev) => ({ ...prev, inset_px: v }))} />
          <SettingNumber label="Cooldown MS" value={layout.cooldown_ms} onChange={(v) => setLayout((prev) => ({ ...prev, cooldown_ms: v }))} />
          <SettingNumber label="Return MS" value={layout.return_cooldown_ms} onChange={(v) => setLayout((prev) => ({ ...prev, return_cooldown_ms: v }))} />
        </div>

        <div className="display-grid-wrapper">
        <div className="display-grid">
          <div className="grid-cell corner"><div className="grid-corner-inner" /></div>
          <EdgeCell direction="north" entry={layout.edges.north} peers={peers} editing={editing === 'north'} onEdit={() => setEditing(editing === 'north' ? null : 'north')} onAssign={assignPeer} activeIdent={activeIdent} />
          <div className="grid-cell corner"><div className="grid-corner-inner" /></div>

          <EdgeCell direction="west" entry={layout.edges.west} peers={peers} editing={editing === 'west'} onEdit={() => setEditing(editing === 'west' ? null : 'west')} onAssign={assignPeer} activeIdent={activeIdent} />
          <CenterCell deviceName={status?.device?.display_name || status?.device_name} isActive={status?.forwarding_active} />
          <EdgeCell direction="east" entry={layout.edges.east} peers={peers} editing={editing === 'east'} onEdit={() => setEditing(editing === 'east' ? null : 'east')} onAssign={assignPeer} activeIdent={activeIdent} />

          <div className="grid-cell corner"><div className="grid-corner-inner" /></div>
          <EdgeCell direction="south" entry={layout.edges.south} peers={peers} editing={editing === 'south'} onEdit={() => setEditing(editing === 'south' ? null : 'south')} onAssign={assignPeer} activeIdent={activeIdent} />
          <div className="grid-cell corner"><div className="grid-corner-inner" /></div>
        </div>

        <div style={{ marginTop: 24, display: 'flex', gap: 20, justifyContent: 'center', flexWrap: 'wrap', fontSize: '.8rem', color: 'var(--text-dim)' }}>
          <span><span className="dot dot-green" /> Active forwarding</span>
          <span><i className="fas fa-mouse-pointer" /> Move mouse to an assigned edge</span>
          <span><i className="fas fa-arrow-right" /> Aktuelle Richtung: {activePeer || '–'}</span>
        </div>
      </div>
      </div>

      <button className="btn btn-primary btn-save" onClick={handleSave} disabled={saving}>
        <i className={`fas ${saving ? 'fa-spinner fa-spin' : 'fa-floppy-disk'}`} />
        {saving ? 'Saving…' : 'Save Layout'}
      </button>
    </div>
  )
}

function EdgeCell({ direction, entry, peers, editing, onEdit, onAssign, activeIdent }) {
  const peerIdentity = entry?.peer_identity || ''
  const peer = peers.find((p) => p.identity === peerIdentity || p.name === peerIdentity)
  const isActive = activeIdent && peerIdentity === activeIdent
  const peerName = peer?.display_name || peer?.name || peerIdentity || '–'

  return (
    <div className={`grid-cell slot ${isActive ? 'slot-active' : ''} ${peerIdentity ? 'slot-assigned' : 'slot-empty'}`} onClick={onEdit}>
      <div className="slot-arrow"><i className={`fas ${DIRECTIONS.find((d) => d.id === direction)?.icon || 'fa-circle'}`} /></div>
      <div className="slot-label">{direction.charAt(0).toUpperCase() + direction.slice(1)}</div>
      <div className={`slot-peer ${isActive ? 'slot-peer-active' : ''}`}>
        {peerIdentity ? (
          <><span className={`dot ${peer?.connected ? 'dot-green' : 'dot-gray'}`} /> {peerName}</>
        ) : (
          <span style={{ color: 'var(--text-muted)' }}>Unassigned</span>
        )}
      </div>
      {editing && (
        <div className="slot-picker" onClick={(e) => e.stopPropagation()}>
          <div className="slot-picker-header">
            Assign {direction.charAt(0).toUpperCase() + direction.slice(1)}
            <button className="btn btn-ghost btn-sm" onClick={(e) => { e.stopPropagation(); onEdit() }}>
              <i className="fas fa-xmark" />
            </button>
          </div>
          <div className="slot-picker-list">
            <button className="slot-picker-option" onClick={() => onAssign(direction, '')}>
              <span style={{ color: 'var(--text-muted)' }}>— None —</span>
            </button>
            {peers.map((p) => (
              <button key={p.identity} className={`slot-picker-option ${peerIdentity === p.identity ? 'selected' : ''}`} onClick={() => onAssign(direction, p.identity)}>
                <span className={`dot ${p.connected ? 'dot-green' : 'dot-gray'}`} />
                {p.display_name || p.name}
                <span style={{ color: 'var(--text-muted)', fontSize: '.72rem' }}>{p.host}</span>
              </button>
            ))}
            {peers.length === 0 && <div style={{ padding: 12, color: 'var(--text-muted)', fontSize: '.8rem' }}>No peers configured</div>}
          </div>
        </div>
      )}
    </div>
  )
}

function CenterCell({ deviceName, isActive }) {
  return (
    <div className="grid-cell center-cell">
      <div className="center-icon"><i className="fas fa-desktop" /></div>
      <div className="center-name">{deviceName || 'This PC'}</div>
      <div className="center-status"><span className={`dot ${isActive ? 'dot-green' : 'dot-gray'}`} />{isActive ? 'Forwarding active' : 'Standby'}</div>
    </div>
  )
}

function SettingNumber({ label, value, onChange }) {
  return (
    <div className="setting-row">
      <div className="setting-label">{label}</div>
      <input type="number" min="0" value={value} onChange={(e) => onChange(Number.parseInt(e.target.value || '0', 10) || 0)} style={{ width: 110 }} />
    </div>
  )
}
