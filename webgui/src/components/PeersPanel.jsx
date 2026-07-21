import { useState, useEffect, useCallback } from 'react'
import * as api from '../api.js'

const MOUSE_SMOOTH_OPTIONS = [
  { value: 2, label: 'Direct' },
  { value: 6, label: 'Normal' },
  { value: 10, label: 'Smooth' },
  { value: 16, label: 'Very Smooth' },
]

export default function PeersPanel({ status, onUpdated }) {
  const [configuredPeers, setConfiguredPeers] = useState([])
  const [discoveredPeers, setDiscoveredPeers] = useState([])
  const [scanning, setScanning] = useState(false)
  const [adding, setAdding] = useState(false)
  const [msg, setMsg] = useState(null)
  const [editingIdx, setEditingIdx] = useState(null)
  const [pingResults, setPingResults] = useState({})

  const [showAddForm, setShowAddForm] = useState(false)
  const [formName, setFormName] = useState('')
  const [formHost, setFormHost] = useState('')
  const [formPort, setFormPort] = useState(45781)
  const [formMouseSens, setFormMouseSens] = useState(1.0)
  const [formMouseSmooth, setFormMouseSmooth] = useState(6)
  const [formMouseBatch, setFormMouseBatch] = useState(12)
  const [formMouseSubpixel, setFormMouseSubpixel] = useState(true)

  const fetchPeers = useCallback(async () => {
    try {
      const s = await api.getSettings()
      if (s && s.peers) setConfiguredPeers(s.peers)
    } catch (e) {
      setMsg({ type: 'error', text: `Failed to load peers: ${e.message}` })
    }
  }, [])

  useEffect(() => { fetchPeers() }, [fetchPeers, status])

  const showMsg = (text, type = 'info') => {
    setMsg({ type, text })
    setTimeout(() => setMsg(null), 4000)
  }

  const handleScan = async () => {
    setScanning(true)
    setDiscoveredPeers([])
    setMsg(null)
    try {
      const result = await api.scanNetwork(2.0)
      const peers = result.peers || []
      if (peers.length === 0) showMsg('No peers discovered on the network.')
      setDiscoveredPeers(peers)
    } catch (e) {
      showMsg(`Scan failed: ${e.message}`, 'error')
    } finally { setScanning(false) }
  }

  const handleAddDiscovered = async (peer) => {
    try {
      await api.addPeer(peer)
      showMsg(`Added ${peer.name || peer.host}`)
      setDiscoveredPeers((prev) => prev.filter((p) => p !== peer))
      await fetchPeers()
      if (onUpdated) onUpdated()
    } catch (e) {
      showMsg(`Failed to add peer: ${e.message}`, 'error')
    }
  }

  const handleAddManual = async (e) => {
    e.preventDefault()
    if (!formHost.trim()) return
    setAdding(true)
    try {
      const peer = {
        name: formName.trim() || undefined,
        host: formHost.trim(),
        port: formPort,
        mouse: {
          sensitivity: formMouseSens,
          flush_interval_ms: formMouseSmooth,
          max_batch_ms: formMouseBatch,
          accumulate_subpixel: formMouseSubpixel,
        },
      }
      await api.addPeer(peer)
      showMsg(`Added ${formName.trim() || formHost.trim()}`)
      setShowAddForm(false)
      setFormName(''); setFormHost(''); setFormPort(45781)
      setFormMouseSens(1.0); setFormMouseSmooth(6); setFormMouseBatch(12); setFormMouseSubpixel(true)
      await fetchPeers()
      if (onUpdated) onUpdated()
    } catch (err) {
      showMsg(`Failed to add peer: ${err.message}`, 'error')
    } finally { setAdding(false) }
  }

  const handleRemove = async (index, peer) => {
    if (!confirm(`Remove "${peer.name || peer.host}"?`)) return
    try {
      await api.removePeer(index)
      showMsg(`Removed ${peer.name || peer.host}`)
      await fetchPeers()
      if (onUpdated) onUpdated()
    } catch (e) {
      showMsg(`Failed to remove peer: ${e.message}`, 'error')
    }
  }

  const handlePing = async (peer) => {
    const ref = peer.identity || peer.name || peer.host
    setPingResults((prev) => ({ ...prev, [ref]: { pinging: true } }))
    try {
      const r = await api.pingPeer(ref)
      setPingResults((prev) => ({ ...prev, [ref]: { pinging: false, rtt_ms: r.rtt_ms, host: r.host } }))
    } catch (e) {
      setPingResults((prev) => ({ ...prev, [ref]: { pinging: false, error: e.message } }))
    }
  }

  const handleSaveEdit = async (idx, data) => {
    try {
      await api.editPeer(idx, data)
      showMsg('Peer updated')
      setEditingIdx(null)
      await fetchPeers()
      if (onUpdated) onUpdated()
    } catch (e) {
      showMsg(`Failed to update peer: ${e.message}`, 'error')
    }
  }

  const activePeer = status?.active_peer || null

  return (
    <div className="peers-panel">
      <div className="page-title">
        <i className="fas fa-network-wired" /> Peers
        <span className="sub">{status?.device_name || '–'}</span>
      </div>

      {msg && (
        <div className={`msg-box ${msg.type || 'info'}`}>
          <i className={`fas ${msg.type === 'error' ? 'fa-circle-exclamation' : 'fa-circle-info'}`} />
          {msg.text}
        </div>
      )}

      {/* ── Configured Peers ── */}
      <div className="settings-group">
        <h3><i className="fas fa-list" /> Configured Peers ({configuredPeers.length})</h3>

        {configuredPeers.length === 0 ? (
          <p style={{ color: 'var(--text-muted)', fontSize: '.85rem', padding: 8 }}>No peers configured.</p>
        ) : (
          <table className="peer-table" style={{ marginTop: 8 }}>
            <thead>
              <tr>
                <th style={{ width: 34 }}>#</th>
                <th>Name</th>
                <th>Host</th>
                <th>Port</th>
                <th>Active</th>
                <th>Ping</th>
                <th style={{ width: 100 }}></th>
              </tr>
            </thead>
            <tbody>
              {configuredPeers.map((peer, i) => (
                editingIdx === i ? (
                  <PeerEditRow
                    key={i}
                    peer={peer}
                    onSave={(data) => handleSaveEdit(i, data)}
                    onCancel={() => setEditingIdx(null)}
                  />
                ) : (
                  <tr key={i} className={activePeer === peer.name || activePeer === peer.host || activePeer === peer.identity ? 'peer-active-row' : ''}>
                    <td style={{ color: 'var(--text-muted)', fontSize: '.8rem' }}>{i + 1}</td>
                    <td style={{ fontWeight: 500 }}>{peer.name || peer.host}</td>
                    <td><code>{peer.host}</code></td>
                    <td>{peer.port}</td>
                    <td>
                      {activePeer === peer.name || activePeer === peer.host || activePeer === peer.identity ? (
                        <span style={{ color: 'var(--green)' }}><i className="fas fa-circle" style={{ fontSize: '.6rem', marginRight: 4 }} /> Active</span>
                      ) : (
                        <span style={{ color: 'var(--text-muted)' }}>—</span>
                      )}
                    </td>
                    <td>
                      <PingButton peer={peer} pingResults={pingResults} onPing={() => handlePing(peer)} />
                    </td>
                    <td>
                      <div style={{ display: 'flex', gap: 6 }}>
                        <button className="btn btn-outline btn-sm" onClick={() => setEditingIdx(i)}>
                          <i className="fas fa-pen" /> Edit
                        </button>
                        <button className="btn btn-outline-danger btn-sm" onClick={() => handleRemove(i, peer)}>
                          <i className="fas fa-trash-can" /> Remove
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              ))}
            </tbody>
          </table>
        )}

        <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
          <button className="btn btn-secondary btn-sm" onClick={() => setShowAddForm(!showAddForm)}>
            <i className={`fas ${showAddForm ? 'fa-xmark' : 'fa-plus'}`} /> {showAddForm ? 'Cancel' : 'Add Peer'}
          </button>
          <button className="btn btn-ghost btn-sm" onClick={handleScan} disabled={scanning}>
            <i className={`fas ${scanning ? 'fa-spinner fa-spin' : 'fa-magnifying-glass'}`} />
            {scanning ? 'Scanning…' : 'Scan Network'}
          </button>
        </div>

        {showAddForm && (
          <form className="peer-add-form" onSubmit={handleAddManual}>
            <input type="text" placeholder="Name (optional)" value={formName} onChange={(e) => setFormName(e.target.value)} />
            <input type="text" placeholder="IP Address *" value={formHost} onChange={(e) => setFormHost(e.target.value)} required />
            <input type="number" placeholder="Port" value={formPort} onChange={(e) => setFormPort(Number(e.target.value))} min={1} max={65535} />
            <details className="mouse-details">
              <summary>Mouse Settings</summary>
              <div className="mouse-fields">
                <label>Sensitivity: <input type="number" step={0.05} min={0.25} max={3.0} value={formMouseSens} onChange={(e) => setFormMouseSens(Number(e.target.value))} /></label>
                <label>Smoothness: <select value={formMouseSmooth} onChange={(e) => setFormMouseSmooth(Number(e.target.value))}>{MOUSE_SMOOTH_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select></label>
                <label>Batch (ms): <input type="number" min={1} max={200} value={formMouseBatch} onChange={(e) => setFormMouseBatch(Number(e.target.value))} /></label>
                <label><input type="checkbox" checked={formMouseSubpixel} onChange={(e) => setFormMouseSubpixel(e.target.checked)} /> Subpixel</label>
              </div>
            </details>
            <button className="btn btn-secondary btn-sm" type="submit" disabled={adding}>
              <i className={`fas ${adding ? 'fa-spinner fa-spin' : 'fa-floppy-disk'}`} /> {adding ? 'Saving…' : 'Save'}
            </button>
          </form>
        )}
      </div>

      {/* ── Discovered Peers ── */}
      {discoveredPeers.length > 0 && (
        <div className="settings-group">
          <h3><i className="fas fa-satellite-dish" /> Discovered ({discoveredPeers.length})</h3>
          <table className="peer-table" style={{ marginTop: 8 }}>
            <thead>
              <tr>
                <th>Name</th>
                <th>Host</th>
                <th>Port</th>
                <th>Device ID</th>
                <th style={{ width: 80 }}></th>
              </tr>
            </thead>
            <tbody>
              {discoveredPeers.map((peer, i) => (
                <tr key={i}>
                  <td style={{ fontWeight: 500 }}>{peer.name || peer.host}</td>
                  <td><code>{peer.host}</code></td>
                  <td>{peer.port}</td>
                  <td><code style={{ fontSize: '.72rem' }}>{(peer.device_id || '').slice(0, 16)}…</code></td>
                  <td>
                    <button className="btn btn-secondary btn-sm" onClick={() => handleAddDiscovered(peer)}>
                      <i className="fas fa-plus" /> Add
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function PingButton({ peer, pingResults, onPing }) {
  const ref = peer.identity || peer.name || peer.host
  const pr = pingResults[ref]
  const pinging = pr?.pinging
  const rtt = pr?.rtt_ms
  const err = pr?.error

  if (pinging) return <span style={{ color: 'var(--text-dim)', fontSize: '.78rem' }}><i className="fas fa-spinner fa-spin" /> pinging…</span>
  if (rtt != null) return <span style={{ color: rtt < 50 ? 'var(--green)' : rtt < 150 ? 'var(--orange)' : 'var(--red)', fontSize: '.78rem', cursor: 'pointer' }} onClick={onPing}>{rtt}ms</span>
  if (err) return <span style={{ color: 'var(--red)', fontSize: '.72rem', cursor: 'pointer' }} onClick={onPing} title={err}>ERR</span>

  return (
    <button className="btn btn-ghost btn-sm btn-icon" onClick={onPing} title="Ping peer" style={{ padding: '2px 8px', fontSize: '.72rem' }}>
      <i className="fas fa-plug" /> Ping
    </button>
  )
}

function PeerEditRow({ peer, onSave, onCancel }) {
  const [name, setName] = useState(peer.name || '')
  const [host, setHost] = useState(peer.host || '')
  const [port, setPort] = useState(peer.port || 45781)
  const m = peer.mouse || {}
  const [sens, setSens] = useState(m.sensitivity != null ? m.sensitivity : 1.0)
  const [smooth, setSmooth] = useState(m.flush_interval_ms != null ? m.flush_interval_ms : 6)
  const [batch, setBatch] = useState(m.max_batch_ms != null ? m.max_batch_ms : 12)
  const [subpixel, setSubpixel] = useState(m.accumulate_subpixel != null ? m.accumulate_subpixel : true)

  const handleSave = () => {
    const data = { name: name.trim() || undefined, host: host.trim(), port }
    data.mouse = { sensitivity: sens, flush_interval_ms: smooth, max_batch_ms: batch, accumulate_subpixel: subpixel }
    if (peer.device_id) data.device_id = peer.device_id
    onSave(data)
  }

  return (
    <tr>
      <td colSpan={7} style={{ padding: 0 }}>
        <div className="peer-edit-inline">
          <div className="peer-edit-fields">
            <label>Name <input type="text" value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label>Host <input type="text" value={host} onChange={(e) => setHost(e.target.value)} /></label>
            <label>Port <input type="number" value={port} onChange={(e) => setPort(Number(e.target.value))} min={1} max={65535} /></label>
            <details className="mouse-details" open>
              <summary>Mouse Settings</summary>
              <div className="mouse-fields">
                <label>Sensitivity: <input type="number" step={0.05} min={0.25} max={3.0} value={sens} onChange={(e) => setSens(Number(e.target.value))} /></label>
                <label>Smoothness: <select value={smooth} onChange={(e) => setSmooth(Number(e.target.value))}>{MOUSE_SMOOTH_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select></label>
                <label>Batch (ms): <input type="number" min={1} max={200} value={batch} onChange={(e) => setBatch(Number(e.target.value))} /></label>
                <label><input type="checkbox" checked={subpixel} onChange={(e) => setSubpixel(e.target.checked)} /> Subpixel</label>
              </div>
            </details>
          </div>
          <div className="peer-edit-actions">
            <button className="btn btn-primary btn-sm" onClick={handleSave}><i className="fas fa-floppy-disk" /> Save</button>
            <button className="btn btn-ghost btn-sm" onClick={onCancel}><i className="fas fa-xmark" /> Cancel</button>
          </div>
        </div>
      </td>
    </tr>
  )
}
