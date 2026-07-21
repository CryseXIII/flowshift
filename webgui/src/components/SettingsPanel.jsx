import { useState, useEffect, useCallback } from 'react'
import * as api from '../api.js'
import SoftwareUpdateSection from './SoftwareUpdateSection.jsx'

const MOD_CTRL = 1
const MOD_ALT = 2
const MOD_SHIFT = 4
const MOD_WIN = 8

const MOD_LABELS = [
  { bit: MOD_CTRL, label: 'Ctrl' },
  { bit: MOD_ALT,  label: 'Alt' },
  { bit: MOD_SHIFT, label: 'Shift' },
  { bit: MOD_WIN,  label: 'Win' },
]

const VK_NAMES = {
  0x08: 'Backspace', 0x09: 'Tab', 0x0D: 'Enter', 0x1B: 'Escape',
  0x20: 'Space', 0x21: 'PageUp', 0x22: 'PageDown', 0x23: 'End', 0x24: 'Home',
  0x25: 'Left', 0x26: 'Up', 0x27: 'Right', 0x28: 'Down',
  0x2D: 'Insert', 0x2E: 'Delete',
  0x30: '0', 0x31: '1', 0x32: '2', 0x33: '3', 0x34: '4',
  0x35: '5', 0x36: '6', 0x37: '7', 0x38: '8', 0x39: '9',
  0x41: 'A', 0x42: 'B', 0x43: 'C', 0x44: 'D', 0x45: 'E',
  0x46: 'F', 0x47: 'G', 0x48: 'H', 0x49: 'I', 0x4A: 'J',
  0x4B: 'K', 0x4C: 'L', 0x4D: 'M', 0x4E: 'N', 0x4F: 'O',
  0x50: 'P', 0x51: 'Q', 0x52: 'R', 0x53: 'S', 0x54: 'T',
  0x55: 'U', 0x56: 'V', 0x57: 'W', 0x58: 'X', 0x59: 'Y', 0x5A: 'Z',
  0x70: 'F1', 0x71: 'F2', 0x72: 'F3', 0x73: 'F4', 0x74: 'F5',
  0x75: 'F6', 0x76: 'F7', 0x77: 'F8', 0x78: 'F9', 0x79: 'F10',
  0x7A: 'F11', 0x7B: 'F12',
  0x90: 'NumLock', 0x91: 'ScrollLock',
  0xBD: '-', 0xBB: '=', 0xDB: '[', 0xDD: ']', 0xBC: ',', 0xBE: '.',
  0xBF: '/', 0xC0: '`', 0xDE: "'", 0xDC: '\\',
}

export default function SettingsPanel({ status, onUpdated }) {
  const [settings, setSettings] = useState({})
  const [hotkeys, setHotkeys] = useState([])
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState(null)
  const [loadError, setLoadError] = useState(null)
  const [keysaving, setKeysaving] = useState(null)
  const [autoStart, setAutoStart] = useState(false)
  const [autoStartLoading, setAutoStartLoading] = useState(false)
  const [injectText, setInjectText] = useState('')
  const [injectSending, setInjectSending] = useState(false)
  const [webguiPort, setWebguiPort] = useState(5000)
  const [restarting, setRestarting] = useState(false)

  useEffect(() => {
    api.getAutoStart().then((r) => { if (r && r.enabled !== undefined) setAutoStart(r.enabled) }).catch(() => {})
    api.getWebguiConfig().then((d) => { if (d?.config?.port) setWebguiPort(d.config.port) }).catch(() => {})
  }, [])

  const fetchSettings = useCallback(async () => {
    try {
      const s = await api.getSettings()
      if (s && typeof s === 'object') {
        setSettings(s)
        if (s.hotkeys && Array.isArray(s.hotkeys)) {
          setHotkeys(s.hotkeys.map(normalizeHotkey))
        }
      }
      setLoadError(null)
    } catch (e) {
      setLoadError(e.message)
    }
  }, [])

  useEffect(() => { fetchSettings() }, [fetchSettings])

  const set = (key, value) => setSettings((prev) => {
    if (!prev || typeof prev !== 'object') return { [key]: value }
    return { ...prev, [key]: value }
  })

  const handleSave = async (extra) => {
    setSaving(true)
    setMsg(null)
    try {
      const payload = { ...settings }
      if (extra) Object.assign(payload, extra)
      payload.hotkeys = hotkeys.map((h) => ({
        action: h.action, mods: h.mods, key: h.key, label: h.label,
      }))
      const result = await api.saveSettings(payload)
      if (result?.ok) {
        setMsg({ type: 'success', text: 'Settings saved.' })
      } else {
        setMsg({ type: 'error', text: result?.error || 'Save failed' })
      }
      if (onUpdated) onUpdated()
    } catch (e) {
      setMsg({ type: 'error', text: e.message || 'Connection failed' })
    } finally {
      setSaving(false)
    }
  }

  const updateHotkey = (i, patch) => {
    setHotkeys((prev) => {
      const next = [...prev]
      next[i] = { ...next[i], ...patch }
      return next
    })
  }

  const addHotkey = () => {
    setHotkeys((prev) => [...prev, { action: '', mods: MOD_CTRL | MOD_ALT, key: 0x00, label: 'New hotkey' }])
  }

  const removeHotkey = (i) => {
    setHotkeys((prev) => prev.filter((_, idx) => idx !== i))
  }

  const handleAutoStart = async (v) => {
    setAutoStartLoading(true)
    try {
      const r = await api.setAutoStart(v)
      if (r && r.enabled !== undefined) setAutoStart(r.enabled)
    } catch (e) {
      setMsg({ type: 'error', text: `Auto-start: ${e.message}` })
    } finally {
      setAutoStartLoading(false)
    }
  }

  const handleInjectText = async () => {
    if (!injectText.trim()) return
    setInjectSending(true)
    try {
      await api.injectType(injectText)
      setMsg({ type: 'success', text: `Injected ${injectText.length} characters` })
    } catch (e) {
      setMsg({ type: 'error', text: `Inject failed: ${e.message}` })
    } finally { setInjectSending(false) }
  }

  const handleShutdown = async () => {
    if (!confirm('Shutdown FlowShift?\nThis will terminate the application.')) return
    try {
      await api.shutdownApp()
      setMsg({ type: 'info', text: 'Shutting down…' })
    } catch (e) {
      setMsg({ type: 'error', text: `Shutdown failed: ${e.message}` })
    }
  }

  const handleWebguiPort = async () => {
    try {
      await api.setWebguiConfig({ port: webguiPort })
      setMsg({ type: 'success', text: `Web GUI port set to ${webguiPort}. Restart to apply.` })
    } catch (e) {
      setMsg({ type: 'error', text: `Failed to set port: ${e.message}` })
    }
  }

  const handleRestart = async () => {
    if (!confirm('Restart FlowShift?\nThe service will stop and restart automatically.')) return
    setRestarting(true)
    try {
      await api.restartService()
      setMsg({ type: 'info', text: 'Restarting…' })
    } catch (e) {
      setMsg({ type: 'error', text: `Restart failed: ${e.message}` })
      setRestarting(false)
    }
  }

  const peers = (status?.peers || [])

  return (
    <div className="settings-panel" style={{ maxWidth: 860 }}>
      <div className="page-title">
        <i className="fas fa-sliders" /> Settings
        <span className="sub">{status?.device_name || '–'}</span>
      </div>

      {msg && (
        <div className={`msg-box ${msg.type || 'info'}`}>
          <i className={`fas ${msg.type === 'error' ? 'fa-circle-exclamation' : 'fa-circle-check'}`} />
          {msg.text}
        </div>
      )}

      {loadError && (
        <div className="msg-box error">
          <i className="fas fa-triangle-exclamation" /> Could not load settings: {loadError}
        </div>
      )}

      {/* ── Device ── */}
      <div className="settings-group">
        <h3><i className="fas fa-microchip" /> Device</h3>
        <SettingField label="Device Name" value={(settings && settings.device_name) || ''} onChange={(v) => set('device_name', v)} />
        <SettingField label="Port" value={(settings && settings.port) || 45781} type="number" onChange={(v) => set('port', v)} />
      </div>

      {/* ── Clipboard ── */}
      <div className="settings-group">
        <h3><i className="fas fa-clipboard" /> Clipboard</h3>
        <ToggleRow label="Clipboard Sync Enabled" value={!settings || settings.enabled !== false} onChange={(v) => set('enabled', v)} hint="Enable clipboard capture and sync" />
        <ToggleRow label="Capture Plaintext alongside HTML" value={settings && settings.capture_plaintext_alongside_html === true} onChange={(v) => set('capture_plaintext_alongside_html', v)} hint="Also store plaintext when copying formatted HTML" />
        <SettingField label="History Max Items" value={(settings && settings.history_max_items) || 200} type="number" min={20} max={999} onChange={(v) => set('history_max_items', v)} hint="20–999" />
        <SettingField label="History Max Total (GB)" value={(settings && settings.history_max_total_gb) || 2} type="number" step={0.1} min={0.1} max={100} onChange={(v) => set('history_max_total_gb', v)} hint="0.1–100 GB" />
        <SettingField label="Max Auto-Transfer (MB)" value={(settings && settings.max_auto_transfer_mb) || 100} type="number" min={1} onChange={(v) => set('max_auto_transfer_mb', v)} hint="Items larger require manual download" />
        <ToggleRow label="Sync on Activate" value={settings && settings.sync_on_activate !== false} onChange={(v) => set('sync_on_activate', v)} hint="Sync clipboard manifest when forwarding activates" />
        <ToggleRow label="Manual Download Only" value={settings && settings.manual_only === true} onChange={(v) => set('manual_only', v)} hint="Never auto-transfer; manual download only" />
        <ToggleRow label="Intercept Win+V" value={settings && settings.intercept_win_v === true} onChange={(v) => set('intercept_win_v', v)} hint="Override Windows clipboard history with FlowShift" />
        <SelectRow label="Direction Mode" value={(settings && settings.direction_mode) || 'source_to_target'} onChange={(v) => set('direction_mode', v)} hint="Clipboard flow direction" options={[
          { value: 'source_to_target', label: 'Source → Target (clipboard one way while forwarding)' },
          { value: 'bidirectional_manual', label: 'Bidirectional (clipboard sync all peers always)' },
        ]} />
        <SelectRow label="Byte Unit" value={(settings && settings.byte_unit) || 'auto'} onChange={(v) => set('byte_unit', v)} options={[
          { value: 'auto', label: 'Auto' }, { value: 'byte', label: 'Bytes' },
          { value: 'KB', label: 'KB' }, { value: 'MB', label: 'MB' },
          { value: 'KiB', label: 'KiB' }, { value: 'MiB', label: 'MiB' },
        ]} />
        <SelectRow label="Thumbnail Size" value={(settings && settings.thumbnail_size) || 'mittel'} onChange={(v) => set('thumbnail_size', v)} options={[
          { value: 'klein', label: 'Small' }, { value: 'mittel', label: 'Medium' },
          { value: 'gross', label: 'Large' }, { value: 'custom', label: 'Custom' },
        ]} />
        {settings && settings.thumbnail_size === 'custom' && (
          <SettingField label="Thumbnail Custom PX" value={(settings && settings.thumbnail_custom_px) || 96} type="number" min={16} max={1024} onChange={(v) => set('thumbnail_custom_px', v)} />
        )}
      </div>

      {/* ── Transfer ── */}
      <div className="settings-group">
        <h3><i className="fas fa-cloud-arrow-down" /> Transfer</h3>
        <SettingField label="Max Parallel Transfers" value={(settings && settings.clipboard_transfer_max_parallel) || 1} type="number" min={1} max={8} onChange={(v) => set('clipboard_transfer_max_parallel', v)} hint="1–8" />
        <SettingField label="Max Retries" value={(settings && settings.clipboard_transfer_max_retries) !== undefined ? settings.clipboard_transfer_max_retries : 5} type="number" min={0} max={100} onChange={(v) => set('clipboard_transfer_max_retries', v)} />
        <SettingField label="Retry Delay (ms)" value={(settings && settings.clipboard_transfer_retry_delay_ms) || 500} type="number" min={0} max={60000} onChange={(v) => set('clipboard_transfer_retry_delay_ms', v)} />
        <SettingField label="Max Transfer KB/s" value={(settings && settings.clipboard_max_transfer_kib_per_sec) || 0} type="number" min={0} onChange={(v) => set('clipboard_max_transfer_kib_per_sec', v)} hint="0 = unlimited" />
        <SettingField label="Disk Assembler Threshold (MB)" value={(settings && settings.clipboard_disk_assembler_threshold_mb) || 32} type="number" min={1} onChange={(v) => set('clipboard_disk_assembler_threshold_mb', v)} />
        <SettingField label="RAM Zip Limit (MB)" value={(settings && settings.clipboard_ram_zip_limit_mb) || 256} type="number" min={1} onChange={(v) => set('clipboard_ram_zip_limit_mb', v)} />
        <SettingField label="Temp Cleanup Max Age (h)" value={(settings && settings.clipboard_temp_cleanup_max_age_hours) || 24} type="number" min={1} onChange={(v) => set('clipboard_temp_cleanup_max_age_hours', v)} />
        <SelectRow label="Zip Strategy" value={(settings && settings.zip_strategy) || 'auto'} onChange={(v) => set('zip_strategy', v)} options={[
          { value: 'auto', label: 'Auto' }, { value: 'never', label: 'Never' },
          { value: 'always_batch', label: 'Always batch' },
        ]} />
      </div>

      {/* ── Direction Display Layout ── */}
      <div className="settings-group">
        <h3><i className="fas fa-table-cells" /> Display Layout</h3>
        <p style={{ fontSize: '.8rem', color: 'var(--text-dim)', marginBottom: 8 }}>
          Configure on the <strong>Display</strong> tab. Auto-switch activates when mouse hits the screen edge.
        </p>
        <div style={{ fontSize: '.82rem' }}>
          {settings && settings.display_layout ? (
            <>
              <div style={{ marginBottom: 6 }}><strong>Enabled:</strong> {settings.display_layout.enabled ? 'Yes' : 'No'}</div>
              <div style={{ marginBottom: 6 }}><strong>Threshold / Inset:</strong> {settings.display_layout.threshold_px || 0}px / {settings.display_layout.inset_px || 0}px</div>
              <div style={{ marginBottom: 6 }}><strong>Cooldowns:</strong> {settings.display_layout.cooldown_ms || 0}ms / {settings.display_layout.return_cooldown_ms || 0}ms</div>
              {settings.display_layout.edges
                ? Object.entries(settings.display_layout.edges).map(([dir, edge]) => {
                    const ident = edge?.peer_identity || ''
                    const peer = peers.find((p) => p.identity === ident || p.name === ident)
                    const entry = edge?.target_entry_edge || '—'
                    return <div key={dir} style={{ marginBottom: 4 }}><strong>{dir}:</strong> {peer ? peer.name : ident || '—'} <span style={{ color: 'var(--text-muted)' }}>({entry})</span></div>
                  })
                : Object.entries(settings.display_layout).filter(([dir]) => ['north', 'south', 'east', 'west'].includes(dir)).map(([dir, ident]) => {
                    const peer = peers.find((p) => p.identity === ident || p.name === ident)
                    return <div key={dir} style={{ marginBottom: 4 }}><strong>{dir}:</strong> {peer ? peer.name : ident || '—'}</div>
                  })}
              {status?.edge_switching?.warnings && status.edge_switching.warnings.length > 0 && (
                <div style={{ color: 'var(--red)', marginTop: 6 }}>{status.edge_switching.warnings.join(' | ')}</div>
              )}
              {status?.edge_switching?.active_session && (
                <div style={{ marginTop: 6, color: 'var(--text-dim)' }}>
                  Active session: {status.edge_switching.active_session.role} {status.edge_switching.active_session.source_exit_edge}→{status.edge_switching.active_session.target_entry_edge}
                </div>
              )}
            </>
          ) : (
            <span style={{ color: 'var(--text-muted)' }}>Not configured</span>
          )}
        </div>
      </div>

      {/* ── Hotkeys ── */}
      <div className="settings-group">
        <h3><i className="fas fa-keyboard" /> Hotkeys</h3>
        <p style={{ fontSize: '.8rem', color: 'var(--text-dim)', marginBottom: 10 }}>
          Define keyboard shortcuts for forwarding actions. Click a key field and press the desired key.
        </p>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {hotkeys.map((hk, i) => (
            <HotkeyRow
              key={i}
              hotkey={hk}
              peers={peers}
              index={i}
              onUpdate={(patch) => updateHotkey(i, patch)}
              onRemove={() => removeHotkey(i)}
              onCapture={(vk) => updateHotkey(i, { key: vk })}
              capturing={keysaving === i}
              setCapturing={(v) => setKeysaving(v ? i : null)}
            />
          ))}
        </div>

        <button className="btn btn-outline btn-sm" onClick={addHotkey} style={{ marginTop: 10 }}>
          <i className="fas fa-plus" /> Add Hotkey
        </button>

        {hotkeys.length === 0 && (
          <p style={{ color: 'var(--text-muted)', fontSize: '.8rem', marginTop: 8 }}>
            No hotkeys defined. Default hotkeys will be used.
          </p>
        )}
      </div>

      {/* ── System Info ── */}
      {status && (
        <div className="settings-group">
          <h3><i className="fas fa-circle-info" /> System</h3>
          <table className="hotkey-table" style={{ fontSize: '.85rem' }}>
            <tbody>
              <tr><td className="info-label">Version</td><td>{status.app_version || '–'}</td></tr>
              <tr><td className="info-label">Git</td><td>{(status.git_branch || '?')} @ {((status.git_commit || '').slice(0, 8)) || '?'}</td></tr>
              <tr><td className="info-label">Device ID</td><td><code>{status.device_id || '–'}</code></td></tr>
              <tr><td className="info-label">OS</td><td>{status.os || '–'}</td></tr>
              <tr><td className="info-label">Capabilities</td><td>{(Array.isArray(status.capabilities) ? status.capabilities.join(', ') : '–')}</td></tr>
              <tr><td className="info-label">Protocol</td><td>{status.protocol_version || '–'}</td></tr>
              <tr><td className="info-label">Session</td><td>{status.session ? `${status.session.session_id} (${status.session.interactive ? 'interactive' : 'service'})` : '–'}</td></tr>
              <tr><td className="info-label">Runtime</td><td>{status.runtime_healthy ? <span style={{ color: 'var(--green)' }}><i className="fas fa-circle-check" /> Healthy</span> : <span style={{ color: 'var(--red)' }}><i className="fas fa-circle-exclamation" /> Degraded</span>}</td></tr>
              {status.critical_workers_down && status.critical_workers_down.length > 0 && (
                <tr><td className="info-label">Down Workers</td><td style={{ color: 'var(--red)' }}>{status.critical_workers_down.join(', ')}</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      <SoftwareUpdateSection />

      {/* ── Management ── */}
      <div className="settings-group">
        <h3><i className="fas fa-gears" /> Management</h3>
        <div className="setting-row">
          <div className="setting-label">Auto-start with Windows</div>
          <label className="toggle-switch">
            <input type="checkbox" checked={autoStart} disabled={autoStartLoading} onChange={(e) => handleAutoStart(e.target.checked)} />
            <span className="toggle-slider" />
          </label>
        </div>
        <div className="setting-row">
          <div className="setting-label">Shutdown FlowShift</div>
          <button className="btn btn-danger btn-sm" onClick={handleShutdown}>
            <i className="fas fa-power-off" /> Shutdown
          </button>
        </div>

        <div className="setting-row" style={{ borderBottom: 'none', flexDirection: 'column', alignItems: 'stretch' }}>
          <div className="setting-label" style={{ alignSelf: 'flex-start', marginBottom: 6 }}>Inject Text</div>
          <textarea
            rows={3}
            placeholder="Type text to inject on the remote machine…"
            value={injectText}
            onChange={(e) => setInjectText(e.target.value)}
            style={{ width: '100%', background: 'var(--bg-base)', border: '1px solid var(--border)', borderRadius: 4, color: 'var(--text)', padding: '8px 10px', fontSize: '.85rem', outline: 'none', resize: 'vertical', fontFamily: 'inherit' }}
          />
          <div style={{ display: 'flex', justifyContent: 'flex-start', marginTop: 6 }}>
            <button className="btn btn-secondary btn-sm" onClick={handleInjectText} disabled={injectSending || !injectText.trim()}>
              <i className={`fas ${injectSending ? 'fa-spinner fa-spin' : 'fa-keyboard'}`} />
              {injectSending ? 'Sending…' : 'Send'}
            </button>
          </div>
        </div>

        <div className="setting-row" style={{ borderBottom: 'none' }}>
          <div className="setting-label">Web GUI Port</div>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <input
              type="number" min={1024} max={65535}
              value={webguiPort}
              onChange={(e) => setWebguiPort(Number(e.target.value))}
              style={{ background: 'var(--bg-base)', border: '1px solid var(--border)', borderRadius: 4, color: 'var(--text)', padding: '6px 10px', fontSize: '.85rem', outline: 'none', width: 100 }}
            />
            <button className="btn btn-ghost btn-sm" onClick={handleWebguiPort}>
              <i className="fas fa-floppy-disk" /> Save Port
            </button>
            <button className="btn btn-secondary btn-sm" onClick={handleRestart} disabled={restarting}>
              <i className={`fas ${restarting ? 'fa-spinner fa-spin' : 'fa-rotate'}`} />
              {restarting ? 'Restarting…' : 'Restart Service'}
            </button>
          </div>
        </div>
      </div>

      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
        <button className="btn btn-primary btn-save" onClick={() => handleSave()} disabled={saving || loadError !== null}>
          <i className={`fas ${saving ? 'fa-spinner fa-spin' : 'fa-floppy-disk'}`} />
          {saving ? 'Saving…' : 'Save Settings'}
        </button>
      </div>
    </div>
  )
}

/* ── Helpers ── */

function vkName(vk) { if (!vk || vk === 0x00) return '—'; return VK_NAMES[vk] || (vk >= 0x30 && vk <= 0x39 ? String.fromCharCode(vk) : vk >= 0x41 && vk <= 0x5A ? String.fromCharCode(vk) : `0x${vk.toString(16).toUpperCase()}`) }

function formatKeyCombo(mods, key) {
  const parts = []
  if (mods & MOD_CTRL) parts.push('Ctrl')
  if (mods & MOD_ALT) parts.push('Alt')
  if (mods & MOD_SHIFT) parts.push('Shift')
  if (mods & MOD_WIN) parts.push('Win')
  const name = vkName(key)
  if (name) parts.push(name)
  return parts.join('+') || '–'
}

function normalizeHotkey(h) {
  if (typeof h.mods === 'number' && typeof h.key === 'number') return h
  return { action: h.action || '', mods: MOD_CTRL | MOD_ALT, key: 0x00, label: h.label || h.action || '' }
}

/* ── Subcomponents ── */

function HotkeyRow({ hotkey, peers, index, onUpdate, onRemove, onCapture, capturing, setCapturing }) {
  const actionOptions = [
    { value: '', label: '— Select action —' },
    ...peers.map((p) => ({ value: `forward_${p.identity}`, label: `Forward to ${p.name}` })),
    { value: 'return_local', label: 'Return to local' },
  ]

  return (
    <div className="hotkey-row">
      <div className="hotkey-num">{index + 1}</div>

      <div className="hotkey-action">
        <select value={hotkey.action} onChange={(e) => onUpdate({ action: e.target.value, label: e.target.options[e.target.selectedIndex]?.text || '' })}>
          {actionOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </div>

      <div className="hotkey-mods">
        {MOD_LABELS.map((m) => (
          <label key={m.bit} className="mod-chip">
            <input
              type="checkbox"
              checked={(hotkey.mods & m.bit) !== 0}
              onChange={(e) => onUpdate({ mods: e.target.checked ? hotkey.mods | m.bit : hotkey.mods & ~m.bit })}
            />
            {m.label}
          </label>
        ))}
      </div>

      <div className="hotkey-key" onClick={() => setCapturing(true)} onKeyDown={(e) => {
        if (capturing) {
          e.preventDefault()
          const vk = e.keyCode || e.which
          if (vk >= 0x08) {
            onCapture(vk)
            setCapturing(false)
          }
        }
      }} tabIndex={0}>
        {capturing ? (
          <span style={{ color: 'var(--accent)' }}>Press a key…</span>
        ) : (
          <kbd>{formatKeyCombo(hotkey.mods, hotkey.key)}</kbd>
        )}
      </div>

      <div className="hotkey-label">
        <input type="text" value={hotkey.label} onChange={(e) => onUpdate({ label: e.target.value })} placeholder="Label" />
      </div>

      <button className="btn btn-outline-danger btn-sm" onClick={onRemove}>
        <i className="fas fa-xmark" /> Remove
      </button>
    </div>
  )
}

function SettingField({ label, value, type = 'text', onChange, hint, min, max, step }) {
  return (
    <div className="setting-row">
      <div className="setting-label">{label}{hint && <span className="hint">{hint}</span>}</div>
      <input
        type={type}
        value={value}
        min={min}
        max={max}
        step={step}
        onChange={(e) => onChange(type === 'number' ? Number(e.target.value) : e.target.value)}
      />
    </div>
  )
}

function ToggleRow({ label, value, onChange, hint }) {
  return (
    <div className="setting-row">
      <div className="setting-label">{label}{hint && <span className="hint">{hint}</span>}</div>
      <label className="toggle-switch">
        <input type="checkbox" checked={value} onChange={(e) => onChange(e.target.checked)} />
        <span className="toggle-slider" />
      </label>
    </div>
  )
}

function SelectRow({ label, value, options, onChange, hint }) {
  return (
    <div className="setting-row">
      <div className="setting-label">{label}{hint && <span className="hint">{hint}</span>}</div>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </div>
  )
}
