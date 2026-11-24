import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import CommandWheelSection, { NO_HOTKEY_CONFIRM, NO_HOTKEY_TEXT } from './CommandWheelSection.jsx'
import * as api from '../api.js'

vi.mock('../api.js', () => ({
  getActions: vi.fn(),
  saveWheel: vi.fn(),
}))

const actionsResponse = {
  ok: true,
  actions: [
    { id: 'copy', label: 'Copy', icon: 'copy', kind: 'keys' },
    { id: 'paste', label: 'Paste', icon: 'paste', kind: 'keys' },
    { id: 'delete', label: 'Delete', icon: 'trash', kind: 'keys' },
  ],
  wheel: {
    pages: [['copy', 'paste'], ['delete']],
    // Runtime bitmask: Ctrl=1 | Alt=4 (runtime_model.py), not Ctrl+Shift.
    hotkey: { mods: 5, vk: 0x20, display: 'Ctrl+Alt+Space' },
  },
  limits: { max_slots_per_page: 8, max_pages: 16 },
}

describe('CommandWheelSection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getActions.mockResolvedValue(actionsResponse)
    api.saveWheel.mockImplementation(async (wheel) => ({
      ok: true,
      wheel: {
        ...wheel,
        hotkey: wheel.hotkey ? { ...wheel.hotkey, display: 'Ctrl+Win+K' } : null,
      },
    }))
  })

  it('loads pages and saves edited slots and hotkey in the API shape', async () => {
    render(<CommandWheelSection />)

    expect(await screen.findByText('Page 1', { exact: false })).toBeVisible()
    expect(screen.getByLabelText('page 1 slot 1')).toHaveValue('copy')
    expect(screen.getByLabelText('page 1 slot 2')).toHaveValue('paste')
    expect(screen.getByRole('button', { name: 'Save Command Wheel' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: 'move Paste up' }))
    expect(screen.getByLabelText('page 1 slot 1')).toHaveValue('paste')

    fireEvent.click(screen.getByLabelText('wheel modifier Alt'))
    fireEvent.click(screen.getByLabelText('wheel modifier Win'))
    const keyField = screen.getByRole('button', { name: 'wheel hotkey key' })
    fireEvent.click(keyField)
    fireEvent.keyDown(keyField, { key: 'k', code: 'KeyK', keyCode: 0x4B, which: 0x4B })
    expect(keyField).toHaveTextContent('Ctrl+Win+K')

    fireEvent.click(screen.getByRole('button', { name: 'Save Command Wheel' }))
    await waitFor(() => expect(api.saveWheel).toHaveBeenCalledWith({
      pages: [['paste', 'copy'], ['delete']],
      hotkey: { mods: 9, vk: 0x4B },
      // An older runtime without trigger block: the defaults are sent.
      trigger: { tap_open: true, hold_swipe: true, hold_ms: 250 },
    }))
    expect(await screen.findByRole('status')).toHaveTextContent('Command wheel saved. Hotkey: Ctrl+Win+K')
    expect(screen.getByRole('button', { name: 'Save Command Wheel' })).toBeDisabled()
  })

  it('Viktor 24.11.: Ctrl+Alt+Space without Alt is saved, stays after reload, a refused hotkey says why', async () => {
    api.saveWheel.mockImplementation(async (wheel) => ({
      ok: true,
      wheel: { ...wheel, hotkey: { ...wheel.hotkey, display: 'Ctrl+Space' } },
      registration: { pending: false, entries: [
        { slot: 'command_wheel', hotkey: 'Ctrl+Space', active: false, error_code: 1409,
          message: 'Ctrl+Space (Command wheel) is already taken by Windows or another program.' },
      ] },
    }))
    const { unmount } = render(<CommandWheelSection />)
    const field = await screen.findByRole('button', { name: 'wheel hotkey key' })
    expect(field).toHaveTextContent('Ctrl+Alt+Space')
    fireEvent.click(screen.getByLabelText('wheel modifier Alt'))
    expect(field).toHaveTextContent('Ctrl+Space')
    expect(screen.getByTestId('wheel-unsaved')).toHaveTextContent('Not saved yet')

    fireEvent.click(screen.getByRole('button', { name: 'Save Command Wheel' }))
    await waitFor(() => expect(api.saveWheel).toHaveBeenCalledWith(expect.objectContaining({
      hotkey: { mods: 1, vk: 0x20 },
    })))
    const result = await screen.findByRole('status')
    expect(result).toHaveTextContent('Command wheel saved (hotkey Ctrl+Space), but not active: Ctrl+Space (Command wheel) is already taken by Windows or another program.')
    // The choice stays visible, never silently the old value.
    expect(field).toHaveTextContent('Ctrl+Space')
    expect(screen.queryByTestId('wheel-unsaved')).toBeNull()
    unmount()

    // F5: the page loads the saved value again.
    api.getActions.mockResolvedValue({
      ...actionsResponse,
      wheel: { ...actionsResponse.wheel, hotkey: { mods: 1, vk: 0x20, display: 'Ctrl+Space' } },
    })
    render(<CommandWheelSection />)
    expect(await screen.findByRole('button', { name: 'wheel hotkey key' })).toHaveTextContent('Ctrl+Space')
  })

  it('supports pages and prevents saving an empty wheel', async () => {
    render(<CommandWheelSection />)
    await screen.findByText('Page 1', { exact: false })

    fireEvent.click(screen.getByRole('button', { name: 'Add page' }))
    expect(screen.getByTestId('wheel-page-2')).toBeVisible()
    fireEvent.change(screen.getByLabelText('add action to page 3'), { target: { value: 'copy' } })
    expect(screen.getByLabelText('page 3 slot 1')).toHaveValue('copy')

    fireEvent.change(screen.getByLabelText('page 1 slot 1'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('page 1 slot 1'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('page 2 slot 1'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('page 3 slot 1'), { target: { value: '' } })
    expect(screen.getByText('The wheel needs at least one action.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Save Command Wheel' })).toBeDisabled()
  })

  it('Viktor: Ctrl+Right click is shown without red text, Ctrl+side button 1 is recorded and saved, then set back', async () => {
    api.getActions.mockResolvedValue({
      ...actionsResponse,
      wheel: { ...actionsResponse.wheel, hotkey: { mods: 1, mouse: 'right', display: 'Ctrl+Right click' } },
    })
    api.saveWheel.mockImplementation(async (wheel) => ({ ok: true, wheel: { ...wheel, hotkey: { ...wheel.hotkey } } }))
    const status = { overlay_hotkeys: { ok: true, entries: [
      { slot: 'command_wheel', hotkey: 'Ctrl+Right click', active: true, message: '' },
    ] } }
    render(<CommandWheelSection status={status} />)
    const field = await screen.findByRole('button', { name: 'wheel hotkey key' })
    expect(field).toHaveTextContent('Ctrl+Right click')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByTestId('wheel-hotkey-status')).toHaveTextContent('Ctrl+Right click active')

    fireEvent.click(field)
    fireEvent.mouseDown(field, { button: 3, ctrlKey: true })
    expect(field).toHaveTextContent('Ctrl+Side button 1')
    fireEvent.click(screen.getByRole('button', { name: 'Save Command Wheel' }))
    await waitFor(() => expect(api.saveWheel).toHaveBeenLastCalledWith(expect.objectContaining({
      hotkey: { mods: 1, mouse: 'x1' },
    })))
    expect(await screen.findByRole('status')).toHaveTextContent('Command wheel saved. Hotkey: Ctrl+Side button 1')

    fireEvent.click(field)
    fireEvent.mouseDown(field, { button: 2, ctrlKey: true })
    expect(field).toHaveTextContent('Ctrl+Right click')
    fireEvent.click(screen.getByRole('button', { name: 'Save Command Wheel' }))
    await waitFor(() => expect(api.saveWheel).toHaveBeenLastCalledWith(expect.objectContaining({
      hotkey: { mods: 1, mouse: 'right' },
    })))
  })

  it('Viktor 24.11.: exactly one hotkey field, no "Add another hotkey"', async () => {
    render(<CommandWheelSection />)
    await screen.findByText('Page 1', { exact: false })
    expect(screen.queryByRole('button', { name: /add another hotkey/i })).toBeNull()
    expect(screen.getAllByRole('button', { name: /hotkey key$/ })).toHaveLength(1)
    expect(screen.queryByText(/More hotkey/)).toBeNull()
  })

  it('Viktor 24.11.: Clear asks before saving, saves hotkey null, says in one sentence how the wheel still opens', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    api.saveWheel.mockImplementation(async (wheel) => ({ ok: true, wheel: { ...wheel, hotkey: null } }))
    render(<CommandWheelSection />)
    const field = await screen.findByRole('button', { name: 'wheel hotkey key' })
    expect(screen.queryByTestId('wheel-no-hotkey')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'clear wheel hotkey' }))
    expect(field).toHaveTextContent('Not set')
    expect(screen.getByTestId('wheel-no-hotkey')).toHaveTextContent(NO_HOTKEY_TEXT)
    expect(screen.queryByRole('alert')).toBeNull()

    // First answer "Cancel": nothing is sent, the page says the hotkey stays.
    fireEvent.click(screen.getByRole('button', { name: 'Save Command Wheel' }))
    expect(confirm).toHaveBeenCalledWith(NO_HOTKEY_CONFIRM)
    expect(await screen.findByRole('status')).toHaveTextContent('Command wheel not saved: the hotkey stays Ctrl+Alt+Space.')
    expect(api.saveWheel).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Save Command Wheel' }))
    await waitFor(() => expect(api.saveWheel).toHaveBeenCalledWith({
      pages: [['copy', 'paste'], ['delete']],
      hotkey: null,
      trigger: { tap_open: true, hold_swipe: true, hold_ms: 250 },
    }))
    expect(await screen.findByRole('status')).toHaveTextContent('Command wheel saved. Hotkey: none')
    expect(screen.queryByRole('alert')).toBeNull()
    confirm.mockRestore()
  })

  it('a failed save is shown in red next to the button', async () => {
    api.saveWheel.mockRejectedValueOnce(Object.assign(new Error('hotkey_conflict'), { detail: 'Ctrl+Side button 1 is assigned twice.' }))
    render(<CommandWheelSection />)
    await screen.findByText('Page 1', { exact: false })
    fireEvent.click(screen.getByRole('button', { name: 'move Paste up' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save Command Wheel' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Command wheel not saved: Ctrl+Side button 1 is assigned twice.')
  })

  it('edits tap, hold and threshold and saves them as trigger', async () => {
    api.getActions.mockResolvedValue({
      ...actionsResponse,
      wheel: {
        ...actionsResponse.wheel,
        // An older runtime still sends mouse_button: it is not sent back.
        trigger: { tap_open: true, hold_swipe: true, hold_ms: 250, mouse_button: 'none' },
        trigger_limits: { hold_ms_min: 100, hold_ms_max: 2000 },
      },
    })
    render(<CommandWheelSection />)
    await screen.findByText('Page 1', { exact: false })
    expect(screen.queryByLabelText('wheel mouse side button')).toBeNull()
    expect(screen.getByLabelText('wheel tap to open')).toBeChecked()
    expect(screen.getByLabelText('wheel hold and swipe')).toBeChecked()

    fireEvent.change(screen.getByLabelText('wheel hold threshold ms'), { target: { value: '5000' } })
    expect(screen.getByRole('alert')).toHaveTextContent('between 100 and 2000 ms')
    expect(screen.getByRole('button', { name: 'Save Command Wheel' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('wheel hold threshold ms'), { target: { value: '400' } })

    // Both ways off would leave a wheel that can never be used.
    fireEvent.click(screen.getByLabelText('wheel tap to open'))
    fireEvent.click(screen.getByLabelText('wheel hold and swipe'))
    expect(screen.getByRole('alert')).toHaveTextContent('Keep at least one')
    expect(screen.getByRole('button', { name: 'Save Command Wheel' })).toBeDisabled()
    fireEvent.click(screen.getByLabelText('wheel hold and swipe'))

    fireEvent.click(screen.getByRole('button', { name: 'Save Command Wheel' }))
    await waitFor(() => expect(api.saveWheel).toHaveBeenCalledWith({
      pages: [['copy', 'paste'], ['delete']],
      hotkey: { mods: 5, vk: 0x20 },
      trigger: { tap_open: false, hold_swipe: true, hold_ms: 400 },
    }))
  })

  it('every button of the wheel pages shows a word or an inline symbol, a tooltip and a name', async () => {
    render(<CommandWheelSection />)
    await screen.findByText('Page 1', { exact: false })
    const up = screen.getByRole('button', { name: 'move Paste up' })
    expect(up).toHaveTextContent('Up')
    expect(up).toHaveAttribute('title', 'Move Paste one field up')
    expect(up.querySelector('svg.button-icon[data-icon="arrow-up"]')).not.toBeNull()
    const down = screen.getByRole('button', { name: 'move Copy down' })
    expect(down).toHaveTextContent('Down')
    expect(down.querySelector('svg.button-icon[data-icon="arrow-down"]')).not.toBeNull()

    for (const button of screen.getByTestId('command-wheel-section').querySelectorAll('button')) {
      // Visible content must not depend on the Font Awesome CDN alone.
      expect(button.textContent.trim() !== '' || button.querySelector('svg') !== null).toBe(true)
      expect(button.getAttribute('title')).toBeTruthy()
      expect(button.getAttribute('aria-label') || button.textContent.trim()).toBeTruthy()
    }
    for (const select of screen.getByTestId('command-wheel-section').querySelectorAll('select')) {
      expect(select.getAttribute('aria-label')).toBeTruthy()
    }
  })

  it('shows load failures and retries', async () => {
    api.getActions.mockRejectedValueOnce(new Error('runtime unavailable'))
    render(<CommandWheelSection />)
    expect(await screen.findByText('Command wheel settings unavailable: runtime unavailable')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Page 1', { exact: false })).toBeVisible()
    expect(api.getActions).toHaveBeenCalledTimes(2)
  })
})
