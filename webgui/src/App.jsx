import { useState, useEffect, useCallback, useRef } from 'react'
import * as api from './api.js'
import ErrorBoundary from './components/ErrorBoundary.jsx'
import Dashboard from './components/Dashboard.jsx'
import ClipboardView from './components/ClipboardView.jsx'
import SettingsPanel from './components/SettingsPanel.jsx'
import DisplayConfig from './components/DisplayConfig.jsx'
import PeersPanel from './components/PeersPanel.jsx'
import DiagnosticsPanel from './components/DiagnosticsPanel.jsx'
import EventLog from './components/EventLog.jsx'

const TABS = [
  { id: 'dashboard', label: 'Dashboard', icon: 'fa-gauge-high' },
  { id: 'peers', label: 'Peers', icon: 'fa-network-wired' },
  { id: 'clipboard', label: 'Clipboard', icon: 'fa-clipboard-list' },
  { id: 'display', label: 'Display', icon: 'fa-table-cells' },
  { id: 'diagnostics', label: 'Diagnostics', icon: 'fa-stethoscope' },
  { id: 'log', label: 'Event Log', icon: 'fa-list' },
  { id: 'settings', label: 'Settings', icon: 'fa-sliders' },
]

export default function App() {
  const [tab, setTab] = useState('dashboard')
  const [status, setStatus] = useState(null)
  const [error, setError] = useState(null)
  const intervalRef = useRef(null)

  const fetchStatus = useCallback(async () => {
    try {
      const s = await api.getStatus()
      setStatus(s)
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }, [])

  useEffect(() => {
    fetchStatus()
    intervalRef.current = setInterval(fetchStatus, 3000)
    const unsub = api.subscribeSSE((ev) => {
      if (ev.type === 'status_update') fetchStatus()
    })
    return () => {
      clearInterval(intervalRef.current)
      unsub()
    }
  }, [fetchStatus])

  return (
    <div className="app">
      <Sidebar tab={tab} setTab={setTab} status={status} />
      <main className="main">
        {error && (
          <div className="error-banner">
            <i className="fas fa-exclamation-triangle" /> {error}
          </div>
        )}
        <ErrorBoundary name={TABS.find((t) => t.id === tab)?.label || tab}>
          {tab === 'dashboard' && <Dashboard status={status} onRefresh={fetchStatus} />}
          {tab === 'peers' && <PeersPanel status={status} onUpdated={fetchStatus} />}
          {tab === 'clipboard' && <ClipboardView status={status} onRefresh={fetchStatus} />}
          {tab === 'display' && <DisplayConfig status={status} onRefresh={fetchStatus} />}
          {tab === 'diagnostics' && <DiagnosticsPanel />}
          {tab === 'log' && <EventLog />}
          {tab === 'settings' && <SettingsPanel status={status} onUpdated={fetchStatus} />}
        </ErrorBoundary>
      </main>
    </div>
  )
}

function Sidebar({ tab, setTab, status }) {
  const isFwd = status?.forwarding_active
  const peer = status?.forwarding_target || '-'
  const deviceName = status?.device_name || '–'
  const isHealthy = status?.runtime_healthy !== false
  const isConnected = status?.network_connected

  return (
    <nav className="sidebar">
      <div className="sidebar-header">
        <i className="fas fa-arrows-left-right-to-line" />
        <span>FlowShift</span>
      </div>

      <div className="device-badge">
        <div className="device-name">
          <span className={`dot ${isConnected ? 'dot-green' : 'dot-gray'}`} />
          {deviceName}
        </div>
        <div className="device-meta">
          {isFwd ? (
            <><i className="fas fa-arrow-right" /> Forwarding → {peer}</>
          ) : (
            <><i className="fas fa-pause" /> Standby</>
          )}
        </div>
      </div>

      <div className="sidebar-nav">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={`sidebar-btn ${tab === t.id ? 'active' : ''}`}
            onClick={() => setTab(t.id)}
          >
            <span className="icon"><i className={`fas ${t.icon}`} /></span>
            <span>{t.label}</span>
          </button>
        ))}
      </div>

      <div className="sidebar-footer">
        <div className="status-row">
          <i className={`fas fa-circle ${isHealthy ? 'text-green' : 'text-red'}`} style={{ fontSize: '.55rem', color: isHealthy ? 'var(--green)' : 'var(--red)' }} />
          <span>{isHealthy ? 'System OK' : 'Degraded'}</span>
        </div>
        <div className="version">v{status?.app_version || '–'}</div>
      </div>
    </nav>
  )
}
