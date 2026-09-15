import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import CommandWheelSection from './CommandWheelSection.jsx'
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
    hotkey: { mods: 3, vk: 0x20, display: 'Ctrl+Alt+Space' },
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
        hotkey: { ...wheel.hotkey, display: 'Ctrl+Win+K' },
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
    }))
    expect(await screen.findByRole('status')).toHaveTextContent('Command wheel saved. Hotkey: Ctrl+Win+K')
    expect(screen.getByRole('button', { name: 'Save Command Wheel' })).toBeDisabled()
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

  it('shows load failures and retries', async () => {
    api.getActions.mockRejectedValueOnce(new Error('runtime unavailable'))
    render(<CommandWheelSection />)
    expect(await screen.findByText('Command wheel settings unavailable: runtime unavailable')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Page 1', { exact: false })).toBeVisible()
    expect(api.getActions).toHaveBeenCalledTimes(2)
  })
})
