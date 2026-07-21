import { useState } from 'react'
import * as api from '../api.js'

export default function DiagnosticsPanel({ status, onRefresh }) {
  const [report, setReport] = useState('')
  const [ok, setOk] = useState(null)
  const [problems, setProblems] = useState([])
  const [loading, setLoading] = useState(false)
  const [msg, setMsg] = useState(null)
  const [overlayLoading, setOverlayLoading] = useState(false)

  const overlay = status?.overlay || {}

  const handleOverlay = async (action, success) => {
    setOverlayLoading(true)
    setMsg(null)
    try {
      await action()
      await onRefresh?.()
      setMsg({ type: 'success', text: success })
    } catch (e) {
      setMsg({ type: 'error', text: `Overlay diagnostic failed: ${e.message}` })
    } finally { setOverlayLoading(false) }
  }

  const display = (value) => {
    if (value === true) return 'Yes'
    if (value === false) return 'No'
    if (value === null || value === undefined || value === '') return '–'
    return String(value)
  }

  const handleRun = async () => {
    setLoading(true)
    setMsg(null)
    setReport('')
    try {
      const r = await api.getDiagnostics()
      setReport(r.report || '')
      setOk(r.ok)
      setProblems(r.problems || [])
      if (!r.ok && r.problems?.length) {
        setMsg({ type: 'error', text: `${r.problems.length} problem(s) found` })
      } else {
        setMsg({ type: 'success', text: 'Diagnostics: all checks passed' })
      }
    } catch (e) {
      setMsg({ type: 'error', text: `Diagnostics failed: ${e.message}` })
    } finally { setLoading(false) }
  }

  const colorize = (line) => {
    if (/^FAIL|^ERROR|^\[FAIL\]|✗/.test(line)) return `<span class="fail">${line}</span>`
    if (/^WARN|^\[WARN\]|⚠/.test(line)) return `<span class="warn">${line}</span>`
    if (/^PASS|^\[OK\]|✓|✔/.test(line)) return `<span class="pass">${line}</span>`
    if (/^INFO|^\[INFO\]/.test(line)) return `<span class="info">${line}</span>`
    return line
  }

  return (
    <div>
      <div className="page-title">
        <i className="fas fa-stethoscope" /> Diagnostics
        <span className="sub">System health check</span>
      </div>

      {msg && (
        <div className={`msg-box ${msg.type || 'info'}`}>
          <i className={`fas ${msg.type === 'error' ? 'fa-circle-exclamation' : 'fa-circle-check'}`} />
          {msg.text}
        </div>
      )}

      <div className="settings-group" style={{ marginBottom: 16 }}>
        <h3><i className="fas fa-window-restore" /> Overlay Host Foundation</h3>
        <p style={{ fontSize: '.78rem', color: 'var(--text-dim)', marginBottom: 10 }}>
          Host lifecycle diagnostics only. Clipboard and command wheel views are placeholders, not feature UI.
        </p>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: '6px 14px', fontSize: '.78rem', marginBottom: 12 }}>
          <span>Enabled: <strong>{display(overlay.enabled)}</strong></span>
          <span>Process alive: <strong>{display(overlay.process_alive)}</strong></span>
          <span>IPC: <strong>{display(overlay.ipc_connected)}</strong></span>
          <span>Ready: <strong>{display(overlay.ready)}</strong></span>
          <span>Visible: <strong>{display(overlay.visible)}</strong></span>
          <span>Mode: <strong>{display(overlay.mode)}</strong></span>
          <span>Restart count: <strong>{display(overlay.restart_count)}</strong></span>
          <span>Last error: <strong>{display(overlay.last_error)}</strong></span>
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          <button className="btn btn-primary" disabled={overlayLoading} onClick={() => handleOverlay(() => api.showDiagnosticOverlay('clipboard'), 'Clipboard diagnostic placeholder shown')}>
            Show Clipboard Diagnostic
          </button>
          <button className="btn btn-primary" disabled={overlayLoading} onClick={() => handleOverlay(() => api.showDiagnosticOverlay('command_wheel'), 'Command wheel diagnostic placeholder shown')}>
            Show Command Wheel Diagnostic
          </button>
          <button className="btn btn-outline" disabled={overlayLoading} onClick={() => handleOverlay(api.hideDiagnosticOverlay, 'Overlay diagnostic hidden')}>
            Hide
          </button>
          <button className="btn btn-outline" disabled={overlayLoading} onClick={() => handleOverlay(api.pingOverlay, 'Overlay host ping succeeded')}>
            Ping
          </button>
        </div>
      </div>

      <div className="settings-group">
        <h3><i className="fas fa-microchip" /> Run Diagnostics</h3>
        <p style={{ fontSize: '.82rem', color: 'var(--text-dim)', marginBottom: 12 }}>
          Collects environment, configuration, and runtime state to identify issues.
        </p>
        <button className="btn btn-primary" onClick={handleRun} disabled={loading}>
          <i className={`fas ${loading ? 'fa-spinner fa-spin' : 'fa-flask'}`} />
          {loading ? 'Running…' : 'Run Diagnostics'}
        </button>

        {ok !== null && (
          <div style={{ marginTop: 12, display: 'flex', gap: 16, fontSize: '.85rem' }}>
            <span>Status: {ok ? <span style={{ color: 'var(--green)' }}><i className="fas fa-circle-check" /> All OK</span> : <span style={{ color: 'var(--red)' }}><i className="fas fa-circle-exclamation" /> Issues found</span>}</span>
            {problems.length > 0 && <span style={{ color: 'var(--orange)' }}>{problems.length} problem(s)</span>}
          </div>
        )}
      </div>

      {report && (
        <div className="settings-group">
          <h3><i className="fas fa-file-lines" /> Report</h3>
          <div
            className="diagnostics-report"
            dangerouslySetInnerHTML={{ __html: report.split('\n').map(colorize).join('\n') }}
          />
        </div>
      )}
    </div>
  )
}
