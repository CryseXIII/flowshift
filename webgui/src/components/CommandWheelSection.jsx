import { useState, useEffect, useCallback } from 'react'
import * as api from '../api.js'
import { MOD_LABELS, formatKeyCombo, captureVk } from '../hotkeyFormat.js'
import { HotkeyStatusList, WHEEL_SLOTS } from './HotkeyStatus.jsx'
import ButtonIcon from './ButtonIcon.jsx'

// Defaults mirror wheel_gesture.DEFAULT_TRIGGER (used when an old runtime
// answers without a trigger block).
export const DEFAULT_TRIGGER = { tap_open: true, hold_swipe: true, hold_ms: 250, mouse_button: 'none' }
const DEFAULT_TRIGGER_LIMITS = { hold_ms_min: 100, hold_ms_max: 2000 }
const MOUSE_BUTTON_LABELS = {
  none: 'Not used (keeps back/forward)',
  x1: 'Side button 1 (usually Back)',
  x2: 'Side button 2 (usually Forward)',
}

// Settings section for the Command Wheel: page/slot layout, open hotkey and
// how the wheel reacts to its trigger (tap to click, hold and swipe, side button).
// Loads from GET /api/actions and saves via POST /api/actions/wheel; the
// backend validates and re-registers the hotkey, the returned wheel is the
// new truth (so this section never keeps an unsaved layout after a save).
export default function CommandWheelSection({ status }) {
  const [actions, setActions] = useState([])
  const [limits, setLimits] = useState({ max_slots_per_page: 8, max_pages: 16 })
  const [pages, setPages] = useState(null)
  const [hotkey, setHotkey] = useState(null)
  const [trigger, setTrigger] = useState(DEFAULT_TRIGGER)
  const [triggerLimits, setTriggerLimits] = useState(DEFAULT_TRIGGER_LIMITS)
  const [savedHotkeyDisplay, setSavedHotkeyDisplay] = useState('')
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [capturing, setCapturing] = useState(false)
  const [msg, setMsg] = useState(null)
  const [loadError, setLoadError] = useState(null)

  const applyWheel = useCallback((wheel) => {
    setPages(wheel.pages.map((p) => [...p]))
    setHotkey({ mods: wheel.hotkey.mods, vk: wheel.hotkey.vk })
    setTrigger({ ...DEFAULT_TRIGGER, ...(wheel.trigger || {}) })
    if (wheel.trigger_limits) setTriggerLimits(wheel.trigger_limits)
    setSavedHotkeyDisplay(wheel.hotkey.display || formatKeyCombo(wheel.hotkey.mods, wheel.hotkey.vk))
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

  const toggleMod = (bit, on) => {
    setHotkey({ ...hotkey, mods: on ? hotkey.mods | bit : hotkey.mods & ~bit })
    setDirty(true)
    setMsg(null)
  }

  const updateTrigger = (patch) => {
    setTrigger({ ...trigger, ...patch })
    setDirty(true)
    setMsg(null)
  }

  const save = async () => {
    setSaving(true)
    setMsg(null)
    try {
      const body = { pages: pages.filter((p) => p.length > 0), hotkey, trigger }
      const r = await api.saveWheel(body)
      if (r?.ok && r.wheel) {
        applyWheel(r.wheel)
        setMsg({ ok: true, text: `Command wheel saved. Hotkey: ${r.wheel.hotkey.display}` })
      } else {
        setMsg({ ok: false, text: r?.message || r?.error || 'Save failed' })
      }
    } catch (e) {
      const texts = {
        hotkey_conflict: 'This hotkey already opens the clipboard overlay. Choose another one.',
        trigger_disabled: 'Keep at least one of "Tap to open" and "Hold and swipe" switched on.',
      }
      setMsg({ ok: false, text: texts[e.message] || e.message })
    } finally {
      setSaving(false)
    }
  }

  const totalSlots = pages ? pages.reduce((n, p) => n + p.length, 0) : 0
  const triggerUsable = trigger.tap_open || trigger.hold_swipe
  const holdMsValid = Number.isInteger(trigger.hold_ms)
    && trigger.hold_ms >= triggerLimits.hold_ms_min && trigger.hold_ms <= triggerLimits.hold_ms_max
  const canSave = dirty && !saving && totalSlots > 0 && hotkey && hotkey.mods !== 0
    && triggerUsable && holdMsValid

  return (
    <div className="settings-group" data-testid="command-wheel-section">
      <h3><i className="fas fa-circle-dot" /> Command Wheel</h3>
      <p style={{ fontSize: '.8rem', color: 'var(--text-dim)', marginBottom: 10 }}>
        Radial menu at the mouse pointer. Open it with Ctrl+Right click anywhere, the hotkey below or a mouse side button;
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

      {pages && hotkey && (
        <>
          <div className="setting-row">
            <div className="setting-label">Open hotkey<span className="hint">currently {savedHotkeyDisplay}</span></div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <div className="hotkey-mods">
                {MOD_LABELS.map((m) => (
                  <label key={m.bit} className="mod-chip">
                    <input
                      type="checkbox"
                      aria-label={`wheel modifier ${m.label}`}
                      checked={(hotkey.mods & m.bit) !== 0}
                      onChange={(e) => toggleMod(m.bit, e.target.checked)}
                    />
                    {m.label}
                  </label>
                ))}
              </div>
              <div
                className="hotkey-key"
                role="button"
                aria-label="wheel hotkey key"
                title="Click, then press the new key"
                tabIndex={0}
                onClick={() => setCapturing(true)}
                onKeyDown={(e) => {
                  if (!capturing) return
                  e.preventDefault()
                  const vk = captureVk(e)
                  if (vk) {
                    setHotkey({ ...hotkey, vk })
                    setDirty(true)
                    setMsg(null)
                    setCapturing(false)
                  }
                }}
              >
                {capturing
                  ? <span style={{ color: 'var(--accent)' }}>Press a key…</span>
                  : <kbd>{formatKeyCombo(hotkey.mods, hotkey.vk)}</kbd>}
              </div>
            </div>
          </div>
          <HotkeyStatusList status={status} slots={WHEEL_SLOTS} testId="wheel-hotkey-status" />

          <div className="setting-row">
            <div className="setting-label">Mouse side button<span className="hint">opens the wheel; hold and swipe works the same</span></div>
            <select aria-label="wheel mouse side button" value={trigger.mouse_button}
              onChange={(e) => updateTrigger({ mouse_button: e.target.value })}>
              {Object.entries(MOUSE_BUTTON_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </div>
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
            {msg && <span role="status" style={{ color: msg.ok ? 'var(--green)' : 'var(--red)', fontSize: '.8rem' }}>{msg.text}</span>}
          </div>
        </>
      )}
    </div>
  )
}
