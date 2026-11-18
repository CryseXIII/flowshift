import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import OverlayShell from './OverlayShell.jsx'
import * as api from '../api.js'

vi.mock('../api.js', () => ({
  executeAction: vi.fn(),
  getClipboardProfiles: vi.fn(),
  getClipboardItems: vi.fn(),
  getClipboardProgress: vi.fn(),
  getClipboardStatus: vi.fn(),
  pasteItem: vi.fn(),
  requestItem: vi.fn(),
  pinItem: vi.fn(),
  copyItemCardText: vi.fn(),
  deleteItem: vi.fn(),
  syncClipboard: vi.fn(),
  subscribeSSE: vi.fn(),
}))

function show(mode, data) {
  act(() => {
    window.flowshiftOverlay.update({
      mode, target: { kind: 'local', identity: 'local' }, x: 10, y: 20, dpi: 96, scale: 1, data,
    })
  })
}

const WHEEL = { pages: [['copy', 'paste'], ['undo']], actions: { copy: { label: 'Copy' }, paste: { label: 'Paste' }, undo: { label: 'Undo' } } }

describe('OverlayShell', () => {
  let bridge

  beforeEach(() => {
    vi.clearAllMocks()
    bridge = { hide_overlay: vi.fn(() => Promise.resolve(true)), overlay_event: vi.fn(() => Promise.resolve(true)) }
    window.pywebview = { api: bridge }
    api.getClipboardItems.mockResolvedValue({ items: [] })
    api.getClipboardProgress.mockResolvedValue({})
    api.getClipboardStatus.mockResolvedValue({ ok: true, diagnostics: { stream_v2: [] } })
    api.subscribeSSE.mockImplementation(() => () => {})
    api.executeAction.mockResolvedValue({ ok: true })
  })

  afterEach(() => {
    vi.useRealTimers()
    delete window.pywebview
  })

  it('stops polling the clipboard API once the host hides the overlay', async () => {
    vi.useFakeTimers()
    render(<OverlayShell />)
    show('clipboard', { profile: 'local', profiles: [] })
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    const items = api.getClipboardItems.mock.calls.length
    const progress = api.getClipboardProgress.mock.calls.length
    expect(items).toBeGreaterThan(1)
    expect(progress).toBeGreaterThan(1)

    act(() => { window.flowshiftOverlay.hide() })
    await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
    expect(api.getClipboardItems).toHaveBeenCalledTimes(items)
    expect(api.getClipboardProgress).toHaveBeenCalledTimes(progress)
    expect(document.querySelector('.clip-panel')).toBeNull()
  })

  it('renders nothing while hidden and the wheel again on the next show', () => {
    render(<OverlayShell />)
    show('command_wheel', WHEEL)
    expect(screen.getByTestId('command-wheel')).toBeInTheDocument()
    act(() => { window.flowshiftOverlay.hide() })
    expect(screen.queryByTestId('command-wheel')).toBeNull()
    expect(document.querySelector('.diagnostic-card')).toBeNull()
    show('command_wheel', WHEEL)
    expect(screen.getByTestId('command-wheel')).toBeInTheDocument()
  })

  it('tells the host the visible wheel page and the reason of each hide', async () => {
    render(<OverlayShell />)
    show('command_wheel', WHEEL)
    expect(bridge.overlay_event).toHaveBeenLastCalledWith('wheel_page', { page: 0 })
    fireEvent.wheel(screen.getByTestId('command-wheel'), { deltaY: 120 })
    expect(bridge.overlay_event).toHaveBeenLastCalledWith('wheel_page', { page: 1 })

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(bridge.hide_overlay).toHaveBeenLastCalledWith('escape')

    // Executing leaves hiding to the runtime, so React does not race a
    // clipboard overlay opened by the action.
    bridge.hide_overlay.mockClear()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Undo' }))
    await waitFor(() => expect(api.executeAction).toHaveBeenCalledWith('undo', { source: 'command_wheel' }))
    expect(bridge.hide_overlay).not.toHaveBeenCalled()
  })
})
