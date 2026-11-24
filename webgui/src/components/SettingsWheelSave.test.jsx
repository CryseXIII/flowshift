import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SettingsPanel from './SettingsPanel.jsx'
import * as api from '../api.js'

// Viktor 24.11. (0.7.0-dev.50): changed the wheel hotkey, clicked "Save
// Settings", F5 -> Ctrl+Right click again. SettingsPanel.test.jsx replaces the
// wheel section by an empty stub, so no test ever saw what "Save Settings"
// does with the wheel. Here the real section sits in the real page; only the
// HTTP calls are recorded (route and body). The server side of the same
// requests runs against the real Web-API in test_wheel_hotkey_roundtrip.py.
vi.mock('./SoftwareUpdateSection.jsx', () => ({ default: () => null }))
vi.mock('../api.js', () => ({
  getSettings: vi.fn(),
  saveSettings: vi.fn(),
  getActions: vi.fn(),
  saveWheel: vi.fn(),
  getAutoStart: vi.fn(),
  getWebguiConfig: vi.fn(),
  setAutoStart: vi.fn(),
  injectType: vi.fn(),
  shutdownApp: vi.fn(),
  setWebguiConfig: vi.fn(),
  restartService: vi.fn(),
}))

const wheel = {
  pages: [['copy', 'paste']],
  hotkey: { mods: 1, mouse: 'right', display: 'Ctrl+Right click' },
  trigger: { tap_open: true, hold_swipe: true, hold_ms: 250 },
}

describe('Save Settings with the command wheel', () => {
  let stored
  beforeEach(() => {
    vi.clearAllMocks()
    // A tiny stand-in for config.json: what the wheel route stores, GET returns.
    stored = { ...wheel }
    api.getSettings.mockResolvedValue({ device_name: 'Local', port: 45781, hotkeys: [], enabled: true })
    api.saveSettings.mockResolvedValue({ ok: true })
    api.getAutoStart.mockResolvedValue({ enabled: false })
    api.getWebguiConfig.mockResolvedValue({ config: { port: 5000 } })
    api.getActions.mockImplementation(async () => ({
      ok: true,
      actions: [{ id: 'copy', label: 'Copy' }, { id: 'paste', label: 'Paste' }],
      wheel: stored,
      limits: { max_slots_per_page: 8, max_pages: 16 },
    }))
    api.saveWheel.mockImplementation(async (body) => {
      stored = { ...stored, ...body, hotkey: body.hotkey && { ...body.hotkey } }
      return { ok: true, wheel: stored }
    })
  })

  it('changed hotkey: "Save Settings" posts it to /api/actions/wheel, F5 shows it again', async () => {
    const { unmount } = render(<SettingsPanel status={{ device_name: 'Local', peers: [] }} />)
    const field = await screen.findByRole('button', { name: 'wheel hotkey key' })
    expect(field).toHaveTextContent('Ctrl+Right click')
    fireEvent.click(field)
    fireEvent.mouseDown(field, { button: 3, ctrlKey: true })
    expect(field).toHaveTextContent('Ctrl+Side button 1')

    fireEvent.click(screen.getByRole('button', { name: 'Save Settings' }))
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledTimes(1))
    expect(api.saveWheel).toHaveBeenCalledTimes(1)
    expect(api.saveWheel).toHaveBeenCalledWith({
      pages: [['copy', 'paste']],
      hotkey: { mods: 1, mouse: 'x1' },
      trigger: { tap_open: true, hold_swipe: true, hold_ms: 250 },
    })
    // Wheel first, then the other settings; never the wheel inside /api/settings.
    expect(api.saveWheel.mock.invocationCallOrder[0]).toBeLessThan(api.saveSettings.mock.invocationCallOrder[0])
    expect(api.saveSettings.mock.calls[0][0]).not.toHaveProperty('command_wheel')
    expect(await screen.findByTestId('settings-save-result'))
      .toHaveTextContent('Command wheel saved. Hotkey: Ctrl+Side button 1. Settings saved.')
    unmount()

    render(<SettingsPanel status={{ device_name: 'Local', peers: [] }} />)
    expect(await screen.findByRole('button', { name: 'wheel hotkey key' })).toHaveTextContent('Ctrl+Side button 1')
  })

  it('cleared hotkey: asks first, saves null, F5 keeps it empty without red text', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { unmount } = render(<SettingsPanel status={{ device_name: 'Local', peers: [] }} />)
    await screen.findByRole('button', { name: 'wheel hotkey key' })
    fireEvent.click(screen.getByRole('button', { name: 'clear wheel hotkey' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save Settings' }))
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledTimes(1))
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(api.saveWheel).toHaveBeenCalledWith(expect.objectContaining({ hotkey: null }))
    expect(await screen.findByTestId('settings-save-result'))
      .toHaveTextContent('Command wheel saved. Hotkey: none. Settings saved.')
    unmount()

    render(<SettingsPanel status={{ device_name: 'Local', peers: [] }} />)
    expect(await screen.findByRole('button', { name: 'wheel hotkey key' })).toHaveTextContent('Not set')
    expect(screen.getByTestId('wheel-no-hotkey')).toHaveTextContent('tray icon')
    expect(screen.queryByRole('alert')).toBeNull()
    confirm.mockRestore()
  })

  it('cancelled clear question: nothing is saved and the page says so', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<SettingsPanel status={{ device_name: 'Local', peers: [] }} />)
    await screen.findByRole('button', { name: 'wheel hotkey key' })
    fireEvent.click(screen.getByRole('button', { name: 'clear wheel hotkey' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save Settings' }))
    expect(await screen.findByTestId('settings-save-result'))
      .toHaveTextContent('Command wheel not saved: the hotkey stays Ctrl+Right click.')
    expect(api.saveWheel).not.toHaveBeenCalled()
    expect(api.saveSettings).not.toHaveBeenCalled()
    confirm.mockRestore()
  })

  it('unchanged wheel: "Save Settings" does not touch it; a refused wheel save is shown in red', async () => {
    render(<SettingsPanel status={{ device_name: 'Local', peers: [] }} />)
    await screen.findByRole('button', { name: 'wheel hotkey key' })
    fireEvent.click(screen.getByRole('button', { name: 'Save Settings' }))
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledTimes(1))
    expect(api.saveWheel).not.toHaveBeenCalled()
    expect(await screen.findByTestId('settings-save-result')).toHaveTextContent(/^Settings saved\.$/)

    api.saveWheel.mockRejectedValueOnce(Object.assign(new Error('hotkey_conflict'),
      { detail: 'Ctrl+Side button 1 is assigned twice: Command wheel and Return to local.' }))
    const field = screen.getByRole('button', { name: 'wheel hotkey key' })
    fireEvent.click(field)
    fireEvent.mouseDown(field, { button: 3, ctrlKey: true })
    fireEvent.click(screen.getByRole('button', { name: 'Save Settings' }))
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledTimes(2))
    const result = await screen.findByTestId('settings-save-result')
    await waitFor(() => expect(result).toHaveTextContent('Command wheel not saved: Ctrl+Side button 1 is assigned twice'))
    expect(result.getAttribute('style')).toContain('var(--red)')
  })
})
