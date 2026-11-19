import { useState } from 'react'
import { MOD_LABELS, formatKeyCombo, captureVk } from '../hotkeyFormat.js'

// Overlay hotkey slots reported by the runtime in status.overlay_hotkeys
// (hotkey_status.py). A busy hotkey (Windows error 1409) never reaches
// FlowShift, so every slot shows whether it is active or what works instead.
export const CLIPBOARD_SLOTS = ['clipboard_overlay', 'clipboard_ctrl_win_v', 'clipboard_win_v']
export const WHEEL_SLOTS = ['command_wheel']

export function hotkeyConflicts(status) {
  const report = status?.overlay_hotkeys
  if (!report || !Array.isArray(report.entries)) return []
  return report.entries.filter((e) => !e.active)
}

// One line per registered slot: green "active" or the plain-text conflict message.
export function HotkeyStatusList({ status, slots, testId }) {
  const entries = (status?.overlay_hotkeys?.entries || []).filter((e) => slots.includes(e.slot))
  if (entries.length === 0) return null
  return (
    <ul className="hotkey-status" data-testid={testId} style={{ listStyle: 'none', padding: 0, margin: '6px 0', fontSize: '.8rem' }}>
      {entries.map((e) => (
        <li key={e.slot} style={{ color: e.active ? 'var(--green)' : 'var(--red)', marginBottom: 2 }}>
          <i className={`fas ${e.active ? 'fa-circle-check' : 'fa-triangle-exclamation'}`} />{' '}
          {e.active ? <><kbd>{e.hotkey}</kbd> active</> : e.message}
        </li>
      ))}
    </ul>
  )
}

// Dashboard banner listing every busy overlay hotkey.
export function HotkeyConflictBanner({ status }) {
  const conflicts = hotkeyConflicts(status)
  if (conflicts.length === 0) return null
  return (
    <div className="msg-box error" role="alert" data-testid="hotkey-conflict-banner">
      <i className="fas fa-keyboard" />{' '}
      <strong>Hotkey taken:</strong>
      <ul style={{ margin: '4px 0 0 18px', padding: 0 }}>
        {conflicts.map((e) => <li key={e.slot}>{e.message}</li>)}
      </ul>
      <a href="/settings">Change hotkeys in Settings</a>
    </div>
  )
}

// Modifier checkboxes plus a "press a key" field for a {mods, vk} hotkey.
export function HotkeyPicker({ value, onChange, name }) {
  const [capturing, setCapturing] = useState(false)
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
      <div className="hotkey-mods">
        {MOD_LABELS.map((m) => (
          <label key={m.bit} className="mod-chip">
            <input
              type="checkbox"
              aria-label={`${name} modifier ${m.label}`}
              checked={(value.mods & m.bit) !== 0}
              onChange={(e) => onChange({ ...value, mods: e.target.checked ? value.mods | m.bit : value.mods & ~m.bit })}
            />
            {m.label}
          </label>
        ))}
      </div>
      <div
        className="hotkey-key"
        role="button"
        aria-label={`${name} key`}
        tabIndex={0}
        onClick={() => setCapturing(true)}
        onKeyDown={(e) => {
          if (!capturing) return
          e.preventDefault()
          const vk = captureVk(e)
          if (vk) {
            onChange({ ...value, vk })
            setCapturing(false)
          }
        }}
      >
        {capturing
          ? <span style={{ color: 'var(--accent)' }}>Press a key…</span>
          : <kbd>{formatKeyCombo(value.mods, value.vk)}</kbd>}
      </div>
    </div>
  )
}
