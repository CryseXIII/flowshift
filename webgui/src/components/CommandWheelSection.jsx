import { useState, useEffect, useCallback } from 'react'
import * as api from '../api.js'
import { MOD_LABELS, formatKeyCombo, captureVk } from '../hotkeyFormat.js'

// Settings section for the Command Wheel: page/slot layout and open hotkey.
// Loads from GET /api/actions and saves via POST /api/actions/wheel; the
// backend validates and re-registers the hotkey, the returned wheel is the
// new truth (so this section never keeps an unsaved layout after a save).
export default function CommandWheelSection() {
  const [actions, setActions] = useState([])
  const [limits, setLimits] = useState({ max_slots_per_page: 8, max_pages: 16 })
  const [pages, setPages] = useState(null)
  const [hotkey, setHotkey] = useState(null)
  const [savedHotkeyDisplay, setSavedHotkeyDisplay] = useState('')
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [capturing, setCapturing] = useState(false)
  const [msg, setMsg] = useState(null)
  const [loadError, setLoadError] = useState(null)

  const applyWheel = useCallback((wheel) => {
    setPages(wheel.pages.map((p) => [...p]))
    setHotkey({ mods: wheel.hotkey.mods, vk: wheel.hotkey.vk })
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

  const save = async () => {
    setSaving(true)
    setMsg(null)
    try {
      const body = { pages: pages.filter((p) => p.length > 0), hotkey }
      const r = await api.saveWheel(body)
      if (r?.ok && r.wheel) {
        applyWheel(r.wheel)
        setMsg({ ok: true, text: `Command wheel saved. Hotkey: ${r.wheel.hotkey.display}` })
      } else {
        setMsg({ ok: false, text: r?.message || r?.error || 'Save failed' })
      }
    } catch (e) {
      setMsg({ ok: false, text: e.message })
    } finally {
      setSaving(false)
    }
  }

  const totalSlots = pages ? pages.reduce((n, p) => n + p.length, 0) : 0
  const canSave = dirty && !saving && totalSlots > 0 && hotkey && hotkey.mods !== 0

  return (
    <div className="settings-group" data-testid="command-wheel-section">
      <h3><i className="fas fa-circle-dot" /> Command Wheel</h3>
      <p style={{ fontSize: '.8rem', color: 'var(--text-dim)', marginBottom: 10 }}>
        Radial menu at the mouse pointer. Open it with Ctrl+Right click anywhere or with the hotkey below;
        the mouse wheel flips through pages ({limits.max_slots_per_page} actions per page, up to {limits.max_pages} pages).
      </p>

      {loadError && (
        <div className="alert alert-error" style={{ marginBottom: 10 }}>
          Command wheel settings unavailable: {loadError}
          <button className="btn btn-ghost btn-sm" onClick={load} style={{ marginLeft: 8 }}>Retry</button>
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

          {pages.map((page, pi) => (
            <div key={pi} className="wheel-page" data-testid={`wheel-page-${pi}`}
              style={{ border: '1px solid var(--border)', borderRadius: 6, padding: 10, marginTop: 10 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                <strong style={{ fontSize: '.85rem' }}>Page {pi + 1} <span style={{ color: 'var(--text-dim)' }}>({page.length}/{limits.max_slots_per_page})</span></strong>
                <button className="btn btn-outline-danger btn-sm" onClick={() => removePage(pi)} disabled={pages.length <= 1}>
                  <i className="fas fa-xmark" /> Remove page
                </button>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {page.map((id, si) => (
                  <div key={`${pi}-${si}`} className="hotkey-row" style={{ alignItems: 'center' }}>
                    <div className="hotkey-num">{si + 1}</div>
                    <select aria-label={`page ${pi + 1} slot ${si + 1}`} value={id} onChange={(e) => setSlot(pi, si, e.target.value)}>
                      {actions.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
                      <option value="">— Remove —</option>
                    </select>
                    <button className="btn btn-ghost btn-sm" aria-label={`move ${labelOf(id)} up`} onClick={() => moveSlot(pi, si, -1)} disabled={si === 0}><i className="fas fa-arrow-up" /></button>
                    <button className="btn btn-ghost btn-sm" aria-label={`move ${labelOf(id)} down`} onClick={() => moveSlot(pi, si, 1)} disabled={si === page.length - 1}><i className="fas fa-arrow-down" /></button>
                  </div>
                ))}
                {page.length < limits.max_slots_per_page && (
                  <select aria-label={`add action to page ${pi + 1}`} value="" onChange={(e) => addSlot(pi, e.target.value)}>
                    <option value="">+ Add action…</option>
                    {actions.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
                  </select>
                )}
              </div>
            </div>
          ))}

          <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginTop: 10, flexWrap: 'wrap' }}>
            <button className="btn btn-outline btn-sm" onClick={addPage} disabled={pages.length >= limits.max_pages}>
              <i className="fas fa-plus" /> Add page
            </button>
            <button className="btn btn-primary btn-sm" onClick={save} disabled={!canSave}>
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
