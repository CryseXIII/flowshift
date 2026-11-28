import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import RemotePreviewSection, { PREVIEW_DEFAULTS, previewStateText } from './RemotePreviewSection.jsx'
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
      'Wirkt erst, wenn die Geräte gekoppelt sind und die Einstellung auf beiden PCs an ist.')
    expect(screen.getByTestId('preview-pairing-notice')).not.toHaveTextContent('noch nicht eingebaut')
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

describe('RemotePreviewSection live state', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getSettings.mockResolvedValue({ device_name: 'Local', remote_preview: { ...stored, enabled: true } })
  })

  it('names an older other PC and what this PC can do', async () => {
    const status = {
      preview: { state: 'rejected', reason: 'preview_unavailable', reason_text: 'the other PC runs an older FlowShift version that cannot send the picture (update it to 0.7.7 or newer)', peer: 'PC-2', can_send: true, can_send_text: '', window_error: '' },
      peers: [{ identity: 'device:b2', name: 'PC-2', peer_label: 'PC-2', connected: true, remote_version: '0.7.6',
        remote_preview: { code: 'peer_too_old', text: 'the other PC runs an older FlowShift version that cannot send the picture (update it to 0.7.7 or newer)' } }],
    }
    render(<RemotePreviewSection status={status} />)
    const state = await screen.findByTestId('preview-live-state')
    expect(state).toHaveClass('error')
    expect(screen.getByTestId('preview-state-text')).toHaveTextContent('No preview: the other PC runs an older FlowShift version')
    expect(screen.getByTestId('preview-this-pc')).toHaveTextContent('This PC: setting on; can send its picture')
    expect(screen.getByTestId('preview-peer-device:b2')).toHaveTextContent('PC-2: the other PC runs an older FlowShift version that cannot send the picture (update it to 0.7.7 or newer) (FlowShift 0.7.6)')
  })

  it('says why this PC cannot send or show', async () => {
    const status = {
      preview: { state: 'idle', reason: '', can_send: false, can_send_text: 'Pillow (screen capture) is missing', window_error: 'OSError: no desktop' },
      peers: [],
    }
    render(<RemotePreviewSection status={status} />)
    expect(await screen.findByTestId('preview-this-pc')).toHaveTextContent('cannot send its picture: Pillow (screen capture) is missing; the preview window cannot open: OSError: no desktop')
    expect(screen.getByTestId('preview-no-peer')).toHaveTextContent('No other PC connected.')
  })

  it('turns live numbers into one sentence', () => {
    expect(previewStateText({ state: 'streaming', peer: 'PC-2', receiving: { frames: 40, fps: 12.3, kb_per_s: 610.5 } }))
      .toBe('Showing PC-2: 12.3 pictures per second, 610.5 KB/s, 40 pictures so far.')
    expect(previewStateText({ state: 'streaming', peer: 'PC-2', notice: 'the screen of the other PC is locked', receiving: { frames: 3 } }))
      .toBe('Preview of PC-2 open: the screen of the other PC is locked.')
    expect(previewStateText({ state: 'idle', sending: { peer: 'PC-1', sent: 5, problem: '' } }))
      .toBe('Sending this screen to PC-1: 5 pictures, running.')
    expect(previewStateText(undefined)).toBe('This FlowShift runtime reports no preview state (older version).')
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
      message: 'Capture, encoding, transfer and the preview window work on this PC.',
      image: 'data:image/jpeg;base64,AAAA',
      steps: [
        { step: 'capture', label: 'Screen capture', ok: true, detail: '1920x1080 in 12 ms (average of 3)' },
        { step: 'loopback', label: 'Transfer through the connection (loopback on this PC)', ok: true, detail: '37 KB in 1 part(s)' },
        { step: 'window', label: 'Preview window on this PC', ok: false, detail: 'the window cannot be opened here' },
        { step: 'display', label: 'Picture shown in the browser', ok: true, detail: 'the picture below comes from this capture' },
      ],
    })
    render(<RemotePreviewSection />)
    fireEvent.click(await screen.findByRole('button', { name: /Run self-test/ }))
    expect(await screen.findByTestId('preview-self-test')).toHaveTextContent('Capture, encoding, transfer and the preview window work on this PC.')
    expect(api.remotePreviewSelfTest).toHaveBeenCalledWith(3)
    expect(screen.getByTestId('self-test-step-capture')).toHaveTextContent('Screen capture: OK')
    expect(screen.getByTestId('self-test-step-loopback')).toHaveTextContent('Transfer through the connection (loopback on this PC): OK')
    expect(screen.getByTestId('self-test-step-window')).toHaveTextContent('Preview window on this PC: failed – the window cannot be opened here')
    expect(screen.getByTestId('self-test-step-display')).toHaveTextContent('checking...')
    fireEvent.load(screen.getByTestId('self-test-image'))
    expect(screen.getByTestId('self-test-step-display')).toHaveTextContent('Picture shown in the browser: OK')
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
