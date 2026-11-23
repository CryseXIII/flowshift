import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ClipboardOverlay from './ClipboardOverlay.jsx'
import { fmtEta, mergeTransferProgress, progressLine, progressRows } from '../clipboardFormat.js'
import * as api from '../api.js'

vi.mock('../api.js', () => ({
  getClipboardProfiles: vi.fn(),
  getClipboardItems: vi.fn(),
  getClipboardProgress: vi.fn(),
  getClipboardStatus: vi.fn(),
  pasteItem: vi.fn(),
  requestItem: vi.fn(),
  pinItem: vi.fn(),
  copyItemCardText: vi.fn(),
  deleteItem: vi.fn(),
  deleteItems: vi.fn(),
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
      [{ item_id: 'a', state: 'receiving', percent: 55.5, bytes_done: 555, total_bytes: 1000, rate_bytes_per_s: 1048576, eta_seconds: 2, file_index: 2, file_ordinal: 2, file_count: 3, current_file: 'batch/two.bin', current_file_bytes: 55, current_file_size: 100, current_file_percent: 55, last_file_duration_seconds: 1.2 },
       { item_id: 'c', state: 'waiting_reconnect', percent: 20 },
       { item_id: 'd', state: 'completed', percent: 100 },
       { state: 'receiving' }],
    )
    expect(merged.a.strategy).toBe('stream_v2')
    expect(merged.a.status).toBe('running')
    expect(progressLine(merged.a)).toEqual({ kind: 'running', percent: 55.5, text: '56% · 555 B/1000 B · 1.0 MB/s · ETA 00:00:02 · file 2/3 · last file 00:00:01' })
    expect(progressRows(merged.a, 'file_batch')).toEqual([
      { kind: 'running', percent: 55, text: 'Current 2/3: batch/two.bin · 55% · 55 B/100 B' },
      { kind: 'running', percent: 55.5, text: 'Total · 56% · 555 B/1000 B · 1.0 MB/s · ETA 00:00:02 · file 2/3 · last file 00:00:01' },
    ])
    expect(progressLine(merged.b)).toEqual({ kind: 'failed', text: 'Failed: disk_full' })
    expect(progressLine(merged.c)).toEqual({ kind: 'paused', text: 'Paused 20%', percent: 20 })
    expect(progressLine(merged.d)).toBeNull()
    expect(Object.keys(merged)).toEqual(['a', 'b', 'c', 'd'])
  })

  it('formats ETA and durations as HH:mm:ss without wrapping hours', () => {
    expect([0, 59, 60, 3599, 3600, 90061].map(fmtEta)).toEqual([
      '00:00:00', '00:00:59', '00:01:00', '00:59:59', '01:00:00', '25:01:01',
    ])
    expect(fmtEta(null)).toBe('')
    expect(fmtEta(Number.POSITIVE_INFINITY)).toBe('')
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
    api.copyItemCardText.mockResolvedValue({ copied: true, count: 2, truncated: false })
    api.deleteItem.mockResolvedValue({ deleted: true })
    api.deleteItems.mockImplementation(async (_profile, ids) => ({ deleted: ids.length, item_ids: ids }))
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

  it('defaults to the local history when the payload has no profile', async () => {
    render(<ClipboardOverlay data={{}} onClose={vi.fn()} />)
    await waitFor(() => expect(api.getClipboardItems).toHaveBeenCalledWith('local'))
    expect(await screen.findAllByTestId('clip-row')).toHaveLength(40)
  })

  it('renders one type-specific card per item and separates Recent from Pinned', async () => {
    api.getClipboardItems.mockResolvedValue({ items: [
      item(1, { preview_text: 'Readable text', size: 13 }),
      item(2, {
        kind: 'file', display_name: 'report.pdf', preview_text: '', size: 2048,
        file_card: { root_names: ['report.pdf'], file_names: ['report.pdf'], relative_paths: ['report.pdf'], total_files: 1, total_size: 2048, truncated: false },
      }),
      item(3, {
        kind: 'file_batch', display_name: 'Project', preview_text: '', pinned: true,
        file_card: { root_names: ['Project'], file_names: ['a.txt', 'b.txt'], relative_paths: ['Project/a.txt', 'Project/b.txt'], total_files: 2, total_size: 30, truncated: false },
      }),
    ] })
    render(<ClipboardOverlay data={DATA} onClose={vi.fn()} />)

    expect(await screen.findAllByTestId('clip-row')).toHaveLength(2)
    expect(screen.getByText('Readable text')).toBeVisible()
    expect(screen.getAllByText('report.pdf')).toHaveLength(1)
    expect(screen.queryByText('Project/a.txt, Project/b.txt')).not.toBeInTheDocument()

    const list = screen.getByTestId('clipboard-list')
    list.scrollTop = 120
    fireEvent.click(screen.getByRole('button', { name: /Pinned 1/ }))
    expect(screen.getAllByTestId('clip-row')).toHaveLength(1)
    expect(list.scrollTop).toBe(0)
    expect(screen.getByText('Project/a.txt, Project/b.txt')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Copy relative paths' }))
    await waitFor(() => expect(api.copyItemCardText).toHaveBeenCalledWith(
      'device:peer-a', 'item-3', 'relative_paths'))
  })

  it('does not run the primary action when an item action is double-clicked', async () => {
    api.getClipboardItems.mockResolvedValue({ items: [item(1)] })
    render(<ClipboardOverlay data={DATA} onClose={vi.fn()} />)

    const pin = await screen.findByRole('button', { name: 'Pin' })
    fireEvent.click(pin)
    fireEvent.click(pin)
    fireEvent.doubleClick(pin)
    await waitFor(() => expect(api.pinItem).toHaveBeenCalledTimes(1))
    expect(api.pasteItem).not.toHaveBeenCalled()
  })

  describe('multi-selection', () => {
    const rowIds = () => screen.queryAllByTestId('clip-row').map((row) => row.getAttribute('data-item'))
    const checkedIds = () => screen.queryAllByTestId('clip-row')
      .filter((row) => row.getAttribute('data-checked') === 'true')
      .map((row) => row.getAttribute('data-item'))
    const count = () => screen.getByTestId('selection-count').textContent

    beforeEach(() => {
      api.getClipboardItems.mockResolvedValue({ items: [item(1), item(2), item(3), item(4), item(5, { pinned: true })] })
    })

    it('selects by checkbox, Ctrl-click and Shift-click range and shows the count', async () => {
      render(<ClipboardOverlay data={DATA} onClose={vi.fn()} />)
      await waitFor(() => expect(rowIds()).toEqual(['item-1', 'item-2', 'item-3', 'item-4']))
      expect(count()).toBe('0 of 5 selected')
      expect(screen.getByRole('button', { name: 'Delete selected' })).toBeDisabled()

      fireEvent.click(screen.getAllByRole('checkbox')[0])
      expect(checkedIds()).toEqual(['item-1'])
      fireEvent.click(screen.getAllByTestId('clip-row')[3], { shiftKey: true })
      expect(checkedIds()).toEqual(['item-1', 'item-2', 'item-3', 'item-4'])
      fireEvent.click(screen.getAllByTestId('clip-row')[1], { ctrlKey: true })
      expect(checkedIds()).toEqual(['item-1', 'item-3', 'item-4'])
      expect(count()).toBe('3 of 5 selected')
      expect(screen.getByRole('button', { name: 'Delete selected' })).toBeEnabled()
      expect(api.pasteItem).not.toHaveBeenCalled()
    })

    it('All, Invert and None act on every entry, Recent and Pinned', async () => {
      render(<ClipboardOverlay data={DATA} onClose={vi.fn()} />)
      await waitFor(() => expect(rowIds()).toHaveLength(4))
      fireEvent.click(screen.getByRole('button', { name: 'All' }))
      expect(checkedIds()).toEqual(['item-1', 'item-2', 'item-3', 'item-4'])
      expect(count()).toBe('5 of 5 selected')
      expect(screen.getByRole('button', { name: 'Delete selected' })).toHaveTextContent('5')

      // Switching tabs keeps the selection: the pinned entry is checked too.
      fireEvent.click(screen.getByRole('button', { name: /Pinned 1/ }))
      expect(rowIds()).toEqual(['item-5'])
      expect(checkedIds()).toEqual(['item-5'])
      expect(count()).toBe('5 of 5 selected')

      fireEvent.click(screen.getByRole('button', { name: 'None' }))
      expect(checkedIds()).toEqual([])
      expect(count()).toBe('0 of 5 selected')
      fireEvent.click(screen.getByRole('button', { name: 'Invert' }))
      expect(checkedIds()).toEqual(['item-5'])
      expect(count()).toBe('5 of 5 selected')
      fireEvent.click(screen.getByRole('checkbox'))
      fireEvent.click(screen.getByRole('button', { name: /Recent 4/ }))
      fireEvent.click(screen.getAllByRole('checkbox')[0])
      fireEvent.click(screen.getByRole('button', { name: 'Invert' }))
      expect(checkedIds()).toEqual(['item-1'])
      expect(count()).toBe('2 of 5 selected')
      fireEvent.click(screen.getByRole('button', { name: /Pinned 1/ }))
      expect(checkedIds()).toEqual(['item-5'])
    })

    it('All + Delete empties the whole list, pinned entries included, without confirmation', async () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
      render(<ClipboardOverlay data={DATA} onClose={vi.fn()} />)
      await waitFor(() => expect(rowIds()).toHaveLength(4))
      fireEvent.click(screen.getByRole('button', { name: 'All' }))
      api.getClipboardItems.mockResolvedValue({ items: [] })
      fireEvent.click(screen.getByRole('button', { name: 'Delete selected' }))
      expect(rowIds()).toEqual([])
      await waitFor(() => expect(api.deleteItems).toHaveBeenCalledTimes(1))
      expect(api.deleteItems).toHaveBeenCalledWith('device:peer-a', ['item-1', 'item-2', 'item-3', 'item-4', 'item-5'])
      await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Deleted 5 items'))
      expect(screen.getByRole('button', { name: /Recent 0/ })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /Pinned 0/ })).toBeInTheDocument()
      expect(count()).toBe('0 of 0 selected')
      expect(confirmSpy).not.toHaveBeenCalled()
      expect(api.deleteItem).not.toHaveBeenCalled()
      expect(api.pasteItem).not.toHaveBeenCalled()
      confirmSpy.mockRestore()
    })

    it('deletes a hand-picked pinned item without confirmation', async () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
      render(<ClipboardOverlay data={DATA} onClose={vi.fn()} />)
      await waitFor(() => expect(rowIds()).toHaveLength(4))
      fireEvent.click(screen.getByRole('button', { name: /Pinned 1/ }))
      fireEvent.click(screen.getByRole('checkbox'))
      fireEvent.click(screen.getByRole('button', { name: 'Delete selected' }))
      await waitFor(() => expect(api.deleteItems).toHaveBeenCalledWith('device:peer-a', ['item-5']))
      expect(confirmSpy).not.toHaveBeenCalled()
      confirmSpy.mockRestore()
    })

    it('never sets the clipboard from selection clicks, but double-click still does', async () => {
      const onClose = vi.fn()
      render(<ClipboardOverlay data={DATA} onClose={onClose} />)
      await waitFor(() => expect(rowIds()).toHaveLength(4))
      const row = screen.getAllByTestId('clip-row')[0]
      fireEvent.click(row, { ctrlKey: true })
      fireEvent.click(row, { ctrlKey: true })
      fireEvent.doubleClick(row, { ctrlKey: true })
      fireEvent.doubleClick(row, { shiftKey: true })
      const box = screen.getAllByRole('checkbox')[0]
      fireEvent.click(box)
      fireEvent.click(box)
      fireEvent.doubleClick(box)
      expect(api.pasteItem).not.toHaveBeenCalled()
      fireEvent.doubleClick(row)
      await waitFor(() => expect(api.pasteItem).toHaveBeenCalledWith('device:peer-a', 'item-1'))
      expect(onClose).toHaveBeenCalledTimes(1)
    })

    it('supports Ctrl+A, Ctrl+I, Ctrl+Shift+A and Delete outside the search field', async () => {
      render(<ClipboardOverlay data={DATA} onClose={vi.fn()} />)
      await waitFor(() => expect(rowIds()).toHaveLength(4))
      const search = screen.getByLabelText('Search clipboard')
      fireEvent.keyDown(search, { key: 'a', ctrlKey: true })
      expect(checkedIds()).toEqual([])
      fireEvent.keyDown(window, { key: 'a', ctrlKey: true })
      expect(checkedIds()).toHaveLength(4)
      fireEvent.keyDown(window, { key: 'A', ctrlKey: true, shiftKey: true })
      expect(checkedIds()).toEqual([])
      fireEvent.click(screen.getAllByRole('checkbox')[1])
      fireEvent.keyDown(window, { key: 'i', ctrlKey: true })
      expect(checkedIds()).toEqual(['item-1', 'item-3', 'item-4'])
      expect(count()).toBe('4 of 5 selected')
      fireEvent.keyDown(search, { key: 'Delete' })
      expect(api.deleteItems).not.toHaveBeenCalled()
      fireEvent.keyDown(window, { key: 'Delete' })
      await waitFor(() => expect(api.deleteItems).toHaveBeenCalledWith('device:peer-a', ['item-1', 'item-3', 'item-4', 'item-5']))
    })
  })
})
