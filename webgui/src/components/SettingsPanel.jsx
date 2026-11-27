import { useState, useEffect, useCallback, useRef } from 'react'
import * as api from '../api.js'
import SoftwareUpdateSection from './SoftwareUpdateSection.jsx'
import CommandWheelSection from './CommandWheelSection.jsx'
import RemotePreviewSection from './RemotePreviewSection.jsx'
import ButtonIcon from './ButtonIcon.jsx'
import { TriggerPicker, HotkeyStatusList, CLIPBOARD_SLOTS, hotkeyEntryLevel } from './HotkeyStatus.jsx'
import { MOD_CTRL, MOD_ALT, triggerConflicts, triggerProblem } from '../hotkeyFormat.js'

const DEFAULT_CLIPBOARD_HOTKEY = { mods: MOD_CTRL | MOD_ALT, vk: 0x56 }
const SAVE_ERRORS = {
  hotkey_conflict: 'This hotkey is already used by another FlowShift function. Choose another one.',
  invalid_hotkey: 'Invalid hotkey: choose at least one modifier and a key or mouse button.',
  mouse_needs_modifier: 'A left or right click or the mouse wheel needs Ctrl, Alt, Shift or Win.',
}

// Blocks with their own save route (web_api.SETTINGS_FOREIGN_KEYS plus the
// remote preview section); "Save Settings" never posts them back. A changed
// command wheel is saved through its own route first (wheelRef).
const FOREIGN_KEYS = ['command_wheel', 'peers', 'display_layout', 'updates', 'remote_preview']

// Confirmation of "Save Settings": a clipboard overlay hotkey Windows refused
// stays saved, the message says why it does not work.
export function settingsSavedMessage(result) {
  // Only real problems: the extra Ctrl+Win+V shortcut being taken is no error.
  const inactive = (result?.registration?.entries || [])
    .filter((e) => hotkeyEntryLevel(e) === 'conflict').map((e) => e.message).filter(Boolean)
  if (inactive.length > 0) {
    return { type: 'error', text: `Settings saved, but not active: ${inactive.join(' ')}` }
  }
  return { type: 'success', text: 'Settings saved.' }
}

// Clipboard overlay hotkey of the loaded settings: null when cleared on purpose.
function clipboardHotkeyOf(settings) {
  if (settings && settings.overlay_hotkey === null) return null
  return (settings && settings.overlay_hotkey) || DEFAULT_CLIPBOARD_HOTKEY
}

// Peer hotkey row {action, mods, key, mouse} <-> trigger of the picker.
function rowTrigger(h) {
  if (h.mouse) return { mods: h.mods | 0, mouse: h.mouse }
  if (h.key) return { mods: h.mods | 0, vk: h.key }
  return null
}

function rowPatch(t) {
  if (!t) return { key: 0, mouse: undefined }
  if (t.mouse) return { mods: t.mods, key: 0, mouse: t.mouse }
  return { mods: t.mods, key: t.vk, mouse: undefined }
}

export default function SettingsPanel({ status, onUpdated }) {
  const [settings, setSettings] = useState({})
  const [hotkeys, setHotkeys] = useState([])
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState(null)
  const [loadError, setLoadError] = useState(null)
  const [autoStart, setAutoStart] = useState(false)
  const [autoStartLoading, setAutoStartLoading] = useState(false)
  const [autoStartNote, setAutoStartNote] = useState(null)
  const [injectText, setInjectText] = useState('')
  const [injectSending, setInjectSending] = useState(false)
  const [webguiPort, setWebguiPort] = useState(5000)
  const [restarting, setRestarting] = useState(false)
  const wheelRef = useRef(null)

  // Auto-start is the real Windows state (autostart.py), the same one the tray
  // tick shows; read again when this page comes back to the front, so a
  // switch in the tray menu shows here too.
  const readAutoStart = useCallback(() => {
    api.getAutoStart().then((r) => { if (r && r.enabled !== undefined) setAutoStart(r.enabled) }).catch(() => {})
  }, [])

  useEffect(() => {
    readAutoStart()
    api.getWebguiConfig().then((d) => { if (d?.config?.port) setWebguiPort(d.config.port) }).catch(() => {})
    const onVisible = () => { if (document.visibilityState !== 'hidden') readAutoStart() }
    window.addEventListener('focus', onVisible)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.removeEventListener('focus', onVisible)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [readAutoStart])

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
    // Viktor 24.11.: "Save Settings" must also keep a changed wheel hotkey.
    let wheel = null
    try {
      wheel = wheelRef.current ? await wheelRef.current.saveIfChanged() : null
    } catch (e) {
      wheel = { ok: false, text: `Command wheel not saved: ${e.message}` }
    }
    if (wheel?.cancelled) {
      setMsg({ type: 'info', text: wheel.text })
      setSaving(false)
      return
    }
    const withWheel = (m) => (wheel
      ? { type: wheel.ok && m.type !== 'error' ? m.type : 'error', text: `${wheel.text.replace(/\.?$/, '.')} ${m.text}` }
      : m)
    try {
      const payload = { ...settings }
      // Saved by their own sections/pages; a stale copy here would overwrite them.
      for (const key of FOREIGN_KEYS) delete payload[key]
      if (extra) Object.assign(payload, extra)
      payload.hotkeys = hotkeys.map((h) => ({
        action: h.action, mods: h.mods, key: h.mouse ? 0 : h.key, label: h.label,
        ...(h.mouse ? { mouse: h.mouse } : {}),
      }))
      const result = await api.saveSettings(payload)
      if (result?.ok) {
        setMsg(withWheel(settingsSavedMessage(result)))
      } else {
        setMsg(withWheel({ type: 'error', text: result?.message || result?.error || 'Save failed' }))
      }
      if (onUpdated) onUpdated()
    } catch (e) {
      setMsg(withWheel({ type: 'error', text: e.detail || SAVE_ERRORS[e.message] || e.message || 'Connection failed' }))
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
      // Takes effect at once in Windows; no "Save Settings" needed.
      const r = await api.setAutoStart(v)
      if (r && r.enabled !== undefined) setAutoStart(r.enabled)
      setAutoStartNote({ type: 'success', text: r?.enabled ? 'Auto-start with Windows is on.' : 'Auto-start with Windows is off.' })
    } catch (e) {
      setAutoStartNote({ type: 'error', text: e.message })
      readAutoStart()
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
  const clipboardHotkey = clipboardHotkeyOf(settings)
  // Same hotkey twice on this page (the API also checks the command wheel).
  const hotkeyConflicts = triggerConflicts([
    { label: 'Clipboard overlay', trigger: clipboardHotkey },
    ...hotkeys.map((h, i) => ({ label: h.label || `Hotkey ${i + 1}`, trigger: rowTrigger(h) })),
  ])
  const hotkeysInvalid = !!triggerProblem(clipboardHotkey)
    || hotkeys.some((h) => triggerProblem(rowTrigger(h), { keyNeedsModifier: false }))

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
        <ToggleRow label="Persist Clipboard History" value={!settings || settings.persist !== false} onChange={(v) => set('persist', v)} hint="Keep history across FlowShift restarts" />
        <ToggleRow label="Capture Plaintext alongside HTML" value={settings && settings.capture_plaintext_alongside_html === true} onChange={(v) => set('capture_plaintext_alongside_html', v)} hint="Also store plaintext when copying formatted HTML" />
        <SettingField label="History Max Items" value={(settings && settings.history_max_items) || 200} type="number" min={20} max={999} onChange={(v) => set('history_max_items', v)} hint="20–999" />
        <SettingField label="History Max Total (GB)" value={(settings && settings.history_max_total_gb) || 2} type="number" step={0.1} min={0.1} max={100} onChange={(v) => set('history_max_total_gb', v)} hint="0.1–100 GB" />
        <SettingField label="Max Auto-Transfer (MB)" value={(settings && settings.max_auto_transfer_mb) || 100} type="number" min={1} onChange={(v) => set('max_auto_transfer_mb', v)} hint="Items larger require manual download" />
        <SettingField label="Max Item (GB)" value={(settings && settings.max_item_gb) || 50} type="number" step={0.1} min={0.1} max={100000} onChange={(v) => set('max_item_gb', v)} hint="Hard limit for manual and automatic transfers" />
        <ToggleRow label="Sync on Activate" value={settings && settings.sync_on_activate !== false} onChange={(v) => set('sync_on_activate', v)} hint="Sync clipboard manifest when forwarding activates" />
        <ToggleRow label="Manual Download Only" value={settings && settings.manual_only === true} onChange={(v) => set('manual_only', v)} hint="Never auto-transfer; manual download only" />
        <div className="setting-row">
          <div className="setting-label">Overlay hotkey<span className="hint">opens the clipboard window at the mouse (a key or a mouse button); Ctrl+Win+V also works when free</span></div>
          <TriggerPicker
            name="clipboard hotkey"
            value={clipboardHotkey}
            onChange={(v) => set('overlay_hotkey', v)}
          />
        </div>
        <ToggleRow label="Intercept Win+V" value={settings && settings.intercept_win_v === true} onChange={(v) => set('intercept_win_v', v)} hint="Override Windows clipboard history with FlowShift" />
        <HotkeyStatusList status={status} slots={CLIPBOARD_SLOTS} settings={settings} testId="clipboard-hotkey-status" />
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
        {/* Preview values arrive normalised (defaults, ranges) from /api/settings;
            the backend clamps what is saved. */}
        <SelectRow label="Animated File Previews" value={settings?.file_preview_animation ?? ''} onChange={(v) => set('file_preview_animation', v)} hint="GIF, APNG and animated WebP on file cards" options={[
          { value: 'always', label: 'Always play' },
          { value: 'hover', label: 'Play on mouse hover' },
        ]} />
        <ToggleRow label="Video Preview on Hover" value={settings?.file_preview_video_hover === true} onChange={(v) => set('file_preview_video_hover', v)} hint="Short muted clip when the mouse is over a video" />
        <SettingField label="File Preview Cache (MB)" value={settings?.file_preview_cache_mb ?? ''} type="number" onChange={(v) => set('file_preview_cache_mb', v)} hint="Oldest previews are removed first" />
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
              <div style={{ marginBottom: 6 }}><strong>Re-arm after return:</strong> {settings.display_layout.rearm_distance_px ?? 40}px / {settings.display_layout.rearm_ms ?? 400}ms</div>
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
                // Red only while edge switching is on; switched off it is a neutral note.
                <div data-testid="settings-edge-warnings" data-level={status.edge_switching.enabled === false ? 'notice' : 'conflict'}
                  style={{ color: status.edge_switching.enabled === false ? 'var(--text-dim)' : 'var(--red)', marginTop: 6 }}>
                  {status.edge_switching.warnings.join(' | ')}
                </div>
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
          Hotkeys for switching to another PC and back. Click a field and press a key or a mouse button
          (with Ctrl, Alt, Shift or Win); Clear leaves a hotkey empty.
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
              runtime={(status?.hotkeys || [])[i]}
            />
          ))}
        </div>
        {hotkeyConflicts.map((c) => (
          <div key={c.hotkey} role="alert" style={{ color: 'var(--red)', fontSize: '.8rem', marginTop: 6 }}>{c.message}</div>
        ))}

        <button className="btn btn-outline btn-sm" onClick={addHotkey} style={{ marginTop: 10 }}>
          <i className="fas fa-plus" /> Add Hotkey
        </button>

        {hotkeys.length === 0 && (
          <p style={{ color: 'var(--text-muted)', fontSize: '.8rem', marginTop: 8 }}>
            No hotkeys defined. Default hotkeys will be used.
          </p>
        )}
      </div>

      <CommandWheelSection ref={wheelRef} status={status} />

      <RemotePreviewSection />

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
            <input type="checkbox" aria-label="Auto-start with Windows" checked={autoStart} disabled={autoStartLoading} onChange={(e) => handleAutoStart(e.target.checked)} />
            <span className="toggle-slider" />
          </label>
        </div>
        {autoStartNote && (
          <div className={`msg-box ${autoStartNote.type}`} data-testid="autostart-result" aria-live="polite">
            {autoStartNote.text}
          </div>
        )}
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
            aria-label="Inject Text"
            style={{ width: '100%', resize: 'vertical' }}
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
              aria-label="Web GUI Port"
              style={{ width: 100, minWidth: 0 }}
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
        <button className="btn btn-primary btn-save" onClick={() => handleSave()}
          disabled={saving || loadError !== null || hotkeyConflicts.length > 0 || hotkeysInvalid}>
          <i className={`fas ${saving ? 'fa-spinner fa-spin' : 'fa-floppy-disk'}`} />
          {saving ? 'Saving…' : 'Save Settings'}
        </button>
        {/* Repeated next to the button: the box at the top is out of view after scrolling. */}
        {msg && (
          <span role="status" data-testid="settings-save-result"
            style={{ color: { error: 'var(--red)', info: 'var(--text-dim)' }[msg.type] || 'var(--green)', fontSize: '.85rem' }}>
            {msg.text}
          </span>
        )}
      </div>
    </div>
  )
}

/* ── Helpers ── */

function normalizeHotkey(h) {
  if (typeof h.mods === 'number' && typeof h.key === 'number') return h
  return { action: h.action || '', mods: MOD_CTRL | MOD_ALT, key: 0x00, label: h.label || h.action || '' }
}

/* ── Subcomponents ── */

function HotkeyRow({ hotkey, peers, index, onUpdate, onRemove, runtime }) {
  // forward_peer:<identity> is the form the runtime knows (up to 0.7.5-dev.1
  // this list sent forward_<identity>, which the runtime refused).
  const actionOptions = [
    { value: '', label: '— Select action —' },
    ...peers.map((p) => ({ value: `forward_peer:${p.identity}`, label: `Forward to ${p.name}` })),
    { value: 'return_local', label: 'Return to local' },
  ]
  // A saved target that is no longer in the peer list stays visible (red below).
  if (hotkey.action && !actionOptions.some((o) => o.value === hotkey.action)) {
    actionOptions.splice(1, 0, { value: hotkey.action, label: 'Forward (PC no longer in the peer list)' })
  }
  // Red only when this saved hotkey really could not be registered (with the reason).
  const runtimeError = runtime && runtime.action === hotkey.action && runtime.active === false ? runtime.error : null

  return (
    <div className="hotkey-row" style={{ flexWrap: 'wrap' }}>
      <div className="hotkey-num">{index + 1}</div>

      <div className="hotkey-action">
        <select aria-label={`hotkey ${index + 1} action`} value={hotkey.action} onChange={(e) => onUpdate({ action: e.target.value, label: e.target.options[e.target.selectedIndex]?.text || '' })}>
          {actionOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </div>

      <TriggerPicker name={`hotkey ${index + 1}`} value={rowTrigger(hotkey)} keyNeedsModifier={false}
        onChange={(t) => onUpdate(rowPatch(t))} />

      <div className="hotkey-label">
        <input type="text" aria-label={`hotkey ${index + 1} label`} value={hotkey.label} onChange={(e) => onUpdate({ label: e.target.value })} placeholder="Label" />
      </div>

      <button className="btn btn-outline-danger btn-sm" onClick={onRemove} title={`Remove hotkey ${index + 1}`}>
        <ButtonIcon name="xmark" /> Remove
      </button>
      {runtimeError && (
        <div role="alert" style={{ color: 'var(--red)', fontSize: '.8rem', flexBasis: '100%' }}>{runtimeError}</div>
      )}
    </div>
  )
}

function SettingField({ label, value, type = 'text', onChange, hint, min, max, step }) {
  return (
    <div className="setting-row">
      <div className="setting-label">{label}{hint && <span className="hint">{hint}</span>}</div>
      <input
        aria-label={label}
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
        <input aria-label={label} type="checkbox" checked={value} onChange={(e) => onChange(e.target.checked)} />
        <span className="toggle-slider" />
      </label>
    </div>
  )
}

function SelectRow({ label, value, options, onChange, hint }) {
  return (
    <div className="setting-row">
      <div className="setting-label">{label}{hint && <span className="hint">{hint}</span>}</div>
      <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </div>
  )
}
