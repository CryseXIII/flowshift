import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import {
  HotkeyConflictBanner, TriggerPicker, HotkeyStatusList, CLIPBOARD_SLOTS, WHEEL_SLOTS,
  hotkeyConflicts, hotkeyEntryLevel,
} from './HotkeyStatus.jsx'

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
  it('banner names only real conflicts of switched-on functions', () => {
    render(<HotkeyConflictBanner status={status} />)
    const banner = screen.getByTestId('hotkey-conflict-banner')
    expect(banner).toHaveTextContent('Ctrl+Alt+Space (Command wheel)')
    expect(banner).toHaveTextContent('Use Ctrl+Right click')
    // The extra Ctrl+Win+V shortcut is not a problem while Ctrl+Alt+V works.
    expect(banner).not.toHaveTextContent('Ctrl+Win+V')
  })

  it('no red banner when only the extra Ctrl+Win+V shortcut is taken', () => {
    const onlyExtra = { overlay_hotkeys: { ok: false, entries: status.overlay_hotkeys.entries.slice(0, 2) } }
    const { container } = render(<HotkeyConflictBanner status={onlyExtra} />)
    expect(container).toBeEmptyDOMElement()
    expect(hotkeyConflicts(onlyExtra)).toEqual([])
  })

  it('classifies entries: ok, neutral extra, red conflict, switched off', () => {
    const [clip, extra, wheel] = status.overlay_hotkeys.entries
    const winV = { slot: 'clipboard_win_v', hotkey: 'Win+V', active: false, message: 'Win+V taken' }
    expect(hotkeyEntryLevel(clip)).toBe('ok')
    expect(hotkeyEntryLevel(extra)).toBe('notice')
    expect(hotkeyEntryLevel(wheel)).toBe('conflict')
    // Win+V interception switched on but busy: a real problem.
    expect(hotkeyEntryLevel(winV, { enabled: true, intercept_win_v: true })).toBe('conflict')
    // Interception or the whole clipboard switched off: nothing to warn about.
    expect(hotkeyEntryLevel(winV, { enabled: true, intercept_win_v: false })).toBe('off')
    expect(hotkeyEntryLevel(clip, { enabled: false })).toBe('off')
    expect(hotkeyEntryLevel(wheel, { enabled: false })).toBe('conflict')
  })

  it('banner is hidden when all hotkeys are active', () => {
    const { container } = render(<HotkeyConflictBanner status={{ overlay_hotkeys: { ok: true, entries: [] } }} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('lists only the slots of its section', () => {
    render(<HotkeyStatusList status={status} slots={CLIPBOARD_SLOTS} testId="clip" />)
    const list = screen.getByTestId('clip')
    expect(list).toHaveTextContent('Ctrl+Alt+V active')
    expect(list).toHaveTextContent('Ctrl+Win+V (extra shortcut) is used by Windows or another program. Ctrl+Alt+V opens the clipboard overlay.')
    expect(list).not.toHaveTextContent('already taken')
    expect(list).not.toHaveTextContent('Command wheel')
    const extraLine = [...list.querySelectorAll('li')].find((li) => li.textContent.includes('Ctrl+Win+V'))
    expect(extraLine).toHaveAttribute('data-level', 'notice')
    render(<HotkeyStatusList status={status} slots={WHEEL_SLOTS} testId="wheel" />)
    const wheelLine = screen.getByTestId('wheel').querySelector('li')
    expect(wheelLine).toHaveTextContent('Ctrl+Alt+Space (Command wheel)')
    expect(wheelLine).toHaveAttribute('data-level', 'conflict')
  })

  it('leaves out hotkeys of switched-off functions', () => {
    const withWinV = { overlay_hotkeys: { ok: false, entries: [
      { slot: 'clipboard_overlay', hotkey: 'Ctrl+Alt+V', active: true, message: '' },
      { slot: 'clipboard_win_v', hotkey: 'Win+V', active: false, message: 'Win+V (Clipboard overlay (Win+V interception)) is already taken' },
    ] } }
    render(<HotkeyStatusList status={withWinV} slots={CLIPBOARD_SLOTS} settings={{ enabled: true, intercept_win_v: false }} testId="off" />)
    expect(screen.getByTestId('off')).not.toHaveTextContent('Win+V (')
    expect(screen.getByTestId('off')).toHaveTextContent('Ctrl+Alt+V active')
    const { container } = render(<HotkeyStatusList status={withWinV} slots={CLIPBOARD_SLOTS} settings={{ enabled: false }} testId="none" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('picker edits modifiers with runtime bits and captures a key', () => {
    const onChange = vi.fn()
    const { rerender } = render(<TriggerPicker name="clipboard hotkey" value={{ mods: 5, vk: 0x56 }} onChange={onChange} />)
    expect(screen.getByRole('button', { name: 'clipboard hotkey key' })).toHaveTextContent('Ctrl+Alt+V')
    fireEvent.click(screen.getByLabelText('clipboard hotkey modifier Shift'))
    expect(onChange).toHaveBeenLastCalledWith({ mods: 7, vk: 0x56 })
    rerender(<TriggerPicker name="clipboard hotkey" value={{ mods: 5, vk: 0x56 }} onChange={onChange} />)
    const key = screen.getByRole('button', { name: 'clipboard hotkey key' })
    fireEvent.click(key)
    fireEvent.keyDown(key, { key: 'b', keyCode: 0x42, which: 0x42 })
    expect(onChange).toHaveBeenLastCalledWith({ mods: 5, vk: 0x42 })
    // Modifiers held with the key replace the ticked ones.
    fireEvent.click(key)
    fireEvent.keyDown(key, { key: 'b', keyCode: 0x42, which: 0x42, ctrlKey: true, shiftKey: true })
    expect(onChange).toHaveBeenLastCalledWith({ mods: 3, vk: 0x42 })
  })

  it('picker records mouse buttons with modifiers and refuses a plain left or right click', () => {
    const onChange = vi.fn()
    render(<TriggerPicker name="wheel hotkey" value={{ mods: 1, mouse: 'right' }} onChange={onChange} />)
    const field = screen.getByRole('button', { name: 'wheel hotkey key' })
    expect(field).toHaveTextContent('Ctrl+Right click')
    fireEvent.click(field)
    expect(field).toHaveTextContent('Press a key or mouse button')
    // Plain right click: hint, nothing changes, still recording.
    fireEvent.mouseDown(field, { button: 2 })
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByText(/Right click alone cannot be a hotkey/)).toBeVisible()
    // Ctrl + side button 1 (Viktor's check), Ctrl+Shift + middle click.
    fireEvent.mouseDown(field, { button: 3, ctrlKey: true })
    expect(onChange).toHaveBeenLastCalledWith({ mods: 1, mouse: 'x1' })
    fireEvent.click(field)
    fireEvent.mouseDown(field, { button: 1, ctrlKey: true, shiftKey: true })
    expect(onChange).toHaveBeenLastCalledWith({ mods: 3, mouse: 'middle' })
    // A plain side button is fine (no modifier needed).
    fireEvent.click(field)
    fireEvent.mouseDown(field, { button: 4 })
    expect(onChange).toHaveBeenLastCalledWith({ mods: 0, mouse: 'x2' })
    // Mouse wheel with Alt.
    fireEvent.click(field)
    fireEvent.wheel(field, { deltaY: 120, altKey: true })
    expect(onChange).toHaveBeenLastCalledWith({ mods: 4, mouse: 'wheel_down' })
    // Escape stops recording without a change.
    const calls = onChange.mock.calls.length
    fireEvent.click(field)
    fireEvent.keyDown(field, { key: 'Escape', keyCode: 0x1B, which: 0x1B })
    expect(onChange.mock.calls.length).toBe(calls)
    expect(field).toHaveTextContent('Ctrl+Right click')
  })

  it('picker chooses a mouse button from the list, clears, and shows a plain left click as invalid', () => {
    const onChange = vi.fn()
    const { rerender } = render(<TriggerPicker name="wheel hotkey" value={{ mods: 1, vk: 0x57 }} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText('wheel hotkey mouse button'), { target: { value: 'right' } })
    expect(onChange).toHaveBeenLastCalledWith({ mods: 1, mouse: 'right' })
    fireEvent.click(screen.getByRole('button', { name: 'clear wheel hotkey' }))
    expect(onChange).toHaveBeenLastCalledWith(null)
    rerender(<TriggerPicker name="wheel hotkey" value={null} onChange={onChange} />)
    expect(screen.getByRole('button', { name: 'wheel hotkey key' })).toHaveTextContent('Not set')
    expect(screen.getByLabelText('wheel hotkey modifier Ctrl')).toBeDisabled()
    expect(screen.queryByRole('alert')).toBeNull()
    rerender(<TriggerPicker name="wheel hotkey" value={{ mods: 0, mouse: 'left' }} onChange={onChange} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Left click alone cannot be a hotkey')
  })
})
