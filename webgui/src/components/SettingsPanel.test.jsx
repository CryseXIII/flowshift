import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SettingsPanel from './SettingsPanel.jsx'
import * as api from '../api.js'

vi.mock('./SoftwareUpdateSection.jsx', () => ({ default: () => null }))
vi.mock('./CommandWheelSection.jsx', () => ({ default: () => null }))
vi.mock('../api.js', () => ({
  getSettings: vi.fn(),
  saveSettings: vi.fn(),
  getAutoStart: vi.fn(),
  getWebguiConfig: vi.fn(),
  setAutoStart: vi.fn(),
  injectType: vi.fn(),
  shutdownApp: vi.fn(),
  setWebguiConfig: vi.fn(),
  restartService: vi.fn(),
}))

describe('SettingsPanel clipboard settings', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getSettings.mockResolvedValue({
      device_name: 'Local', port: 45781, hotkeys: [],
      enabled: true, persist: false, history_max_items: 200,
      history_max_total_gb: 2, max_auto_transfer_mb: 100, max_item_gb: 12.5,
    })
    api.saveSettings.mockResolvedValue({ ok: true })
    api.getAutoStart.mockResolvedValue({ enabled: false })
    api.getWebguiConfig.mockResolvedValue({ config: { port: 5000 } })
  })

  it('edits and saves persistence and the hard item limit', async () => {
    render(<SettingsPanel status={{ device_name: 'Local', peers: [] }} />)

    const persist = await screen.findByLabelText('Persist Clipboard History')
    const maxItem = screen.getByLabelText('Max Item (GB)')
    expect(persist).not.toBeChecked()
    expect(maxItem).toHaveValue(12.5)

    fireEvent.click(persist)
    fireEvent.change(maxItem, { target: { value: '22.5' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save Settings' }))

    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledWith(expect.objectContaining({
      persist: true,
      max_item_gb: 22.5,
      hotkeys: [],
    })))
    expect(await screen.findByText('Settings saved.')).toBeVisible()
  })

  it('edits the clipboard overlay hotkey and shows its registration status', async () => {
    const status = {
      device_name: 'Local', peers: [],
      overlay_hotkeys: { ok: false, entries: [
        { slot: 'clipboard_overlay', hotkey: 'Ctrl+Alt+V', active: true, message: '' },
        { slot: 'clipboard_ctrl_win_v', hotkey: 'Ctrl+Win+V', active: false,
          message: 'Ctrl+Win+V (Clipboard overlay) is already taken by Windows or another program. Use Ctrl+Alt+V instead.' },
      ] },
    }
    render(<SettingsPanel status={status} />)
    const key = await screen.findByRole('button', { name: 'clipboard hotkey key' })
    expect(key).toHaveTextContent('Ctrl+Alt+V')
    // Only the extra Ctrl+Win+V shortcut is taken: neutral hint, no red warning.
    const hotkeyStatus = screen.getByTestId('clipboard-hotkey-status')
    expect(hotkeyStatus).toHaveTextContent('Ctrl+Win+V (extra shortcut) is used by Windows or another program.')
    expect(hotkeyStatus).not.toHaveTextContent('already taken')
    expect(hotkeyStatus.querySelector('[data-level="conflict"]')).toBeNull()

    fireEvent.click(screen.getByLabelText('clipboard hotkey modifier Alt'))
    fireEvent.click(screen.getByLabelText('clipboard hotkey modifier Shift'))
    fireEvent.click(screen.getByRole('button', { name: 'Save Settings' }))
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledWith(expect.objectContaining({
      overlay_hotkey: { mods: 3, vk: 0x56 },
    })))
  })

  it('shows the remote preview section and never saves a stale copy of it', async () => {
    api.getSettings.mockResolvedValue({
      device_name: 'Local', port: 45781, hotkeys: [],
      remote_preview: { enabled: true, fps: 20, max_width: 960, monitor: 'primary', cursor_crop: false,
        window: { corner: 'top_right', width_px: 480 } },
    })
    render(<SettingsPanel status={{ device_name: 'Local', peers: [] }} />)
    expect(await screen.findByLabelText('Enable remote preview')).toBeChecked()
    expect(screen.getByTestId('preview-pairing-notice')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Save Settings' }))
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledTimes(1))
    expect(api.saveSettings.mock.calls[0][0]).not.toHaveProperty('remote_preview')
  })

  it('shows no Win+V warning while the interception is switched off', async () => {
    const status = {
      device_name: 'Local', peers: [],
      overlay_hotkeys: { ok: false, entries: [
        { slot: 'clipboard_overlay', hotkey: 'Ctrl+Alt+V', active: true, message: '' },
        { slot: 'clipboard_win_v', hotkey: 'Win+V', active: false, message: 'Win+V (Clipboard overlay (Win+V interception)) is already taken' },
      ] },
    }
    render(<SettingsPanel status={status} />)
    await screen.findByLabelText('Intercept Win+V')
    await waitFor(() => expect(screen.getByTestId('clipboard-hotkey-status')).toHaveTextContent('Ctrl+Alt+V active'))
    expect(screen.getByTestId('clipboard-hotkey-status')).not.toHaveTextContent('Win+V (')
  })

  it('shows edge-switching layout warnings neutral while edge switching is off', async () => {
    const status = {
      device_name: 'Local', peers: [],
      edge_switching: { enabled: false, warnings: ['unknown peer identity for east: gone'] },
    }
    api.getSettings.mockResolvedValue({ device_name: 'Local', hotkeys: [], display_layout: { enabled: false, edges: {} } })
    render(<SettingsPanel status={status} />)
    const note = await screen.findByTestId('settings-edge-warnings')
    expect(note).toHaveTextContent('unknown peer identity for east')
    expect(note).toHaveAttribute('data-level', 'notice')
  })

  it('names every hotkey row control and the remove button', async () => {
    api.getSettings.mockResolvedValue({
      device_name: 'Local', hotkeys: [{ action: 'return_local', mods: 3, key: 0x4c, label: 'Back' }],
    })
    render(<SettingsPanel status={{ device_name: 'Local', peers: [] }} />)
    expect(await screen.findByRole('button', { name: 'hotkey 1 key' })).toHaveAttribute('title', expect.stringContaining('press a key or a mouse button'))
    expect(screen.getByLabelText('hotkey 1 action')).toHaveValue('return_local')
    const remove = screen.getByRole('button', { name: 'Remove' })
    expect(remove).toHaveAttribute('title', 'Remove hotkey 1')
    expect(remove.querySelector('svg.button-icon')).not.toBeNull()
  })

  it('explains a hotkey conflict in plain text', async () => {
    api.saveSettings.mockRejectedValue(new Error('hotkey_conflict'))
    render(<SettingsPanel status={{ device_name: 'Local', peers: [] }} />)
    await screen.findByRole('button', { name: 'clipboard hotkey key' })
    fireEvent.click(screen.getByRole('button', { name: 'Save Settings' }))
    expect(await screen.findByText('This hotkey is already used by another FlowShift function. Choose another one.')).toBeVisible()
  })

  it('shows the API explanation of a hotkey assigned twice', async () => {
    const error = new Error('hotkey_conflict')
    error.detail = 'Ctrl+Right click is assigned twice: Command wheel and Clipboard overlay. Choose another hotkey for one of them.'
    api.saveSettings.mockRejectedValue(error)
    render(<SettingsPanel status={{ device_name: 'Local', peers: [] }} />)
    await screen.findByRole('button', { name: 'clipboard hotkey key' })
    fireEvent.click(screen.getByRole('button', { name: 'Save Settings' }))
    expect(await screen.findByText(error.detail)).toBeVisible()
  })

  it('clears the clipboard overlay hotkey and sends null', async () => {
    render(<SettingsPanel status={{ device_name: 'Local', peers: [] }} />)
    await screen.findByRole('button', { name: 'clipboard hotkey key' })
    fireEvent.click(screen.getByRole('button', { name: 'clear clipboard hotkey' }))
    expect(screen.getByRole('button', { name: 'clipboard hotkey key' })).toHaveTextContent('Not set')
    // Cleared on purpose: no red text.
    expect(screen.queryByRole('alert')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Save Settings' }))
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledWith(expect.objectContaining({ overlay_hotkey: null })))
  })

  it('records a mouse button for a peer hotkey and blocks a hotkey set twice', async () => {
    api.getSettings.mockResolvedValue({
      device_name: 'Local',
      hotkeys: [
        { action: 'return_local', mods: 5, key: 0x30, label: 'Return to local' },
        { action: 'return_local', mods: 5, key: 0x31, label: 'Second' },
      ],
    })
    render(<SettingsPanel status={{ device_name: 'Local', peers: [] }} />)
    const field = await screen.findByRole('button', { name: 'hotkey 1 key' })
    fireEvent.click(field)
    fireEvent.mouseDown(field, { button: 3, ctrlKey: true })
    expect(field).toHaveTextContent('Ctrl+Side button 1')
    const second = screen.getByRole('button', { name: 'hotkey 2 key' })
    fireEvent.click(second)
    fireEvent.mouseDown(second, { button: 3, ctrlKey: true })
    expect(screen.getByRole('alert')).toHaveTextContent('Ctrl+Side button 1 is assigned twice: Return to local and Second')
    expect(screen.getByRole('button', { name: 'Save Settings' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'clear hotkey 2' }))
    expect(screen.getByRole('button', { name: 'Save Settings' })).not.toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Save Settings' }))
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledWith(expect.objectContaining({
      hotkeys: [
        { action: 'return_local', mods: 1, key: 0, label: 'Return to local', mouse: 'x1' },
        { action: 'return_local', mods: 5, key: 0, label: 'Second' },
      ],
    })))
  })
})
