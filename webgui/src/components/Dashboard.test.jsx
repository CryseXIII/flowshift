import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import Dashboard, { HotkeyValidity, WebGuiNotice } from './Dashboard.jsx'

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
