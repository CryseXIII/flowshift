import { useEffect, useRef, useState } from 'react'
import * as api from '../api.js'

const ACTIVE_STATES = new Set([
  'checking', 'downloading', 'waiting_for_idle', 'install_handoff',
  'installing', 'restarting',
])

const STATE_LABELS = {
  idle: 'Idle',
  checking: 'Checking for updates',
  up_to_date: 'Up to date',
  update_available: 'Update available',
  downloading: 'Downloading update',
  downloaded: 'Ready to install',
  waiting_for_idle: 'Waiting for idle',
  install_handoff: 'Preparing installer',
  installing: 'Installing update',
  restarting: 'Restarting',
  error: 'Update error',
}

const DEFAULT_SETTINGS = {
  enabled: true,
  check_on_start: true,
  channel: 'stable',
  policy: 'notify',
}

function formatDate(value) {
  if (!value) return 'Never'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function formatMegabytes(value) {
  if (typeof value !== 'number' || value < 0) return null
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

function errorText(error) {
  if (!error) return null
  if (typeof error === 'string') return error
  return error.message || error.code || JSON.stringify(error)
}

export default function SoftwareUpdateSection() {
  const [update, setUpdate] = useState(null)
  const [draft, setDraft] = useState(DEFAULT_SETTINGS)
  const [operation, setOperation] = useState(null)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState(null)
  const pollingRef = useRef(false)
  const operationRef = useRef(null)
  const dirtyRef = useRef(false)
  const editVersionRef = useRef(0)
  const fastPollingRef = useRef(true)
  const mountedRef = useRef(false)

  const applyStatus = (payload) => {
    const next = payload?.update
    if (!next || typeof next !== 'object') throw new Error('Update status response is invalid')
    fastPollingRef.current = ACTIVE_STATES.has(next.state) || next.operation_active === true
    setUpdate(next)
    if (!dirtyRef.current) {
      setDraft({
        enabled: next.enabled,
        check_on_start: next.check_on_start,
        channel: next.channel,
        policy: next.policy,
      })
    }
    return next
  }

  const refresh = async () => {
    if (pollingRef.current) return null
    pollingRef.current = true
    try {
      const payload = await api.getUpdateStatus()
      if (!mountedRef.current) return null
      const next = applyStatus(payload)
      setMessage((current) => current?.kind === 'load-error' ? null : current)
      return next
    } catch (error) {
      if (mountedRef.current) {
        setMessage({ kind: 'load-error', type: 'error', text: `Could not load update status: ${error.message}` })
      }
      return null
    } finally {
      pollingRef.current = false
    }
  }

  useEffect(() => {
    mountedRef.current = true
    let idleSeconds = 10
    refresh()
    const timer = window.setInterval(() => {
      idleSeconds += 1
      if (fastPollingRef.current || idleSeconds >= 10) {
        idleSeconds = 0
        refresh()
      }
    }, 1000)
    return () => {
      mountedRef.current = false
      window.clearInterval(timer)
    }
  }, [])

  const changeSetting = (key, value) => {
    dirtyRef.current = true
    editVersionRef.current += 1
    setDraft((current) => ({ ...current, [key]: value }))
  }

  const runOperation = async (name, request) => {
    if (operationRef.current) return
    operationRef.current = name
    fastPollingRef.current = true
    setOperation(name)
    setMessage(null)
    try {
      const result = await request()
      if (!mountedRef.current) return
      setMessage({
        type: result.status === 'queued' ? 'success' : 'info',
        text: result.message || `${name} ${result.status}`,
      })
      await refresh()
    } catch (error) {
      if (mountedRef.current) setMessage({ type: 'error', text: error.message })
    } finally {
      operationRef.current = null
      if (mountedRef.current) setOperation(null)
    }
  }

  const saveSettings = async () => {
    if (saving) return
    const editVersion = editVersionRef.current
    setSaving(true)
    setMessage(null)
    try {
      await api.saveUpdateSettings(draft)
      if (!mountedRef.current) return
      if (editVersionRef.current === editVersion) dirtyRef.current = false
      setMessage({ type: 'success', text: 'Update settings saved.' })
      await refresh()
    } catch (error) {
      if (mountedRef.current) setMessage({ type: 'error', text: error.message })
    } finally {
      if (mountedRef.current) setSaving(false)
    }
  }

  const state = update?.state
  const authoritativeBusy = ACTIVE_STATES.has(state) || update?.operation_active === true
  const busy = Boolean(operation) || authoritativeBusy
  const knownUpdate = Boolean(
    update?.latest_version
    && update.latest_version !== update.current_version
    && (state === 'update_available' || state === 'error')
  )
  const canDownload = knownUpdate && !busy
  const canInstall = state === 'downloaded' && update?.can_install === true && !busy
  const progress = update?.progress
  const downloadedMb = formatMegabytes(progress?.bytes_downloaded)
  const totalMb = formatMegabytes(progress?.bytes_total)
  const speedMb = formatMegabytes(progress?.bytes_per_second)
  const lastError = errorText(update?.last_error)

  return (
    <section className="settings-group software-update" aria-labelledby="software-update-title">
      <h3 id="software-update-title"><i className="fas fa-arrows-rotate" /> Software Update</h3>

      {message && (
        <div className={`msg-box update-message ${message.type || 'info'}`} aria-live="polite">
          {message.text}
        </div>
      )}

      {!update ? (
        <p className="update-muted">Loading update status...</p>
      ) : (
        <>
          <div className="update-summary">
            <div><span>Installed</span><strong>{update.current_version || 'Unknown'}</strong></div>
            <div><span>Latest</span><strong>{update.latest_version || 'Not checked'}</strong></div>
            <div><span>Last check</span><strong>{formatDate(update.last_check_at)}</strong></div>
            <div><span>Status</span><strong>{STATE_LABELS[state] || state || 'Unknown'}</strong></div>
          </div>

          {state === 'waiting_for_idle' && (
            <div className="update-notice" aria-live="polite">
              <strong>Waiting for idle</strong>
              <span>Update downloaded. Installation will start when FlowShift is idle.</span>
            </div>
          )}

          {update.development_mode && (
            <div className="update-notice warning">
              <strong>Development checkout</strong>
              <span>Automatic installation unavailable in development checkout. Download remains available for verification.</span>
            </div>
          )}

          {progress && (state === 'downloading' || progress.bytes_downloaded > 0) && (
            <div className="update-progress" aria-label="Download progress">
              <div className="update-progress-heading">
                <span>{downloadedMb || `${progress.bytes_downloaded} bytes`}{totalMb ? ` / ${totalMb}` : ''}</span>
                {typeof progress.percentage === 'number' && <strong>{progress.percentage.toFixed(1)}%</strong>}
              </div>
              {typeof progress.percentage === 'number' && (
                <div className="transfer-bar-track">
                  <div className="transfer-bar-fill" style={{ width: `${Math.max(0, Math.min(100, progress.percentage))}%` }} />
                </div>
              )}
              <div className="update-progress-meta">
                {speedMb && <span>{speedMb}/s</span>}
                {typeof progress.eta_seconds === 'number' && <span>ETA {Math.ceil(progress.eta_seconds)}s</span>}
              </div>
            </div>
          )}

          {lastError && <div className="msg-box error update-error" aria-live="polite">{lastError}</div>}
          {update.recovery_notices?.map((notice, index) => (
            <div className="update-recovery" key={`${notice.code || 'notice'}-${index}`}>{notice.message || notice.code}</div>
          ))}

          <div className="update-settings-grid">
            <label>
              <span>Channel</span>
              <select value={draft.channel} onChange={(event) => changeSetting('channel', event.target.value)}>
                <option value="stable">Stable</option>
              </select>
            </label>
            <label>
              <span>Policy</span>
              <select value={draft.policy} onChange={(event) => changeSetting('policy', event.target.value)}>
                <option value="notify">Notify only</option>
                <option value="download">Download automatically</option>
                <option value="install">Download and install automatically</option>
              </select>
            </label>
            <label className="update-checkbox">
              <input type="checkbox" checked={draft.enabled} onChange={(event) => changeSetting('enabled', event.target.checked)} />
              <span>Enable automatic updates</span>
            </label>
            <label className="update-checkbox">
              <input type="checkbox" checked={draft.check_on_start} onChange={(event) => changeSetting('check_on_start', event.target.checked)} />
              <span>Check on start</span>
            </label>
          </div>

          <button className="btn btn-ghost btn-sm update-save" onClick={saveSettings} disabled={saving}>
            <i className={`fas ${saving ? 'fa-spinner fa-spin' : 'fa-floppy-disk'}`} />
            {saving ? 'Saving...' : 'Save Update Settings'}
          </button>

          {update.release_notes && (
            <div className="update-release-notes">
              <h4>Release notes</h4>
              <pre>{update.release_notes}</pre>
            </div>
          )}

          <div className="update-actions">
            <button className="btn btn-outline btn-sm" onClick={() => runOperation('check', api.checkForUpdates)} disabled={busy}>
              {operation === 'check' ? 'Checking...' : 'Check'}
            </button>
            <button className="btn btn-secondary btn-sm" onClick={() => runOperation('download', api.downloadUpdate)} disabled={!canDownload}>
              {operation === 'download' ? 'Downloading...' : 'Download'}
            </button>
            <button className="btn btn-primary btn-sm" onClick={() => runOperation('install', api.installUpdate)} disabled={!canInstall} title={update.blocked_reason || ''}>
              {operation === 'install' ? 'Installing...' : 'Install'}
            </button>
          </div>
        </>
      )}
    </section>
  )
}
