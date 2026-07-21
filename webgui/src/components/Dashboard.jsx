import { useState, useEffect } from 'react'
import * as api from '../api.js'

export default function Dashboard({ status, onRefresh }) {
  const [peers, setPeers] = useState([])
  const [hotkeys, setHotkeys] = useState([])
  const [activating, setActivating] = useState(null)

  useEffect(() => {
    if (status?.peers) {
      setPeers(status.peers)
    } else {
      api.getPeers().then((d) => setPeers(d.peers || [])).catch(() => {})
    }
    api.getHotkeys().then((d) => setHotkeys(d.hotkeys || [])).catch(() => {})
  }, [status])

  const handleToggle = async (ident) => {
    setActivating(ident)
    try {
      await api.toggleForwarding(ident)
      await onRefresh()
    } catch (e) {
      alert(e.message)
    } finally {
      setActivating(null)
    }
  }

  const isFwd = status?.forwarding_active
  const isHealthy = status?.runtime_healthy !== false
  const down = status?.critical_workers_down || []

  return (
    <div>
      <div className="page-title">
        <i className="fas fa-gauge-high" /> Dashboard
        <span className="sub">{status?.device_name || '–'}</span>
      </div>

      {/* ── Status Cards ── */}
      <div className="card-grid">
        <StatCard
          icon="fa-globe" label="Network"
          value={status?.network_connected ? 'Connected' : 'Disconnected'}
          color={status?.network_connected ? 'var(--green)' : 'var(--text-muted)'}
          bg={status?.network_connected ? 'rgba(46,204,113,.12)' : 'rgba(85,85,119,.12)'}
          sub={status?.network_peer || ''}
        />
        <StatCard
          icon="fa-arrow-right" label="Forwarding"
          value={isFwd ? `→ ${status?.forwarding_target}` : 'Inactive'}
          color={isFwd ? 'var(--green)' : 'var(--text-muted)'}
          bg={isFwd ? 'rgba(46,204,113,.12)' : 'rgba(85,85,119,.12)'}
          sub={isFwd ? status?.mode : ''}
        />
        <StatCard
          icon="fa-table-cells" label="Edge Switching"
          value={status?.edge_switching?.enabled ? 'Enabled' : 'Disabled'}
          color={status?.edge_switching?.enabled ? 'var(--green)' : 'var(--text-muted)'}
          bg={status?.edge_switching?.enabled ? 'rgba(46,204,113,.12)' : 'rgba(85,85,119,.12)'}
          sub={status?.edge_switching?.active_session ? `${status.edge_switching.active_session.role} ${status.edge_switching.active_session.source_exit_edge}->${status.edge_switching.active_session.target_entry_edge}` : 'No active session'}
        />
        <StatCard
          icon="fa-clipboard" label="Clipboard"
          value={status?.capture_active ? 'Active' : 'Idle'}
          color={status?.capture_active ? 'var(--green)' : 'var(--text-muted)'}
          bg={status?.capture_active ? 'rgba(46,204,113,.12)' : 'rgba(85,85,119,.12)'}
          sub={status?.forwarding_target ? `→ ${status.forwarding_target}` : ''}
        />
        <StatCard
          icon="fa-server" label="Peers"
          value={`${peers.length} configured`}
          color="var(--blue)"
          bg="rgba(52,152,219,.12)"
          sub={`${peers.filter((p) => p.connected).length} connected`}
        />
        <StatCard
          icon="fa-heart-pulse" label="Health"
          value={isHealthy ? 'All OK' : `${down.length} down`}
          color={isHealthy ? 'var(--green)' : 'var(--red)'}
          bg={isHealthy ? 'rgba(46,204,113,.12)' : 'rgba(231,76,60,.12)'}
        />
        <StatCard
          icon="fa-microchip" label="Device"
          value={status?.device_name || '–'}
          color="var(--accent2)"
          bg="rgba(83,52,131,.15)"
          sub={`ID: ${(status?.device_id || '').slice(0, 8)}`}
        />
      </div>

      {/* ── Workers ── */}
      {status?.workers && (
        <>
          <div className="section-title"><i className="fas fa-cogs" /> Workers</div>
          <div className="worker-grid">
            {Object.entries(status.workers).map(([name, info]) => (
              <WorkerChip key={name} name={name} info={info} />
            ))}
          </div>
        </>
      )}

      {/* ── Peers / Profiles ── */}
      <div className="section-title"><i className="fas fa-network-wired" /> Profiles</div>
      {peers.length === 0 ? (
        <p style={{ color: 'var(--text-muted)', fontSize: '.85rem' }}>No profiles configured.</p>
      ) : (
        <table className="peer-table">
          <thead>
            <tr>
              <th>Profile</th>
              <th>Host</th>
              <th>Status</th>
              <th>Direction</th>
              <th>OS / Version</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {peers.map((p) => (
              <tr key={p.identity} className={isFwd && status?.active_peer_identity === p.identity ? 'peer-active-row' : ''}>
                <td style={{ fontWeight: 600 }}>
                  {p.name}
                  {isFwd && status?.active_peer_identity === p.identity && (
                    <span style={{ marginLeft: 8, fontSize: '.7rem', background: 'var(--green)', color: '#fff', padding: '2px 8px', borderRadius: 10, fontWeight: 500 }}>ACTIVE</span>
                  )}
                </td>
                <td><code>{p.host}:{p.port}</code></td>
                <td>
                  <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <span className={`dot ${p.connected ? 'dot-green' : 'dot-gray'}`} />
                    {p.connected ? 'Connected' : 'Disconnected'}
                  </span>
                </td>
                <td>
                  {p.direction ? (
                    <span style={{ background: 'var(--accent2)', color: '#ddd', padding: '2px 8px', borderRadius: 4, fontSize: '.75rem' }}>
                      {p.direction}
                    </span>
                  ) : <span style={{ color: 'var(--text-muted)' }}>–</span>}
                </td>
                <td style={{ fontSize: '.8rem', color: 'var(--text-dim)' }}>
                  {p.remote_os || '–'} {p.remote_version ? `v${p.remote_version}` : ''}
                </td>
                <td>
                  <button
                    className={`btn ${isFwd && status?.active_peer_identity === p.identity ? 'btn-danger' : 'btn-primary'} btn-sm`}
                    onClick={() => handleToggle(p.identity)}
                    disabled={activating === p.identity}
                  >
                    <i className={`fas ${activating === p.identity ? 'fa-spinner fa-spin' : isFwd && status?.active_peer_identity === p.identity ? 'fa-stop' : 'fa-play'}`} />
                    {activating === p.identity ? '' : isFwd && status?.active_peer_identity === p.identity ? 'Stop' : 'Start'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* ── Hotkeys ── */}
      {hotkeys.length > 0 && (
        <>
          <div className="section-title"><i className="fas fa-keyboard" /> Hotkeys</div>
          <table className="hotkey-table">
            <thead><tr><th>Action</th><th>Key</th><th>Valid</th></tr></thead>
            <tbody>
              {hotkeys.map((hk, i) => (
                <tr key={i}>
                  <td>{hk.label}</td>
                  <td><kbd>{hk.display}</kbd></td>
                  <td>
                    <i className={`fas ${hk.valid !== false ? 'fa-circle-check' : 'fa-circle-xmark'}`}
                      style={{ color: hk.valid !== false ? 'var(--green)' : 'var(--red)' }} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {/* ── Clipboard Settings Summary ── */}
      {status?.capture_region && (
        <>
          <div className="section-title"><i className="fas fa-crop" /> Capture Region</div>
          <p style={{ color: 'var(--text-dim)', fontSize: '.85rem', fontFamily: 'monospace' }}>
            x={status.capture_region.x} y={status.capture_region.y}
            {' '}w={status.capture_region.width} h={status.capture_region.height}
          </p>
        </>
      )}
    </div>
  )
}

function StatCard({ icon, label, value, color, bg, sub }) {
  return (
    <div className="stat-card">
      <div className="stat-icon" style={{ background: bg, color }}>
        <i className={`fas ${icon}`} />
      </div>
      <div className="stat-body">
        <div className="stat-label">{label}</div>
        <div className="stat-value" style={{ color }}>{value}</div>
        {sub && <div style={{ fontSize: '.72rem', color: 'var(--text-muted)', marginTop: 2 }}>{sub}</div>}
      </div>
    </div>
  )
}

function WorkerChip({ name, info }) {
  const ok = info?.alive !== false
  return (
    <div className={`worker-chip ${ok ? 'ok' : 'dead'}`}>
      <i className={`fas ${ok ? 'fa-circle-check' : 'fa-circle-exclamation'}`} />
      {name}
    </div>
  )
}
