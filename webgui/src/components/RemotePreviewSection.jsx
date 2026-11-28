import { useEffect, useState } from 'react'
import * as api from '../api.js'

// Remote screen preview of the PC you switch to: settings, the live state
// (what this PC and every connected PC can do, pictures per second), and the
// self-test of every step on this PC. Saved through /api/settings; the backend
// refuses invalid values with invalid_remote_preview and stores nothing.
export const PREVIEW_DEFAULTS = {
  enabled: false,
  fps: 15,
  max_width: 960,
  monitor: 'cursor',
  cursor_crop: false,
  window: { corner: 'bottom_right', width_px: 480 },
}

const CORNERS = [
  { value: 'top_left', label: 'Top left' },
  { value: 'top_right', label: 'Top right' },
  { value: 'bottom_left', label: 'Bottom left' },
  { value: 'bottom_right', label: 'Bottom right' },
]

const SAVE_ERRORS = {
  invalid_remote_preview: 'Not saved: a value is outside its range (frame rate 5–30, width 480–1920, window width 200–1200, monitor number 0 or higher).',
}

function fromSettings(value) {
  const v = value && typeof value === 'object' ? value : {}
  return {
    ...PREVIEW_DEFAULTS,
    ...v,
    window: { ...PREVIEW_DEFAULTS.window, ...(v.window && typeof v.window === 'object' ? v.window : {}) },
  }
}

// Monitor select: "cursor", "primary" or "number" with its own index field.
function monitorMode(monitor) {
  return typeof monitor === 'number' ? 'number' : monitor
}

// One plain sentence for the live state of the preview (status.preview).
export function previewStateText(preview) {
  if (!preview) return 'This FlowShift runtime reports no preview state (older version).'
  const peer = preview.peer || 'the other PC'
  if (preview.state === 'streaming') {
    const r = preview.receiving
    if (preview.notice) return `Preview of ${peer} open: ${preview.notice}.`
    if (!r || !r.frames) return `Preview of ${peer} open, waiting for the first picture.`
    return `Showing ${peer}: ${r.fps} pictures per second, ${r.kb_per_s} KB/s, ${r.frames} pictures so far.`
  }
  if (preview.state === 'requested') return `Asked ${peer} for its picture, no answer yet.`
  if (preview.state === 'rejected') return `No preview: ${preview.reason_text || preview.reason}.`
  if (preview.sending) {
    return `Sending this screen to ${preview.sending.peer}: ${preview.sending.sent} pictures, ${preview.sending.problem ? 'paused (screen locked or capture failed)' : 'running'}.`
  }
  return 'No preview open. It opens when you switch to a PC that offers it.'
}

// ``status`` is the live /api/status of the Settings page (App polls it).
export default function RemotePreviewSection({ status }) {
  const [draft, setDraft] = useState(null)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState(null)
  const [testing, setTesting] = useState(false)
  const [selfTest, setSelfTest] = useState(null)
  const [shown, setShown] = useState(null)

  useEffect(() => {
    let alive = true
    api.getSettings()
      .then((s) => { if (alive) setDraft(fromSettings(s?.remote_preview)) })
      .catch((e) => { if (alive) setMessage({ type: 'error', text: `Could not load remote preview settings: ${e.message}` }) })
    return () => { alive = false }
  }, [])

  if (!draft) {
    return (
      <section className="settings-group" aria-labelledby="remote-preview-title">
        <h3 id="remote-preview-title"><i className="fas fa-display" /> Remote Preview</h3>
        {message ? <div className="msg-box error">{message.text}</div> : <p className="update-muted">Loading...</p>}
      </section>
    )
  }

  const change = (key, value) => setDraft((current) => ({ ...current, [key]: value }))
  const changeWindow = (key, value) => setDraft((current) => ({ ...current, window: { ...current.window, [key]: value } }))

  const save = async () => {
    setSaving(true)
    setMessage(null)
    try {
      const result = await api.saveSettings({ remote_preview: draft })
      setDraft(fromSettings(result?.config?.remote_preview || draft))
      setMessage({ type: 'success', text: 'Remote preview settings saved.' })
    } catch (e) {
      setMessage({ type: 'error', text: SAVE_ERRORS[e.message] || e.message || 'Save failed' })
    } finally {
      setSaving(false)
    }
  }

  const runSelfTest = async () => {
    setTesting(true)
    setShown(null)
    try {
      setSelfTest(await api.remotePreviewSelfTest(3))
    } catch (e) {
      setSelfTest({ ok: false, steps: [], image: null, message: `Self-test failed: ${e.detail || e.message}` })
    } finally {
      setTesting(false)
    }
  }

  const mode = monitorMode(draft.monitor)

  return (
    <section className="settings-group" aria-labelledby="remote-preview-title">
      <h3 id="remote-preview-title"><i className="fas fa-display" /> Remote Preview</h3>
      <div className="update-notice warning" data-testid="preview-pairing-notice">
        <strong>Wirkt erst, wenn die Geräte gekoppelt sind und die Einstellung auf beiden PCs an ist. Gekoppelt ist ein PC, sobald er mit Geräte-ID in der Geräteliste steht (Scan Network, oder von Hand eingetragen und einmal verbunden). Beim Wechsel zum anderen PC erscheint dessen Bildschirm in einem kleinen Fenster in der gewählten Ecke; bei der Rückkehr verschwindet es. Maus und Tastatur bleiben beim anderen PC.</strong>
        <span>The other PC needs FlowShift 0.7.7 or newer. Why a preview does not open is written below, on the Dashboard and in the Event Log (filter Connection).</span>
      </div>

      {message && <div className={`msg-box ${message.type}`} aria-live="polite">{message.text}</div>}

      <PreviewLiveState status={status} enabled={draft.enabled} />

      <div className="setting-row">
        <div className="setting-label">Enable remote preview<span className="hint">ask the other PC for a preview after switching</span></div>
        <label className="toggle-switch">
          <input aria-label="Enable remote preview" type="checkbox" checked={draft.enabled} onChange={(e) => change('enabled', e.target.checked)} />
          <span className="toggle-slider" />
        </label>
      </div>
      <NumberRow label="Frame rate (fps)" hint="5–30" value={draft.fps} min={5} max={30} onChange={(v) => change('fps', v)} />
      <NumberRow label="Maximum width (px)" hint="480–1920" value={draft.max_width} min={480} max={1920} onChange={(v) => change('max_width', v)} />
      <div className="setting-row">
        <div className="setting-label">Monitor<span className="hint">which screen of the other PC</span></div>
        <select
          aria-label="Monitor"
          value={mode}
          onChange={(e) => change('monitor', e.target.value === 'number' ? 0 : e.target.value)}
        >
          <option value="cursor">Screen with the mouse</option>
          <option value="primary">Main screen</option>
          <option value="number">Screen number</option>
        </select>
      </div>
      {mode === 'number' && (
        <NumberRow label="Screen number" hint="0 = first screen" value={draft.monitor} min={0} onChange={(v) => change('monitor', v)} />
      )}
      <div className="setting-row">
        <div className="setting-label">Area around the mouse<span className="hint">show only a section around the mouse pointer</span></div>
        <label className="toggle-switch">
          <input aria-label="Area around the mouse" type="checkbox" checked={draft.cursor_crop} onChange={(e) => change('cursor_crop', e.target.checked)} />
          <span className="toggle-slider" />
        </label>
      </div>
      <div className="setting-row">
        <div className="setting-label">Window corner<span className="hint">where the preview window appears</span></div>
        <select aria-label="Window corner" value={draft.window.corner} onChange={(e) => changeWindow('corner', e.target.value)}>
          {CORNERS.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
        </select>
      </div>
      <NumberRow label="Window width (px)" hint="200–1200" value={draft.window.width_px} min={200} max={1200} onChange={(v) => changeWindow('width_px', v)} />

      <button className="btn btn-primary btn-sm" onClick={save} disabled={saving} title="Save the remote preview settings">
        <i className={`fas ${saving ? 'fa-spinner fa-spin' : 'fa-floppy-disk'}`} /> {saving ? 'Saving...' : 'Save Remote Preview'}
      </button>

      <div className="setting-row" style={{ marginTop: 12 }}>
        <div className="setting-label">Self-test on this PC<span className="hint">capture, encoding, transfer through the connection code (loopback), the preview window for 2 seconds and the picture here; no second PC needed</span></div>
        <button className="btn btn-secondary btn-sm" onClick={runSelfTest} disabled={testing} title="Check every step of the preview on this PC">
          <i className={`fas ${testing ? 'fa-spinner fa-spin' : 'fa-vial'}`} /> {testing ? 'Testing...' : 'Run self-test'}
        </button>
      </div>
      {selfTest && (
        <div data-testid="preview-self-test" className={`msg-box ${selfTest.ok ? 'info' : 'error'}`} style={{ flexDirection: 'column', alignItems: 'stretch' }}>
          <span>{selfTest.message}</span>
          <ul style={{ margin: '6px 0', paddingLeft: 18 }}>
            {(selfTest.steps || []).map((step) => {
              // "Shown" is only true once the browser has really drawn the picture.
              const display = step.step === 'display'
              const waiting = display && shown === null
              const ok = display ? shown === true : step.ok
              const detail = display && shown === false ? 'the browser could not show the picture' : step.detail
              const word = ok ? 'OK' : (waiting ? 'checking...' : 'failed')
              return (
                <li key={step.step} data-testid={`self-test-step-${step.step}`} style={{ color: ok ? 'var(--green)' : (waiting ? 'var(--text-muted)' : 'var(--red)') }}>
                  <strong>{step.label}:</strong> {word} – {detail}
                </li>
              )
            })}
          </ul>
          {selfTest.image && (
            <img src={selfTest.image} alt="Self-test picture of this screen" data-testid="self-test-image"
              onLoad={() => setShown(true)} onError={() => setShown(false)}
              style={{ maxWidth: '100%', width: draft.window.width_px, border: '1px solid var(--border)', borderRadius: 6 }} />
          )}
        </div>
      )}
    </section>
  )
}

// What this PC and every connected PC can do right now, in plain words.
function PreviewLiveState({ status, enabled }) {
  if (!status) return null
  const preview = status.preview
  const peers = (status.peers || []).filter((p) => p.connected)
  const red = preview?.state === 'rejected' || (preview && !preview.can_send) || Boolean(preview?.window_error)
  return (
    <div data-testid="preview-live-state" className={`msg-box ${red ? 'error' : 'info'}`} style={{ flexDirection: 'column', alignItems: 'stretch' }}>
      <span data-testid="preview-state-text">{previewStateText(preview)}</span>
      <ul style={{ margin: '6px 0', paddingLeft: 18 }}>
        <li data-testid="preview-this-pc">
          <strong>This PC:</strong> {enabled ? 'setting on' : 'setting off (switch it on and save, on both PCs)'}
          {preview && (preview.can_send ? '; can send its picture' : `; cannot send its picture: ${preview.can_send_text}`)}
          {preview?.window_error ? `; the preview window cannot open: ${preview.window_error}` : ''}
        </li>
        {peers.length === 0 && <li data-testid="preview-no-peer">No other PC connected.</li>}
        {peers.map((p) => (
          <li key={p.identity} data-testid={`preview-peer-${p.identity}`}>
            <strong>{p.peer_label || p.name}:</strong> {p.remote_preview ? p.remote_preview.text : 'no answer about the preview'}
            {p.remote_version ? ` (FlowShift ${p.remote_version})` : ''}
          </li>
        ))}
      </ul>
    </div>
  )
}

function NumberRow({ label, hint, value, min, max, onChange }) {
  return (
    <div className="setting-row">
      <div className="setting-label">{label}{hint && <span className="hint">{hint}</span>}</div>
      <input
        aria-label={label}
        type="number"
        step={1}
        value={value}
        min={min}
        max={max}
        onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))}
      />
    </div>
  )
}
