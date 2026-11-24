import { useEffect, useState } from 'react'
import * as api from '../api.js'

// Remote screen preview of the PC you switch to (stage 1: settings only).
// Saved through /api/settings; the backend refuses invalid values with
// invalid_remote_preview and stores nothing. No image is shown anywhere yet.
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

export default function RemotePreviewSection() {
  const [draft, setDraft] = useState(null)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState(null)

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

  const mode = monitorMode(draft.monitor)

  return (
    <section className="settings-group" aria-labelledby="remote-preview-title">
      <h3 id="remote-preview-title"><i className="fas fa-display" /> Remote Preview</h3>
      <div className="update-notice warning" data-testid="preview-pairing-notice">
        <strong>Wirkt erst, wenn die Geräte gekoppelt sind. Bis dahin wird die Vorschau abgelehnt.</strong>
        <span>Shows a small live picture of the other PC's screen after switching to it. Switch it on on both PCs; the Dashboard shows the answer of the other PC.</span>
      </div>

      {message && <div className={`msg-box ${message.type}`} aria-live="polite">{message.text}</div>}

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
    </section>
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
