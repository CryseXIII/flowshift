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
    expect(screen.getByTestId('clipboard-hotkey-status')).toHaveTextContent('Ctrl+Win+V (Clipboard overlay) is already taken')

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

  it('explains a hotkey conflict in plain text', async () => {
    api.saveSettings.mockRejectedValue(new Error('hotkey_conflict'))
    render(<SettingsPanel status={{ device_name: 'Local', peers: [] }} />)
    await screen.findByRole('button', { name: 'clipboard hotkey key' })
    fireEvent.click(screen.getByRole('button', { name: 'Save Settings' }))
    expect(await screen.findByText('This hotkey already opens the command wheel. Choose another one.')).toBeVisible()
  })
})
