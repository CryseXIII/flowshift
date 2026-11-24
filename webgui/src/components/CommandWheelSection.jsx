import { useState, useEffect, useCallback, forwardRef, useImperativeHandle } from 'react'
import * as api from '../api.js'
import { formatTrigger, plainTrigger, triggerProblem } from '../hotkeyFormat.js'
import { HotkeyStatusList, TriggerPicker, WHEEL_SLOTS, hotkeyEntryLevel } from './HotkeyStatus.jsx'
import ButtonIcon from './ButtonIcon.jsx'

// Defaults mirror wheel_gesture.DEFAULT_TRIGGER (used when an old runtime
// answers without a trigger block).
export const DEFAULT_TRIGGER = { tap_open: true, hold_swipe: true, hold_ms: 250 }
const DEFAULT_TRIGGER_LIMITS = { hold_ms_min: 100, hold_ms_max: 2000 }

// Without a hotkey only the tray icon is left (hotkey_status.WHEEL_FALLBACK).
export const NO_HOTKEY_TEXT = 'No hotkey: no key or mouse button opens the command wheel; it opens only with Ctrl+Right click on the FlowShift tray icon.'
export const NO_HOTKEY_CONFIRM = 'Without a hotkey no key or mouse button opens the command wheel any more; it then opens only with Ctrl+Right click on the FlowShift tray icon.\n\nSave the command wheel without a hotkey?'

function pickTrigger(raw) {
  const t = { ...DEFAULT_TRIGGER }
  for (const key of Object.keys(DEFAULT_TRIGGER)) if (raw && raw[key] !== undefined) t[key] = raw[key]
  return t
}

// Confirmation after a successful save. `registration` (web_api.py) says
// whether Windows accepted each wheel hotkey; a refused one is saved anyway
// and the message names it and why it does not work yet.
export function savedMessage(r) {
  const shown = r.wheel.hotkey ? (r.wheel.hotkey.display || formatTrigger(r.wheel.hotkey)) : 'none'
  const reg = r.registration
  const inactive = (reg?.entries || [])
    .filter((e) => hotkeyEntryLevel(e) === 'conflict').map((e) => e.message).filter(Boolean)
  if (inactive.length > 0) {
    return { ok: false, text: `Command wheel saved (hotkey ${shown}), but not active: ${inactive.join(' ')}` }
  }
  if (reg?.pending) {
    return { ok: true, text: `Command wheel saved. Hotkey: ${shown}. FlowShift has not confirmed the hotkey yet; see the status below.` }
  }
  return { ok: true, text: `Command wheel saved. Hotkey: ${shown}` }
}

// Settings section for the Command Wheel: page/slot layout, its one hotkey
// (default Ctrl+Right click; a key or a mouse button with modifiers, or empty)
// and how the wheel reacts (tap to click, hold and swipe).
// Loads from GET /api/actions and saves via POST /api/actions/wheel, from its
// own button and from "Save Settings" (ref.saveIfChanged); the backend
// validates and re-registers the hotkey, the returned wheel is the new truth
// (so this section never keeps an unsaved layout after a save).
const CommandWheelSection = forwardRef(function CommandWheelSection({ status }, ref) {
  const [actions, setActions] = useState([])
  const [limits, setLimits] = useState({ max_slots_per_page: 8, max_pages: 16 })
  const [pages, setPages] = useState(null)
  const [hotkey, setHotkey] = useState(null)
  const [trigger, setTrigger] = useState(DEFAULT_TRIGGER)
  const [triggerLimits, setTriggerLimits] = useState(DEFAULT_TRIGGER_LIMITS)
  const [savedHotkeyDisplay, setSavedHotkeyDisplay] = useState('')
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState(null)
  const [loadError, setLoadError] = useState(null)

  const applyWheel = useCallback((wheel) => {
    setPages(wheel.pages.map((p) => [...p]))
    setHotkey(plainTrigger(wheel.hotkey))
    setTrigger(pickTrigger(wheel.trigger))
    if (wheel.trigger_limits) setTriggerLimits({ ...DEFAULT_TRIGGER_LIMITS, ...wheel.trigger_limits })
    setSavedHotkeyDisplay(wheel.hotkey ? (wheel.hotkey.display || formatTrigger(wheel.hotkey)) : '')
    setDirty(false)
  }, [])

  const load = useCallback(async () => {
    try {
      const d = await api.getActions()
      setActions(Array.isArray(d.actions) ? d.actions : [])
      if (d.limits) setLimits(d.limits)
      applyWheel(d.wheel)
      setLoadError(null)
    } catch (e) {
      setLoadError(e.message)
    }
  }, [applyWheel])

  useEffect(() => { load() }, [load])

  const labelOf = (id) => actions.find((a) => a.id === id)?.label || id

  const updatePages = (next) => { setPages(next); setDirty(true); setMsg(null) }

  const setSlot = (pi, si, value) => {
    const next = pages.map((p) => [...p])
    if (value === '') next[pi].splice(si, 1)
    else next[pi][si] = value
    updatePages(next)
  }
  const addSlot = (pi, value) => {
    if (!value) return
    const next = pages.map((p) => [...p])
    next[pi].push(value)
    updatePages(next)
  }
  const moveSlot = (pi, si, dir) => {
    const target = si + dir
    if (target < 0 || target >= pages[pi].length) return
    const next = pages.map((p) => [...p])
    const [item] = next[pi].splice(si, 1)
    next[pi].splice(target, 0, item)
    updatePages(next)
  }
  const addPage = () => updatePages([...pages, []])
  const removePage = (pi) => updatePages(pages.filter((_, i) => i !== pi))

  const changeHotkey = (value) => { setHotkey(value); setDirty(true); setMsg(null) }

  const updateTrigger = (patch) => {
    setTrigger({ ...trigger, ...patch })
    setDirty(true)
    setMsg(null)
  }

  const totalSlots = pages ? pages.reduce((n, p) => n + p.length, 0) : 0
  const triggerUsable = trigger.tap_open || trigger.hold_swipe
  const holdMsValid = Number.isInteger(trigger.hold_ms)
    && trigger.hold_ms >= triggerLimits.hold_ms_min && trigger.hold_ms <= triggerLimits.hold_ms_max
  const valid = !!pages && totalSlots > 0 && !triggerProblem(hotkey) && triggerUsable && holdMsValid
  const canSave = dirty && !saving && valid

  // Saves the wheel and returns the shown result ({ ok, text }; `cancelled`
  // when the user keeps the hotkey after the no-hotkey question).
  const save = async () => {
    if (!valid) {
      const r = { ok: false, text: 'Command wheel not saved: correct the fields marked red in the Command Wheel section.' }
      setMsg(r)
      return r
    }
    if (!hotkey && savedHotkeyDisplay && !window.confirm(NO_HOTKEY_CONFIRM)) {
      const r = { ok: false, cancelled: true, text: `Command wheel not saved: the hotkey stays ${savedHotkeyDisplay}.` }
      setMsg(r)
      return r
    }
    setSaving(true)
    setMsg(null)
    let result
    try {
      const body = {
        pages: pages.filter((p) => p.length > 0),
        hotkey: plainTrigger(hotkey),
        trigger,
      }
      const r = await api.saveWheel(body)
      if (r?.ok && r.wheel) {
        // The saved choice stays in the field even when Windows refuses it.
        applyWheel(r.wheel)
        result = savedMessage(r)
      } else {
        result = { ok: false, text: `Command wheel not saved: ${r?.message || r?.error || 'unknown error'}` }
      }
    } catch (e) {
      const texts = {
        hotkey_conflict: 'This hotkey is already used by another FlowShift function. Choose another one.',
        trigger_disabled: 'Keep at least one of "Tap to open" and "Hold and swipe" switched on.',
      }
      result = { ok: false, text: `Command wheel not saved: ${e.detail || texts[e.message] || e.message}` }
    } finally {
      setSaving(false)
    }
    setMsg(result)
    return result
  }

  // "Save Settings" saves the wheel too when it was changed (Viktor 24.11.:
  // the separate wheel button was not noticed, the change got lost).
  useImperativeHandle(ref, () => ({
    saveIfChanged: () => (dirty ? save() : Promise.resolve(null)),
  }))

  return (
    <div className="settings-group" data-testid="command-wheel-section">
      <h3><i className="fas fa-circle-dot" /> Command Wheel</h3>
      <p style={{ fontSize: '.8rem', color: 'var(--text-dim)', marginBottom: 10 }}>
        Radial menu at the mouse pointer. Open it with the hotkey below (a key or a mouse button, standard Ctrl+Right click);
        the mouse wheel flips through pages ({limits.max_slots_per_page} actions per page, up to {limits.max_pages} pages).
        Like in a game: hold the hotkey or button, move the mouse towards a field and let go to run it; let go in the
        middle to close without an action. A short tap opens the wheel for clicking instead.
      </p>

      {loadError && (
        <div className="msg-box error" style={{ marginBottom: 10 }}>
          Command wheel settings unavailable: {loadError}
          <button className="btn btn-ghost btn-sm" onClick={load} style={{ marginLeft: 8 }} title="Load the command wheel settings again">Retry</button>
        </div>
      )}

      {pages && (
        <>
          <div className="setting-row">
            <div className="setting-label">Open hotkey<span className="hint">{savedHotkeyDisplay ? `currently ${savedHotkeyDisplay}` : 'currently not set'}; click the field and press a key or mouse button</span></div>
            <TriggerPicker name="wheel hotkey" value={hotkey} onChange={changeHotkey} />
          </div>
          {!hotkey && (
            <div data-testid="wheel-no-hotkey" style={{ color: 'var(--text-dim)', fontSize: '.8rem' }}>
              {NO_HOTKEY_TEXT}
            </div>
          )}
          <HotkeyStatusList status={status} slots={WHEEL_SLOTS} testId="wheel-hotkey-status" />

          <div className="setting-row">
            <div className="setting-label">Tap to open<span className="hint">short press: the wheel stays open for clicking</span></div>
            <label className="toggle-switch">
              <input type="checkbox" aria-label="wheel tap to open" checked={trigger.tap_open}
                onChange={(e) => updateTrigger({ tap_open: e.target.checked })} />
              <span className="toggle-slider" />
            </label>
          </div>
          <div className="setting-row">
            <div className="setting-label">Hold and swipe<span className="hint">hold, move towards a field, let go: it runs</span></div>
            <label className="toggle-switch">
              <input type="checkbox" aria-label="wheel hold and swipe" checked={trigger.hold_swipe}
                onChange={(e) => updateTrigger({ hold_swipe: e.target.checked })} />
              <span className="toggle-slider" />
            </label>
          </div>
          <div className="setting-row">
            <div className="setting-label">Tap or hold after<span className="hint">shorter presses count as a tap ({triggerLimits.hold_ms_min}–{triggerLimits.hold_ms_max} ms)</span></div>
            <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <input type="number" aria-label="wheel hold threshold ms" style={{ width: 90 }}
                min={triggerLimits.hold_ms_min} max={triggerLimits.hold_ms_max} step={50}
                value={Number.isFinite(trigger.hold_ms) ? trigger.hold_ms : ''}
                onChange={(e) => updateTrigger({ hold_ms: e.target.value === '' ? NaN : Number(e.target.value) })} />
              <span style={{ fontSize: '.8rem', color: 'var(--text-dim)' }}>ms</span>
            </div>
          </div>
          {!triggerUsable && (
            <div role="alert" style={{ color: 'var(--red)', fontSize: '.8rem' }}>
              Keep at least one of &quot;Tap to open&quot; and &quot;Hold and swipe&quot; switched on.
            </div>
          )}
          {triggerUsable && !holdMsValid && (
            <div role="alert" style={{ color: 'var(--red)', fontSize: '.8rem' }}>
              The threshold must be between {triggerLimits.hold_ms_min} and {triggerLimits.hold_ms_max} ms.
            </div>
          )}

          {pages.map((page, pi) => (
            <div key={pi} className="wheel-page" data-testid={`wheel-page-${pi}`}
              style={{ border: '1px solid var(--border)', borderRadius: 6, padding: 10, marginTop: 10 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                <strong style={{ fontSize: '.85rem' }}>Page {pi + 1} <span style={{ color: 'var(--text-dim)' }}>({page.length}/{limits.max_slots_per_page})</span></strong>
                <button className="btn btn-outline-danger btn-sm" onClick={() => removePage(pi)} disabled={pages.length <= 1}
                  title={pages.length <= 1 ? 'The wheel keeps at least one page' : `Remove page ${pi + 1}`}>
                  <ButtonIcon name="xmark" /> Remove page
                </button>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {page.map((id, si) => (
                  <div key={`${pi}-${si}`} className="hotkey-row wheel-slot-row" style={{ alignItems: 'center' }}>
                    <div className="hotkey-num">{si + 1}</div>
                    <select className="wheel-slot-select" aria-label={`page ${pi + 1} slot ${si + 1}`} title="Action in this field; choose “Remove” to take it out"
                      value={id} onChange={(e) => setSlot(pi, si, e.target.value)}>
                      {actions.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
                      <option value="">— Remove —</option>
                    </select>
                    <button className="btn btn-ghost btn-sm wheel-move-btn" aria-label={`move ${labelOf(id)} up`} title={`Move ${labelOf(id)} one field up`}
                      onClick={() => moveSlot(pi, si, -1)} disabled={si === 0}>
                      <ButtonIcon name="arrow-up" /> Up
                    </button>
                    <button className="btn btn-ghost btn-sm wheel-move-btn" aria-label={`move ${labelOf(id)} down`} title={`Move ${labelOf(id)} one field down`}
                      onClick={() => moveSlot(pi, si, 1)} disabled={si === page.length - 1}>
                      <ButtonIcon name="arrow-down" /> Down
                    </button>
                  </div>
                ))}
                {page.length < limits.max_slots_per_page && (
                  <select className="wheel-add-select" aria-label={`add action to page ${pi + 1}`} title={`Add an action to page ${pi + 1}`}
                    value="" onChange={(e) => addSlot(pi, e.target.value)}>
                    <option value="">+ Add action…</option>
                    {actions.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
                  </select>
                )}
              </div>
            </div>
          ))}

          <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginTop: 10, flexWrap: 'wrap' }}>
            <button className="btn btn-outline btn-sm" onClick={addPage} disabled={pages.length >= limits.max_pages} title="Add an empty page to the wheel">
              <i className="fas fa-plus" /> Add page
            </button>
            <button className="btn btn-primary btn-sm" onClick={save} disabled={!canSave} title="Save pages, hotkey and trigger of the command wheel">
              <i className={`fas ${saving ? 'fa-spinner fa-spin' : 'fa-floppy-disk'}`} /> {saving ? 'Saving…' : 'Save Command Wheel'}
            </button>
            {totalSlots === 0 && <span style={{ color: 'var(--red)', fontSize: '.8rem' }}>The wheel needs at least one action.</span>}
            {dirty && !saving && !msg && (
              <span data-testid="wheel-unsaved" style={{ color: 'var(--text-dim)', fontSize: '.8rem' }}>
                Not saved yet: click “Save Command Wheel” or “Save Settings”.
              </span>
            )}
            {msg && <span role="status" style={{ color: msg.ok ? 'var(--green)' : 'var(--red)', fontSize: '.8rem' }}>{msg.text}</span>}
          </div>
        </>
      )}
    </div>
  )
})

export default CommandWheelSection
