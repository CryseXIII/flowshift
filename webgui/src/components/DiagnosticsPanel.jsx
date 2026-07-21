import { useState } from 'react'
import * as api from '../api.js'

export default function DiagnosticsPanel() {
  const [report, setReport] = useState('')
  const [ok, setOk] = useState(null)
  const [problems, setProblems] = useState([])
  const [loading, setLoading] = useState(false)
  const [msg, setMsg] = useState(null)

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
