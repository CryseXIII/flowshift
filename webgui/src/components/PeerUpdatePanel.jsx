import { useEffect, useRef, useState } from 'react'
import * as api from '../api.js'

// Software Update > Updates from connected devices (Viktor 25.11.): when a
// connected FlowShift has a newer version it offers its program; this PC
// decides here. Default "Notify only": nothing is transferred before a click.
const STATE_TEXT = {
  idle: 'No connected device has a newer version.',
  available: 'A connected device offers a newer version.',
  downloading: 'Fetching the package from the other PC...',
  downloaded: 'Package fetched and checked; ready to install.',
  waiting: 'Installing soon: FlowShift restarts when no transfer, clipboard action or forwarding runs.',
  installing: 'Installing: FlowShift closes now and starts again by itself in about a minute.',
  failed: 'The update from the other PC was not installed.',
}

const ERROR_TEXT = {
  not_paired: 'The two PCs do not both have each other in their device list with the device ID (or the address answers with another device ID), so nothing was taken. Add the other PC with Scan Network under Peers on both PCs.',
  checksum_mismatch: 'The package did not arrive unchanged and was deleted. Nothing was installed.',
  file_damaged: 'A file in the package differs from its list of checksums. The package was deleted; nothing was installed.',
  unlisted_file: 'The package contains files that are not in its list. It was deleted; nothing was installed.',
  incomplete: 'The package is incomplete. It was deleted; nothing was installed.',
  unsafe_path: 'The package contains an unsafe file path. It was deleted; nothing was installed.',
  version_mismatch: 'The package is not the offered version. It was deleted; nothing was installed.',
  not_newer: 'The offered version is not newer than the installed one; FlowShift never installs an older version.',
  development_version_not_allowed: 'This PC follows Stable and does not take development versions from other PCs (see the tick below).',
  unreachable: 'The other PC could not be reached. FlowShift tries again the next time the two PCs connect.',
  interrupted: 'The transfer broke off. The next try continues where it stopped.',
  install_busy: 'Another FlowShift installation was running. Click the install button again later.',
  not_elevated: 'FlowShift runs without administrator rights. Sign out and in again, then click the install button.',
  no_offer: 'No connected device offers a newer version right now.',
  already_running: 'This already runs.',
  development_checkout: 'Not available in a development checkout.',
}

const POLICY_HINT = {
  notify: 'You see the offer here and decide with the download or install button. Nothing is transferred before that.',
  download: 'FlowShift fetches and checks the package by itself; you click the install button.',
  install: 'FlowShift fetches, checks and installs the newer version by itself and restarts, never during a transfer, a clipboard action or forwarding. You are told a minute before and can cancel.',
}

function formatSize(bytes) {
  if (typeof bytes !== 'number' || bytes <= 0) return null
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function formatTime(value) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

export default function PeerUpdatePanel() {
  const [info, setInfo] = useState(null)
  const [draft, setDraft] = useState(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState(null)
  const mountedRef = useRef(false)
  const dirtyRef = useRef(false)
  const activeRef = useRef(false)

  const apply = (next) => {
    if (!next) return
    activeRef.current = ['downloading', 'waiting', 'installing'].includes(next.state)
    setInfo(next)
    if (!dirtyRef.current) setDraft({ policy: next.policy, allow_development: next.allow_development })
  }

  const load = async () => {
    try {
      const payload = await api.getPeerUpdate()
      if (mountedRef.current) apply(payload?.peer_update)
    } catch (err) {
      if (mountedRef.current && !activeRef.current) setMessage({ type: 'error', text: `Could not load updates from connected devices: ${err.message}` })
    }
  }

  useEffect(() => {
    mountedRef.current = true
    let ticks = 0
    load()
    const timer = window.setInterval(() => {
      ticks += 1
      if (activeRef.current || ticks >= 10) {
        ticks = 0
        load()
      }
    }, 1000)
    return () => {
      mountedRef.current = false
      window.clearInterval(timer)
    }
  }, [])

  const change = (key, value) => {
    dirtyRef.current = true
    setDraft((current) => ({ ...current, [key]: value }))
  }

  const save = async () => {
    if (busy || !draft) return
    setBusy(true)
    setMessage(null)
    try {
      await api.savePeerUpdateSettings({ policy: draft.policy, allow_development: Boolean(draft.allow_development) })
      dirtyRef.current = false
      if (mountedRef.current) setMessage({ type: 'success', text: 'Setting for updates from connected devices saved.' })
      await load()
    } catch (err) {
      if (mountedRef.current) setMessage({ type: 'error', text: err.message })
    } finally {
      if (mountedRef.current) setBusy(false)
    }
  }

  const act = async (action) => {
    if (busy) return
    setBusy(true)
    setMessage(null)
    try {
      const result = await api.peerUpdateAction(action)
      if (mountedRef.current) apply(result?.peer_update)
    } catch (err) {
      if (mountedRef.current) setMessage({ type: 'error', text: ERROR_TEXT[err.message] || err.message })
    } finally {
      if (mountedRef.current) setBusy(false)
    }
  }

  if (!info || !draft) {
    return (
      <div className="update-notice" data-testid="peer-update">
        <strong>Updates from connected devices</strong>
        {message ? <span className={`msg-box ${message.type}`}>{message.text}</span> : <span>Loading...</span>}
      </div>
    )
  }

  const offer = info.offer
  const state = info.state
  const progress = info.progress
  const percent = progress && progress.total ? Math.min(100, (100 * progress.bytes) / progress.total) : null
  const running = ['downloading', 'waiting', 'installing'].includes(state)
  const last = info.last
  const error = info.error

  return (
    <div className="update-notice" data-testid="peer-update">
      <strong>Updates from connected devices</strong>
      <span>
        When this PC is connected to one of your devices with a newer FlowShift, that device offers its
        version. Only devices in this PC&apos;s device list count; the package is checked file by file
        before anything is installed, and FlowShift never installs an older version.
      </span>

      {message && <span className={`msg-box ${message.type}`} aria-live="polite">{message.text}</span>}

      <span data-testid="peer-update-state" aria-live="polite">{STATE_TEXT[state] || state}</span>
      {offer && (
        <span data-testid="peer-update-offer">
          {offer.peer_name} offers FlowShift <strong>{offer.version}</strong>
          {formatSize(offer.size) ? ` (${formatSize(offer.size)})` : ''}
          {offer.development ? ', a development version' : ''}; installed here: {info.installed_version}.
        </span>
      )}
      {info.unpaired_offer && !offer && (
        <span className="msg-box info" data-testid="peer-update-unpaired">
          {info.unpaired_offer.peer_name} has FlowShift {info.unpaired_offer.version}, but is not in this PC&apos;s
          device list with its device ID, so nothing is taken from it. To allow updates from it, add it with
          Scan Network under Peers (on both PCs).
        </span>
      )}
      {state === 'downloading' && percent !== null && (
        <div className="transfer-bar-track" aria-label="Transfer progress">
          <div className="transfer-bar-fill" style={{ width: `${percent}%` }} />
        </div>
      )}
      {state === 'waiting' && (
        <span data-testid="peer-update-waiting">
          {info.install_at ? `Planned from ${formatTime(info.install_at)}. ` : ''}
          {info.wait_reason ? `Waiting because: ${info.wait_reason}.` : ''}
        </span>
      )}
      {error && state === 'failed' && (
        <span className="msg-box error" data-testid="peer-update-error">{ERROR_TEXT[error.code] || error.message}</span>
      )}
      {last && (
        <span data-testid="peer-update-last">
          Last update from a connected device: {last.from_version} to {last.to_version} from {last.peer_name}
          {' '}({formatTime(last.handed_off_at)}){last.installed ? ', installed and running.' : last.result && last.result !== 'success' ? `, not installed (${last.result}); the previous version runs.` : '.'}
        </span>
      )}

      <div className="update-actions">
        <button type="button" className="btn btn-secondary btn-icon btn-touch" onClick={() => act('download')}
          disabled={busy || running || !offer || state === 'downloaded'}
          title="Download from the connected device"
          aria-label="Download from the connected device">
          <i className="fas fa-download" aria-hidden="true" />
        </button>
        <button type="button" className="btn btn-primary btn-icon btn-touch" onClick={() => act('install')}
          disabled={busy || running || !offer}
          title="Install from the connected device and restart FlowShift"
          aria-label="Install from the connected device and restart">
          <i className="fas fa-power-off" aria-hidden="true" />
        </button>
        {(state === 'downloading' || state === 'waiting') && (
          <button type="button" className="btn btn-ghost btn-icon btn-touch" onClick={() => act('cancel')} disabled={busy}
            title="Cancel the update from the connected device"
            aria-label="Cancel the update from the connected device">
            <i className="fas fa-xmark" aria-hidden="true" />
          </button>
        )}
      </div>

      <div className="update-settings-grid">
        <label>
          <span>Updates from connected devices</span>
          <select value={draft.policy} onChange={(event) => change('policy', event.target.value)} data-testid="peer-update-policy">
            <option value="notify">Notify only (recommended)</option>
            <option value="download">Download automatically</option>
            <option value="install">Download, install and restart automatically</option>
          </select>
        </label>
        <label className="update-checkbox">
          <input type="checkbox" checked={Boolean(draft.allow_development)}
            onChange={(event) => change('allow_development', event.target.checked)}
            data-testid="peer-update-development" />
          <span>Also accept development versions (only for your own PCs)</span>
        </label>
      </div>
      <span className="update-muted">{POLICY_HINT[draft.policy]}</span>
      {info.channel === 'development' && (
        <span className="update-muted">This PC follows Development, so development versions are accepted anyway.</span>
      )}
      <button type="button" className="btn btn-ghost btn-icon btn-touch update-save" onClick={save} disabled={busy}
        title="Save setting for connected devices"
        aria-label="Save setting for connected devices">
        <i className="fas fa-floppy-disk" aria-hidden="true" />
      </button>

      {info.log?.length > 0 && (
        <details className="update-peer-log">
          <summary>What happened ({info.log.length})</summary>
          <ul>
            {info.log.map((line, index) => (
              <li key={`${line.at}-${index}`}>{formatTime(line.at)}: {line.text}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}
