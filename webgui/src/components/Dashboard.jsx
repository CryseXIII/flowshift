import { useState, useEffect } from 'react'
import * as api from '../api.js'
import { HotkeyConflictBanner, HotkeyStatusList, CLIPBOARD_SLOTS, WHEEL_SLOTS } from './HotkeyStatus.jsx'

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
  const preview = previewCardText(status)

  return (
    <div>
      <div className="page-title">
        <i className="fas fa-gauge-high" /> Dashboard
        <span className="sub">{status?.device_name || '–'}</span>
      </div>

      <HotkeyConflictBanner status={status} />
      <WebGuiNotice webgui={status?.webgui} />
      <PeerUpdateNotice peers={peers} />
      <EdgeSwitchNotice edgeSwitching={status?.edge_switching} />
      {/* One hint per refused switch: the general notice covers it when present. */}
      {!status?.edge_switching?.last_failure && (
        <EdgeRejectionNotice rejection={status?.edge_switching?.last_rejection} />
      )}

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
          icon="fa-display" label="Remote Preview" testId="preview-card"
          value={preview.value}
          color={preview.warn ? 'var(--orange)' : 'var(--text-muted)'}
          bg={preview.warn ? 'rgba(243,156,18,.12)' : 'rgba(85,85,119,.12)'}
          sub={preview.sub}
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
                  <PeerUpdateState update={p.update} />
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
                    title={isFwd && status?.active_peer_identity === p.identity ? `Stop forwarding to ${p.name}` : `Start forwarding to ${p.name}`}
                  >
                    <i className={`fas ${activating === p.identity ? 'fa-spinner fa-spin' : isFwd && status?.active_peer_identity === p.identity ? 'fa-stop' : 'fa-play'}`} aria-hidden="true" />
                    {activating === p.identity ? 'Working…' : isFwd && status?.active_peer_identity === p.identity ? 'Stop' : 'Start'}
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
                  <td><HotkeyValidity hotkey={hk} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {/* ── Overlay hotkeys (clipboard overlay, command wheel) ── */}
      {status?.overlay_hotkeys?.entries?.length > 0 && (
        <>
          <div className="section-title"><i className="fas fa-keyboard" /> Overlay Hotkeys</div>
          <HotkeyStatusList status={status} slots={[...CLIPBOARD_SLOTS, ...WHEEL_SLOTS]} testId="dashboard-overlay-hotkeys" />
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

export function WebGuiNotice({ webgui }) {
  // The runtime compares this WebGUI with the program; a mismatch means the
  // WebGUI was not updated together with FlowShift.
  if (!webgui || webgui.ok) return null
  return (
    <div className="update-notice warning" role="alert" data-testid="webgui-notice">
      <i className="fas fa-triangle-exclamation" />{' '}
      <strong>WebGUI does not match FlowShift {webgui.app_version || ''}</strong>
      <div>{webgui.message}</div>
      {webgui.action && <div><span>{webgui.action}</span></div>}
    </div>
  )
}

// The runtime keeps the last edge switch that was triggered but dropped (no
// peer for the edge, connection failed, refused by the other PC).
const EDGE_FAILURE_SHOW_MS = 2 * 60 * 1000

export function EdgeSwitchNotice({ edgeSwitching, now = Date.now() }) {
  const failure = edgeSwitching?.last_failure
  if (!failure?.message || edgeSwitching?.active_session) return null
  if (failure.at && now - failure.at * 1000 > EDGE_FAILURE_SHOW_MS) return null
  return (
    <div className="update-notice warning" role="alert" data-testid="edge-switch-notice" data-reason={failure.reason}>
      <i className="fas fa-triangle-exclamation" />{' '}
      <strong>Edge switch did not happen</strong>
      <div>{failure.message}</div>
    </div>
  )
}

// Update of an older peer from this PC (peer_update.PeerUpdateMonitor): the
// runtime sends the phase and one plain line per peer.
const PEER_UPDATE_STYLE = {
  older: { icon: 'fa-circle-arrow-up', color: 'var(--orange)' },
  offered: { icon: 'fa-circle-arrow-up', color: 'var(--orange)' },
  transferring: { icon: 'fa-spinner fa-spin', color: 'var(--blue)' },
  interrupted: { icon: 'fa-pause', color: 'var(--orange)' },
  ready: { icon: 'fa-box', color: 'var(--blue)' },
  waiting: { icon: 'fa-hourglass-half', color: 'var(--blue)' },
  installing: { icon: 'fa-gear fa-spin', color: 'var(--blue)' },
  restarting: { icon: 'fa-rotate fa-spin', color: 'var(--blue)' },
  updated: { icon: 'fa-circle-check', color: 'var(--green)' },
  failed: { icon: 'fa-circle-xmark', color: 'var(--red)' },
  update_only: { icon: 'fa-triangle-exclamation', color: 'var(--orange)' },
}

const PEER_UPDATE_BUSY = ['transferring', 'waiting', 'installing', 'restarting']
// "Connected again, version Y" stays in the notice for ten minutes.
const PEER_UPDATE_BACK_MS = 10 * 60 * 1000

export function PeerUpdateState({ update }) {
  if (!update?.phase) return null
  const style = PEER_UPDATE_STYLE[update.phase] || { icon: 'fa-circle-info', color: 'var(--text-dim)' }
  return (
    <div data-testid="peer-update-state" data-phase={update.phase}
      style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 4, fontSize: '.75rem', color: style.color }}>
      <i className={`fas ${style.icon}`} aria-hidden="true" />
      <span>{update.text}</span>
    </div>
  )
}

// Short notice while an update of another PC runs, fails or just finished.
export function PeerUpdateNotice({ peers }) {
  const now = Date.now()
  const rows = (peers || []).filter((p) => p?.update && (PEER_UPDATE_BUSY.includes(p.update.phase) ||
    p.update.phase === 'failed' ||
    (p.update.phase === 'updated' && now - Date.parse(p.update.since || 0) < PEER_UPDATE_BACK_MS)))
  if (rows.length === 0) return null
  const failed = rows.some((p) => p.update.phase === 'failed')
  return (
    <div className={`update-notice ${failed ? 'warning' : ''}`} role="status" data-testid="peer-update-notice">
      {rows.map((p) => (
        <div key={p.identity}>
          <i className={`fas ${(PEER_UPDATE_STYLE[p.update.phase] || {}).icon || 'fa-circle-info'}`} aria-hidden="true" />{' '}
          <strong>{p.name}</strong>: {p.update.text}
        </div>
      ))}
    </div>
  )
}

// Why the other PC refused a switch at the screen edge, in plain words.
const EDGE_REJECTION_TEXT = {
  invalid_target: 'did not recognise itself as the target. Connect both PCs once (or click Ping on its profile); FlowShift then stores the device ID and the edge works. If it stays, update FlowShift on both PCs.',
  busy: 'was busy with another switch. Try again in a moment.',
  invalid_edge: 'got an unknown screen edge. Open Display Layout and save the edge again.',
  set_cursor_failed: 'could not move its mouse pointer to the edge (for example while a password prompt or the lock screen is shown).',
  update_maintenance: 'is installing an update right now. Try again when it is done.',
  timeout: 'did not answer in time. Check that FlowShift runs there and the network is up.',
}

export function edgeRejectionText(rejection) {
  if (!rejection?.reason) return ''
  const who = rejection.peer || 'The other PC'
  const edge = rejection.edge ? ` at the ${rejection.edge} edge` : ''
  const why = EDGE_REJECTION_TEXT[rejection.reason] || `refused it (${rejection.reason}).`
  return `Switching${edge} to ${who} did not work: ${who} ${why}`
}

export function EdgeRejectionNotice({ rejection }) {
  const text = edgeRejectionText(rejection)
  if (!text) return null
  return (
    <div className="update-notice warning" role="status" data-testid="edge-rejection-notice">
      <i className="fas fa-triangle-exclamation" aria-hidden="true" />{' '}
      {text}
      {rejection.at && <span className="hint"> ({rejection.at})</span>}
    </div>
  )
}

const PREVIEW_STATE_LABELS = {
  idle: 'Idle',
  requested: 'Requested',
  rejected: 'Rejected',
  streaming: 'Streaming',
  stopping: 'Stopping',
}

const PREVIEW_REASON_HINTS = {
  pairing_required: 'devices are not paired yet',
  preview_unavailable: 'devices are paired, the preview picture itself is not built in yet',
}

// Remote screen preview of the forwarding target. Stage 1 never shows an
// image: the other PC refuses every request, with pairing_required while the
// devices are not paired (no device ID in its device list) and
// preview_unavailable once they are.
export function previewCardText(status) {
  const state = status?.preview_state || 'idle'
  const reason = status?.preview_reason || ''
  const hint = PREVIEW_REASON_HINTS[reason]
  return {
    value: PREVIEW_STATE_LABELS[state] || state,
    sub: reason ? `${reason}${hint ? ` (${hint})` : ''}` : '',
    warn: state === 'rejected',
  }
}

// A hotkey without an action is simply not assigned: neutral, not red. Red
// only when an assigned action cannot run (e.g. its peer was removed).
export function HotkeyValidity({ hotkey }) {
  if (!hotkey?.action) {
    return <span data-testid="hotkey-unassigned" title="Not assigned" style={{ color: 'var(--text-muted)' }}>not assigned</span>
  }
  const ok = hotkey.valid !== false
  return (
    <i className={`fas ${ok ? 'fa-circle-check' : 'fa-circle-xmark'}`} role="img"
      aria-label={ok ? 'valid' : 'target not found'} title={ok ? 'Valid' : 'Target not found'}
      style={{ color: ok ? 'var(--green)' : 'var(--red)' }} />
  )
}

function StatCard({ icon, label, value, color, bg, sub, testId }) {
  return (
    <div className="stat-card" data-testid={testId}>
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
