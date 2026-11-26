import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import Dashboard, { EdgeSwitchNotice, HotkeyValidity, WebGuiNotice, previewCardText } from './Dashboard.jsx'

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

describe('Dashboard warnings only for active problems', () => {
  it('shows a hotkey without action as not assigned, not as a red error', () => {
    const { container, rerender } = render(<HotkeyValidity hotkey={{ label: 'New hotkey', action: '', display: 'Ctrl+Alt+?' }} />)
    expect(screen.getByTestId('hotkey-unassigned')).toHaveTextContent('not assigned')
    expect(container.querySelector('.fa-circle-xmark')).toBeNull()
    rerender(<HotkeyValidity hotkey={{ action: 'forward_peer:gone', valid: false }} />)
    expect(screen.getByRole('img', { name: 'target not found' })).toHaveClass('fa-circle-xmark')
    rerender(<HotkeyValidity hotkey={{ action: 'return_local' }} />)
    expect(screen.getByRole('img', { name: 'valid' })).toHaveClass('fa-circle-check')
  })

  it('shows no red hotkey banner when only the extra Ctrl+Win+V shortcut is taken', () => {
    const status = {
      device_name: 'Laptop', peers: [],
      overlay_hotkeys: { ok: false, entries: [
        { slot: 'clipboard_overlay', hotkey: 'Ctrl+Alt+V', active: true, message: '' },
        { slot: 'clipboard_ctrl_win_v', hotkey: 'Ctrl+Win+V', active: false, message: 'Ctrl+Win+V (Clipboard overlay) is already taken' },
        { slot: 'command_wheel', hotkey: 'Ctrl+Alt+Space', active: true, message: '' },
      ] },
    }
    render(<Dashboard status={status} onRefresh={() => {}} />)
    expect(screen.queryByTestId('hotkey-conflict-banner')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByTestId('dashboard-overlay-hotkeys')).toHaveTextContent('Ctrl+Win+V (extra shortcut) is used by Windows')
  })
})

describe('Update of a connected PC', () => {
  const peer = (update) => ({ identity: 'device:cc33dd44', name: 'PC 2', host: '10.0.0.2', port: 45781, connected: true, update })

  it('shows the state of the peer in its row and in the notice', () => {
    render(<Dashboard status={{ device_name: 'PC 1', peers: [peer({ phase: 'transferring', text: 'Update is being transferred (42 %)', since: new Date().toISOString() })] }} onRefresh={() => {}} />)
    const state = screen.getByTestId('peer-update-state')
    expect(state).toHaveAttribute('data-phase', 'transferring')
    expect(state).toHaveTextContent('Update is being transferred (42 %)')
    expect(screen.getByTestId('peer-update-notice')).toHaveTextContent('PC 2: Update is being transferred (42 %)')
  })

  it('shows a failure with its reason as a warning', () => {
    render(<Dashboard status={{ device_name: 'PC 1', peers: [peer({ phase: 'failed', text: 'Update failed: pip install failed; runs on with 0.7.1-dev.2', since: new Date().toISOString() })] }} onRefresh={() => {}} />)
    const notice = screen.getByTestId('peer-update-notice')
    expect(notice).toHaveClass('warning')
    expect(notice).toHaveTextContent('runs on with 0.7.1-dev.2')
  })

  it('keeps an older version in the row only and hides an old "connected again"', () => {
    const old = new Date(Date.now() - 60 * 60 * 1000).toISOString()
    const { rerender } = render(<Dashboard status={{ device_name: 'PC 1', peers: [peer({ phase: 'older', text: 'Older version 0.7.1-dev.2', since: old })] }} onRefresh={() => {}} />)
    expect(screen.getByTestId('peer-update-state')).toHaveTextContent('Older version 0.7.1-dev.2')
    expect(screen.queryByTestId('peer-update-notice')).toBeNull()
    rerender(<Dashboard status={{ device_name: 'PC 1', peers: [peer({ phase: 'updated', text: 'Connected again, version 0.7.1-dev.3', since: old })] }} onRefresh={() => {}} />)
    expect(screen.getByTestId('peer-update-state')).toHaveTextContent('Connected again, version 0.7.1-dev.3')
    expect(screen.queryByTestId('peer-update-notice')).toBeNull()
  })
})

describe('EdgeSwitchNotice', () => {
  const failure = {
    reason: 'rejected',
    edge: 'north',
    peer: 'PC2',
    detail: 'invalid_target',
    message: 'Edge north: PC2 refused the switch: it did not recognise itself as the target.',
    at: 1000,
  }

  it('shows a dropped edge switch from the runtime status on the dashboard', () => {
    vi.spyOn(Date, 'now').mockReturnValue(1000 * 1000 + 5000)
    render(<Dashboard status={{ device_name: 'Laptop', peers: [], edge_switching: { enabled: true, last_failure: failure } }} onRefresh={() => {}} />)
    const notice = screen.getByTestId('edge-switch-notice')
    expect(notice).toHaveTextContent('Edge switch did not happen')
    expect(notice).toHaveTextContent('Edge north: PC2 refused the switch')
    expect(notice).toHaveAttribute('data-reason', 'rejected')
    vi.restoreAllMocks()
  })

  it('stays hidden without a failure, during a session or after two minutes', () => {
    const { container, rerender } = render(<EdgeSwitchNotice edgeSwitching={{ enabled: true, last_failure: null }} now={1000 * 1000} />)
    expect(container).toBeEmptyDOMElement()
    rerender(<EdgeSwitchNotice edgeSwitching={{ last_failure: failure, active_session: { role: 'source' } }} now={1000 * 1000} />)
    expect(container).toBeEmptyDOMElement()
    rerender(<EdgeSwitchNotice edgeSwitching={{ last_failure: failure }} now={1000 * 1000 + 3 * 60 * 1000} />)
    expect(container).toBeEmptyDOMElement()
  })
})
