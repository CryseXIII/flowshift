import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import Dashboard, { WebGuiNotice, previewCardText } from './Dashboard.jsx'

vi.mock('../api.js', () => ({
  getPeers: vi.fn(() => Promise.resolve({ peers: [] })),
  getHotkeys: vi.fn(() => Promise.resolve({ hotkeys: [] })),
  toggleForwarding: vi.fn(),
}))

const outdated = {
  ok: false,
  state: 'outdated',
  overlay_ready: true,
  app_version: '0.7.0-dev.23',
  webgui_version: '0.7.0-dev.22',
  message: 'The installed FlowShift WebGUI (0.7.0-dev.22) does not match the program (0.7.0-dev.23).',
  action: 'Run install_flowshift.bat (or FlowShift-Setup.exe) again; it updates the program and the WebGUI together and keeps your settings.',
}

describe('WebGuiNotice', () => {
  it('shows what is wrong and what to do when the WebGUI does not match', () => {
    render(<WebGuiNotice webgui={outdated} />)
    const notice = screen.getByTestId('webgui-notice')
    expect(notice).toHaveTextContent('WebGUI does not match FlowShift 0.7.0-dev.23')
    expect(notice).toHaveTextContent('(0.7.0-dev.22) does not match the program')
    expect(notice).toHaveTextContent('Run install_flowshift.bat')
  })

  it('stays hidden when program and WebGUI match or the runtime is older', () => {
    const { container, rerender } = render(<WebGuiNotice webgui={{ ok: true, state: 'ok' }} />)
    expect(container).toBeEmptyDOMElement()
    rerender(<WebGuiNotice webgui={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('is rendered on the dashboard from the runtime status', () => {
    render(<Dashboard status={{ device_name: 'Laptop', peers: [], webgui: outdated }} onRefresh={() => {}} />)
    expect(screen.getByRole('alert')).toHaveTextContent('does not match the program')
  })
})

describe('Remote preview card', () => {
  it('shows a refused preview with its reason', () => {
    render(<Dashboard status={{ device_name: 'Laptop', peers: [], preview_state: 'rejected', preview_reason: 'pairing_required' }} onRefresh={() => {}} />)
    const card = screen.getByTestId('preview-card')
    expect(card).toHaveTextContent('Remote Preview')
    expect(card).toHaveTextContent('Rejected')
    expect(card).toHaveTextContent('pairing_required (devices are not paired yet)')
  })

  it('shows idle without a reason, also for an older runtime', () => {
    expect(previewCardText({ preview_state: 'idle', preview_reason: '' })).toEqual({ value: 'Idle', sub: '', warn: false })
    expect(previewCardText({})).toEqual({ value: 'Idle', sub: '', warn: false })
    expect(previewCardText({ preview_state: 'requested', preview_reason: '' }).value).toBe('Requested')
  })
})
