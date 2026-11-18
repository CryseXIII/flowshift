import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { HotkeyConflictBanner, HotkeyPicker, HotkeyStatusList, CLIPBOARD_SLOTS, WHEEL_SLOTS } from './HotkeyStatus.jsx'

// status.overlay_hotkeys as reported for Viktor's laptop (18.11.): Ctrl+Alt+V
// works, Ctrl+Win+V and Ctrl+Alt+Space are taken by Windows (error 1409).
const status = {
  overlay_hotkeys: {
    ok: false,
    conflicts: ['clipboard_ctrl_win_v', 'command_wheel'],
    entries: [
      { slot: 'clipboard_overlay', hotkey: 'Ctrl+Alt+V', active: true, message: 'Ctrl+Alt+V opens the clipboard overlay.' },
      { slot: 'clipboard_ctrl_win_v', hotkey: 'Ctrl+Win+V', active: false,
        message: 'Ctrl+Win+V (Clipboard overlay) is already taken by Windows or another program. Use Ctrl+Alt+V or tray menu > Clipboard instead, or choose another hotkey under Settings > Clipboard > Overlay hotkey.' },
      { slot: 'command_wheel', hotkey: 'Ctrl+Alt+Space', active: false,
        message: 'Ctrl+Alt+Space (Command wheel) is already taken by Windows or another program. Use Ctrl+Right click instead, or choose another hotkey under Settings > Command Wheel > Open hotkey.' },
    ],
  },
}

describe('HotkeyStatus', () => {
  it('banner names every busy hotkey and what works instead', () => {
    render(<HotkeyConflictBanner status={status} />)
    const banner = screen.getByTestId('hotkey-conflict-banner')
    expect(banner).toHaveTextContent('Ctrl+Win+V (Clipboard overlay) is already taken')
    expect(banner).toHaveTextContent('Use Ctrl+Alt+V')
    expect(banner).toHaveTextContent('Ctrl+Alt+Space (Command wheel)')
    expect(banner).toHaveTextContent('Use Ctrl+Right click')
  })

  it('banner is hidden when all hotkeys are active', () => {
    const { container } = render(<HotkeyConflictBanner status={{ overlay_hotkeys: { ok: true, entries: [] } }} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('lists only the slots of its section', () => {
    render(<HotkeyStatusList status={status} slots={CLIPBOARD_SLOTS} testId="clip" />)
    const list = screen.getByTestId('clip')
    expect(list).toHaveTextContent('Ctrl+Alt+V active')
    expect(list).toHaveTextContent('Ctrl+Win+V (Clipboard overlay) is already taken')
    expect(list).not.toHaveTextContent('Command wheel')
    render(<HotkeyStatusList status={status} slots={WHEEL_SLOTS} testId="wheel" />)
    expect(screen.getByTestId('wheel')).toHaveTextContent('Ctrl+Alt+Space (Command wheel)')
  })

  it('picker edits modifiers with runtime bits and captures a key', () => {
    const onChange = vi.fn()
    const { rerender } = render(<HotkeyPicker name="clipboard hotkey" value={{ mods: 5, vk: 0x56 }} onChange={onChange} />)
    expect(screen.getByRole('button', { name: 'clipboard hotkey key' })).toHaveTextContent('Ctrl+Alt+V')
    fireEvent.click(screen.getByLabelText('clipboard hotkey modifier Shift'))
    expect(onChange).toHaveBeenLastCalledWith({ mods: 7, vk: 0x56 })
    rerender(<HotkeyPicker name="clipboard hotkey" value={{ mods: 5, vk: 0x56 }} onChange={onChange} />)
    const key = screen.getByRole('button', { name: 'clipboard hotkey key' })
    fireEvent.click(key)
    fireEvent.keyDown(key, { key: 'b', keyCode: 0x42, which: 0x42 })
    expect(onChange).toHaveBeenLastCalledWith({ mods: 5, vk: 0x42 })
  })
})
