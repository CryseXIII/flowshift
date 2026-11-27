import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import RemotePreviewSection, { PREVIEW_DEFAULTS } from './RemotePreviewSection.jsx'
import * as api from '../api.js'

vi.mock('../api.js', () => ({
  getSettings: vi.fn(),
  saveSettings: vi.fn(),
  remotePreviewSelfTest: vi.fn(),
}))

const stored = {
  enabled: false,
  fps: 15,
  max_width: 960,
  monitor: 'cursor',
  cursor_crop: false,
  window: { corner: 'bottom_right', width_px: 480 },
}

describe('RemotePreviewSection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getSettings.mockResolvedValue({ device_name: 'Local', remote_preview: stored })
    api.saveSettings.mockImplementation(async (body) => ({ ok: true, config: { remote_preview: body.remote_preview } }))
  })

  it('shows the pairing notice and the stored values', async () => {
    render(<RemotePreviewSection />)
    expect(await screen.findByLabelText('Enable remote preview')).not.toBeChecked()
    expect(screen.getByTestId('preview-pairing-notice')).toHaveTextContent(
      'Wirkt erst, wenn die Geräte gekoppelt sind. Gekoppelt ist ein PC, sobald er mit Geräte-ID in der Geräteliste steht')
    expect(screen.getByLabelText('Frame rate (fps)')).toHaveValue(15)
    expect(screen.getByLabelText('Maximum width (px)')).toHaveValue(960)
    expect(screen.getByLabelText('Monitor')).toHaveValue('cursor')
    expect(screen.getByLabelText('Area around the mouse')).not.toBeChecked()
    expect(screen.getByLabelText('Window corner')).toHaveValue('bottom_right')
    expect(screen.getByLabelText('Window width (px)')).toHaveValue(480)
    expect(screen.queryByLabelText('Screen number')).toBeNull()
  })

  it('saves every field only under remote_preview', async () => {
    render(<RemotePreviewSection />)
    fireEvent.click(await screen.findByLabelText('Enable remote preview'))
    fireEvent.change(screen.getByLabelText('Frame rate (fps)'), { target: { value: '24' } })
    fireEvent.change(screen.getByLabelText('Maximum width (px)'), { target: { value: '1280' } })
    fireEvent.change(screen.getByLabelText('Monitor'), { target: { value: 'number' } })
    fireEvent.change(screen.getByLabelText('Screen number'), { target: { value: '2' } })
    fireEvent.click(screen.getByLabelText('Area around the mouse'))
    fireEvent.change(screen.getByLabelText('Window corner'), { target: { value: 'top_left' } })
    fireEvent.change(screen.getByLabelText('Window width (px)'), { target: { value: '640' } })
    fireEvent.click(screen.getByRole('button', { name: /Save Remote Preview/ }))

    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledWith({
      remote_preview: {
        enabled: true,
        fps: 24,
        max_width: 1280,
        monitor: 2,
        cursor_crop: true,
        window: { corner: 'top_left', width_px: 640 },
      },
    }))
    expect(await screen.findByText('Remote preview settings saved.')).toBeVisible()
  })

  it('shows the refusal of invalid values', async () => {
    api.saveSettings.mockRejectedValue(new Error('invalid_remote_preview'))
    render(<RemotePreviewSection />)
    fireEvent.change(await screen.findByLabelText('Frame rate (fps)'), { target: { value: '99' } })
    fireEvent.click(screen.getByRole('button', { name: /Save Remote Preview/ }))
    expect(await screen.findByText(/Not saved: a value is outside its range/)).toBeVisible()
    expect(api.saveSettings.mock.calls[0][0].remote_preview.fps).toBe(99)
  })

  it('falls back to the defaults for an older runtime without the field', async () => {
    api.getSettings.mockResolvedValue({ device_name: 'Local' })
    render(<RemotePreviewSection />)
    expect(await screen.findByLabelText('Window width (px)')).toHaveValue(PREVIEW_DEFAULTS.window.width_px)
    expect(screen.getByLabelText('Enable remote preview')).not.toBeChecked()
  })
})

describe('RemotePreviewSection self-test', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getSettings.mockResolvedValue({ device_name: 'Local', remote_preview: stored })
  })

  it('shows every step and the captured picture; display is OK only once the picture loaded', async () => {
    api.remotePreviewSelfTest.mockResolvedValue({
      ok: true,
      message: 'Capture and display work on this PC. Sending the picture to the other PC and the preview window are not built yet.',
      image: 'data:image/jpeg;base64,AAAA',
      steps: [
        { step: 'capture', label: 'Screen capture', ok: true, detail: '1920x1080 in 12 ms (average of 3)' },
        { step: 'display', label: 'Picture shown on this PC', ok: true, detail: 'the picture below comes from this capture' },
        { step: 'stream', label: 'Transfer to the other PC and preview window', ok: false, detail: 'not built yet in this version' },
      ],
    })
    render(<RemotePreviewSection />)
    fireEvent.click(await screen.findByRole('button', { name: /Run self-test/ }))
    expect(await screen.findByTestId('preview-self-test')).toHaveTextContent('Capture and display work on this PC.')
    expect(api.remotePreviewSelfTest).toHaveBeenCalledWith(3)
    expect(screen.getByTestId('self-test-step-capture')).toHaveTextContent('Screen capture: OK')
    expect(screen.getByTestId('self-test-step-display')).toHaveTextContent('checking...')
    fireEvent.load(screen.getByTestId('self-test-image'))
    expect(screen.getByTestId('self-test-step-display')).toHaveTextContent('Picture shown on this PC: OK')
    expect(screen.getByTestId('self-test-step-stream')).toHaveTextContent('not yet')
  })

  it('names the reason when the self-test request fails', async () => {
    const error = new Error('not available')
    error.detail = null
    api.remotePreviewSelfTest.mockRejectedValue(error)
    render(<RemotePreviewSection />)
    fireEvent.click(await screen.findByRole('button', { name: /Run self-test/ }))
    expect(await screen.findByTestId('preview-self-test')).toHaveTextContent('Self-test failed: not available')
  })
})
