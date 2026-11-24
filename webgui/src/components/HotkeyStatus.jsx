import { useState } from 'react'
import { MOD_LABELS, formatKeyCombo, captureVk } from '../hotkeyFormat.js'

// Overlay hotkey slots reported by the runtime in status.overlay_hotkeys
// (hotkey_status.py). A busy hotkey (Windows error 1409) never reaches
// FlowShift, so every slot shows whether it is active or what works instead.
export const CLIPBOARD_SLOTS = ['clipboard_overlay', 'clipboard_ctrl_win_v', 'clipboard_win_v']
export const WHEEL_SLOTS = ['command_wheel']
// Ctrl+Win+V is an extra shortcut FlowShift adds on its own; the clipboard
// overlay keeps working through the configured hotkey when it is taken.
const EXTRA_SLOTS = ['clipboard_ctrl_win_v']

// How one overlay hotkey entry is shown (red only for real, active problems):
//   'ok'       registered and working
//   'off'      its function is switched off in the settings: not shown
//   'notice'   only the extra Ctrl+Win+V shortcut is taken: neutral hint
//   'conflict' the hotkey of a switched-on function does not work: red
// `settings` is the saved/edited settings object of the Settings page
// (clipboard `enabled`, `intercept_win_v`); without it the runtime report
// decides, which only lists hotkeys of switched-on functions.
export function hotkeyEntryLevel(entry, settings = null) {
  if (!entry) return 'off'
  if (settings && CLIPBOARD_SLOTS.includes(entry.slot) && settings.enabled === false) return 'off'
  if (settings && entry.slot === 'clipboard_win_v' && settings.intercept_win_v !== true) return 'off'
  if (entry.active) return 'ok'
  if (EXTRA_SLOTS.includes(entry.slot)) return 'notice'
  return 'conflict'
}

export function hotkeyConflicts(status, settings = null) {
  const report = status?.overlay_hotkeys
  if (!report || !Array.isArray(report.entries)) return []
  return report.entries.filter((e) => hotkeyEntryLevel(e, settings) === 'conflict')
}

function noticeText(entry, entries) {
  const working = entries.find((e) => e.slot === 'clipboard_overlay' && e.active)
  return `${entry.hotkey} (extra shortcut) is used by Windows or another program.`
    + (working ? ` ${working.hotkey} opens the clipboard overlay.` : '')
}

const LEVEL_STYLE = {
  ok: { color: 'var(--green)', icon: 'fa-circle-check' },
  notice: { color: 'var(--text-dim)', icon: 'fa-circle-info' },
  conflict: { color: 'var(--red)', icon: 'fa-triangle-exclamation' },
}

// One line per registered slot: green "active", a neutral hint, or the
// plain-text conflict message; slots of switched-off functions are left out.
export function HotkeyStatusList({ status, slots, testId, settings = null }) {
  const all = status?.overlay_hotkeys?.entries || []
  const rows = all
    .filter((e) => slots.includes(e.slot))
    .map((e) => ({ entry: e, level: hotkeyEntryLevel(e, settings) }))
    .filter((r) => r.level !== 'off')
  if (rows.length === 0) return null
  return (
    <ul className="hotkey-status" data-testid={testId} style={{ listStyle: 'none', padding: 0, margin: '6px 0', fontSize: '.8rem' }}>
      {rows.map(({ entry: e, level }) => (
        <li key={e.slot} data-level={level} style={{ color: LEVEL_STYLE[level].color, marginBottom: 2 }}>
          <i className={`fas ${LEVEL_STYLE[level].icon}`} aria-hidden="true" />{' '}
          {level === 'ok' && <><kbd>{e.hotkey}</kbd> active</>}
          {level === 'notice' && noticeText(e, all)}
          {level === 'conflict' && e.message}
        </li>
      ))}
    </ul>
  )
}

// Dashboard banner listing every overlay hotkey of a switched-on function
// that does not work (the extra Ctrl+Win+V shortcut is not part of it).
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
        title="Click, then press the new key"
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
