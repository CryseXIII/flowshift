import { useEffect, useRef, useState } from 'react'
import {
  MOD_CTRL, MOD_LABELS, MOUSE_BUTTONS, MOUSE_LABELS, NEEDS_MODIFIER,
  captureVk, eventMods, formatTrigger, isMouseTrigger, mouseButtonOf, triggerProblem,
} from '../hotkeyFormat.js'
import ButtonIcon from './ButtonIcon.jsx'

// Overlay hotkey slots reported by the runtime in status.overlay_hotkeys
// (hotkey_status.py). A busy hotkey (Windows error 1409) never reaches
// FlowShift, so every slot shows whether it is active or what works instead.
export const CLIPBOARD_SLOTS = ['clipboard_overlay', 'clipboard_ctrl_win_v', 'clipboard_win_v']
export const WHEEL_SLOTS = ['command_wheel', 'command_wheel_extra_1', 'command_wheel_extra_2', 'command_wheel_extra_3']
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

// One hotkey field for every hotkey of FlowShift: a key or a mouse button,
// each with modifiers (shapes of hotkeyFormat.js / input_trigger.py), or empty.
// Click the field, then press the combination: keys, left/right/middle click,
// side buttons and the mouse wheel are recorded with the held Ctrl/Alt/Shift/Win.
// A plain left or right click or wheel is refused with a hint (the computer
// would be unusable); Escape ends the recording. The list next to the field
// picks a mouse button without pressing it (useful when FlowShift itself
// already catches that combination), "Clear" empties the hotkey.
export function TriggerPicker({ value, onChange, name, keyNeedsModifier = true, allowClear = true }) {
  const [capturing, setCapturing] = useState(false)
  const [note, setNote] = useState(null)
  const fieldRef = useRef(null)
  const justCaptured = useRef(false)
  const problem = triggerProblem(value, { keyNeedsModifier })

  const finish = (trigger) => {
    onChange(trigger)
    setCapturing(false)
    setNote(null)
  }

  // React's onWheel is passive; preventDefault (no page zoom/scroll) needs a native listener.
  useEffect(() => {
    const el = fieldRef.current
    if (!el || !capturing) return undefined
    const onWheel = (e) => {
      e.preventDefault()
      const mods = eventMods(e)
      const mouse = e.deltaY < 0 ? 'wheel_up' : 'wheel_down'
      if (!mods) {
        setNote(`${MOUSE_LABELS[mouse]} alone cannot be a hotkey: hold Ctrl, Alt, Shift or Win while turning the wheel.`)
        return
      }
      finish({ mods, mouse })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  })

  const onMouseDown = (e) => {
    if (!capturing) return
    e.preventDefault()
    const mouse = mouseButtonOf(e)
    if (!mouse) return
    const mods = eventMods(e)
    if (!mods && NEEDS_MODIFIER.has(mouse)) {
      setNote(`${MOUSE_LABELS[mouse]} alone cannot be a hotkey: hold Ctrl, Alt, Shift or Win while clicking, or press Escape.`)
      return
    }
    finish({ mods, mouse })
    // Only a left button press is followed by a click on the field.
    justCaptured.current = mouse === 'left'
  }

  const onKeyDown = (e) => {
    if (!capturing) return
    e.preventDefault()
    const mods = eventMods(e)
    if (e.key === 'Escape' && !mods) {
      setCapturing(false)
      setNote(null)
      return
    }
    const vk = captureVk(e)
    if (!vk) return
    // Modifiers held with the key win; otherwise the ticked ones stay.
    finish({ mods: mods || (value ? value.mods : 0), vk })
  }

  // No context menu and no browser back/forward from a click on the field.
  const swallow = (e) => e.preventDefault()
  const onMouseUp = (e) => { if (e.button === 3 || e.button === 4 || capturing) e.preventDefault() }

  const chooseMouse = (mouse) => {
    setNote(null)
    if (!mouse) {
      onChange({ mods: value ? value.mods : MOD_CTRL, vk: 0 })
      setCapturing(true)
      return
    }
    onChange({ mods: value ? value.mods : MOD_CTRL, mouse })
  }

  return (
    <div className="trigger-picker" style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <div className="hotkey-mods">
          {MOD_LABELS.map((m) => (
            <label key={m.bit} className="mod-chip">
              <input
                type="checkbox"
                aria-label={`${name} modifier ${m.label}`}
                disabled={!value}
                checked={!!value && (value.mods & m.bit) !== 0}
                onChange={(e) => onChange({ ...value, mods: e.target.checked ? value.mods | m.bit : value.mods & ~m.bit })}
              />
              {m.label}
            </label>
          ))}
        </div>
        <div
          ref={fieldRef}
          className="hotkey-key"
          role="button"
          aria-label={`${name} key`}
          title="Click, then press a key or a mouse button (hold Ctrl, Alt, Shift or Win with it). Escape stops."
          tabIndex={0}
          onClick={() => {
            if (justCaptured.current) { justCaptured.current = false; return }
            if (!capturing) { setCapturing(true); setNote(null) }
          }}
          onKeyDown={onKeyDown}
          onMouseDown={onMouseDown}
          onMouseUp={onMouseUp}
          onAuxClick={swallow}
          onContextMenu={swallow}
          onBlur={() => { setCapturing(false); justCaptured.current = false }}
        >
          {capturing
            ? <span style={{ color: 'var(--accent)' }}>Press a key or mouse button…</span>
            : value
              ? <kbd>{formatTrigger(value)}</kbd>
              : <span style={{ color: 'var(--text-dim)' }}>Not set</span>}
        </div>
        <select aria-label={`${name} mouse button`} title="Choose a mouse button without pressing it"
          value={isMouseTrigger(value) ? value.mouse : ''} onChange={(e) => chooseMouse(e.target.value)}>
          <option value="">Key (press it in the field)</option>
          {MOUSE_BUTTONS.map((b) => <option key={b} value={b}>{MOUSE_LABELS[b]}</option>)}
        </select>
        {allowClear && (
          <button type="button" className="btn btn-ghost btn-sm" aria-label={`clear ${name}`}
            title="Clear this hotkey: nothing is registered for it" disabled={!value}
            onClick={() => { setCapturing(false); setNote(null); onChange(null) }}>
            <ButtonIcon name="xmark" /> Clear
          </button>
        )}
      </div>
      {problem && <div role="alert" style={{ color: 'var(--red)', fontSize: '.8rem' }}>{problem}</div>}
      {!problem && note && <div style={{ color: 'var(--text-dim)', fontSize: '.8rem' }}>{note}</div>}
      {!problem && !note && isMouseTrigger(value) && (value.mouse === 'left' || value.mouse === 'right') && (
        <div style={{ color: 'var(--text-dim)', fontSize: '.8rem' }}>
          {formatTrigger(value)} is taken by FlowShift and no longer reaches other programs.
        </div>
      )}
    </div>
  )
}
