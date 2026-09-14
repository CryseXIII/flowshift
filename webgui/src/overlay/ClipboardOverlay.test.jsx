import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ClipboardOverlay from './ClipboardOverlay.jsx'
import { mergeTransferProgress, progressLine } from '../clipboardFormat.js'
import * as api from '../api.js'

vi.mock('../api.js', () => ({
  getClipboardItems: vi.fn(),
  getClipboardProgress: vi.fn(),
  getClipboardStatus: vi.fn(),
  pasteItem: vi.fn(),
  requestItem: vi.fn(),
  pinItem: vi.fn(),
  deleteItem: vi.fn(),
  syncClipboard: vi.fn(),
  subscribeSSE: vi.fn(),
}))

function item(i, overrides = {}) {
  return { item_id: `item-${i}`, kind: 'text', preview_text: `Entry ${i}`, size: 10 + i, available: true, pinned: false, ...overrides }
}

const DATA = {
  profile: 'device:peer-a',
  profiles: [{ identity: 'device:peer-a', label: 'Peer A', connected: true }],
}

describe('clipboardFormat', () => {
  it('merges legacy jobs with stream V2 sessions, V2 winning per item', () => {
    const merged = mergeTransferProgress(
      { a: { status: 'running', percent: 40, received_bytes: 4, total_bytes: 10, bytes_per_second: 2, eta_seconds: 3 },
        b: { status: 'failed', error: 'disk_full' } },
      [{ item_id: 'a', state: 'receiving', percent: 55.5, bytes_done: 555, total_bytes: 1000, rate_bytes_per_s: 1048576, eta_seconds: 0.4, file_index: 1, file_count: 3 },
       { item_id: 'c', state: 'waiting_reconnect', percent: 20 },
       { item_id: 'd', state: 'completed', percent: 100 },
       { state: 'receiving' }],
    )
    expect(merged.a.strategy).toBe('stream_v2')
    expect(merged.a.status).toBe('running')
    expect(progressLine(merged.a)).toEqual({ kind: 'running', percent: 55.5, text: '56% · 555 B/1000 B · 1.0 MB/s · ETA 0s · file 2/3' })
    expect(progressLine(merged.b)).toEqual({ kind: 'failed', text: 'Failed: disk_full' })
    expect(progressLine(merged.c)).toEqual({ kind: 'paused', text: 'Paused 20%', percent: 20 })
    expect(progressLine(merged.d)).toBeNull()
    expect(Object.keys(merged)).toEqual(['a', 'b', 'c', 'd'])
  })
})

describe('ClipboardOverlay', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useRealTimers()
    api.getClipboardItems.mockResolvedValue({ items: Array.from({ length: 40 }, (_, i) => item(i)) })
    api.getClipboardProgress.mockResolvedValue({})
    api.getClipboardStatus.mockResolvedValue({ ok: true, diagnostics: { stream_v2: [] } })
    api.pasteItem.mockResolvedValue({ set: true })
    api.requestItem.mockResolvedValue({ ok: true })
    api.pinItem.mockResolvedValue({ pinned: true })
    api.deleteItem.mockResolvedValue({ deleted: true })
    api.syncClipboard.mockResolvedValue({ ok: true })
    api.subscribeSSE.mockImplementation(() => () => {})
  })

  it('keeps the scroll offset and the same list node across data refreshes', async () => {
    render(<ClipboardOverlay data={DATA} onClose={vi.fn()} />)
    await waitFor(() => expect(screen.getAllByTestId('clip-row')).toHaveLength(40))
    const list = screen.getByTestId('clipboard-list')
    list.scrollTop = 333
    fireEvent.scroll(list)

    const sse = api.subscribeSSE.mock.calls[0][0]
    api.getClipboardItems.mockResolvedValue({
      items: [item(99, { preview_text: 'Newest on top' }), ...Array.from({ length: 40 }, (_, i) => item(i))],
    })
    await act(async () => { sse({ type: 'clipboard_update', profiles: ['device:peer-a'] }) })
    await waitFor(() => expect(screen.getAllByTestId('clip-row')).toHaveLength(41))
    expect(screen.getByTestId('clipboard-list')).toBe(list)
    expect(list.scrollTop).toBe(333)

    api.getClipboardProgress.mockResolvedValue({ 'item-3': { status: 'running', percent: 50, received_bytes: 5, total_bytes: 10 } })
    await waitFor(() => expect(screen.getByTestId('clip-progress')).toHaveTextContent('50%'))
    expect(list.scrollTop).toBe(333)
  })

  it('offers Set for available items and Get for missing ones and calls the API', async () => {
    const onClose = vi.fn()
    api.getClipboardItems.mockResolvedValue({ items: [item(1), item(2, { available: false })] })
    render(<ClipboardOverlay data={DATA} onClose={onClose} />)
    const rows = await screen.findAllByTestId('clip-row')
    expect(rows).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: 'Download' }))
    await waitFor(() => expect(api.requestItem).toHaveBeenCalledWith('device:peer-a', 'item-2'))
    fireEvent.click(screen.getByRole('button', { name: 'Set clipboard' }))
    await waitFor(() => expect(api.pasteItem).toHaveBeenCalledWith('device:peer-a', 'item-1'))
    expect(onClose).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getAllByRole('button', { name: 'Pin' })[0])
    await waitFor(() => expect(api.pinItem).toHaveBeenCalledWith('device:peer-a', 'item-1', true))
    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[1])
    await waitFor(() => expect(api.deleteItem).toHaveBeenCalledWith('device:peer-a', 'item-2'))
  })

  it('renders a runtime error and filters by search', async () => {
    api.getClipboardItems.mockResolvedValue({ items: [item(1), item(2, { preview_text: 'Invoice PDF' })] })
    render(<ClipboardOverlay data={DATA} onClose={vi.fn()} />)
    await screen.findAllByTestId('clip-row')
    fireEvent.change(screen.getByLabelText('Search clipboard'), { target: { value: 'invoice' } })
    expect(screen.getAllByTestId('clip-row')).toHaveLength(1)
    expect(screen.getByTestId('clip-row')).toHaveAttribute('data-item', 'item-2')

    api.pasteItem.mockRejectedValue(new Error('file data not present'))
    fireEvent.click(screen.getByRole('button', { name: 'Set clipboard' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('file data not present'))
  })

  it('shows the no-profile state without calling the API', async () => {
    render(<ClipboardOverlay data={{}} onClose={vi.fn()} />)
    expect(screen.getByTestId('clipboard-overlay')).toHaveTextContent('No profile')
    await new Promise((r) => setTimeout(r, 20))
    expect(api.getClipboardItems).not.toHaveBeenCalled()
  })
})
