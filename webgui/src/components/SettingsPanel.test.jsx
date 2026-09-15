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
})
