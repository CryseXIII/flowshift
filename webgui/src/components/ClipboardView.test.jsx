import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ClipboardView from './ClipboardView.jsx'
import * as api from '../api.js'

vi.mock('../api.js', () => ({
  getClipboardProfiles: vi.fn(),
  getClipboardItems: vi.fn(),
  getClipboardItem: vi.fn(),
  getClipboardProgress: vi.fn(),
  getClipboardStatus: vi.fn(),
  getThumbnail: vi.fn(),
  pasteItem: vi.fn(),
  deleteItem: vi.fn(),
  pinItem: vi.fn(),
  copyItemCardText: vi.fn(),
  requestItem: vi.fn(),
  syncClipboard: vi.fn(),
  deleteItems: vi.fn(),
  subscribeSSE: vi.fn(),
}))

const status = {
  peers: [{ identity: 'device:alpha', name: 'Alpha', host: '10.0.0.2', connected: true }],
}
const firstItems = [
  { item_id: 'item-1', kind: 'file_batch', display_name: 'Project files', size: 4096, available: false },
  { item_id: 'item-2', kind: 'text', preview_text: 'Second item', size: 12, available: true },
]

describe('ClipboardView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getClipboardProfiles.mockResolvedValue({ profiles: [] })
    api.getClipboardItems.mockResolvedValue({ items: firstItems })
    api.getClipboardProgress.mockResolvedValue({})
    api.getClipboardStatus.mockResolvedValue({ diagnostics: { stream_v2: [] } })
    api.copyItemCardText.mockResolvedValue({ copied: true, count: 2, truncated: false })
    api.subscribeSSE.mockReturnValue(vi.fn())
  })

  it('keeps the same list node and scroll offset across an item refresh', async () => {
    api.getClipboardItems
      .mockResolvedValueOnce({ items: firstItems })
      .mockResolvedValueOnce({ items: [...firstItems, { item_id: 'item-3', kind: 'text', preview_text: 'New item', available: true }] })
    render(<ClipboardView status={status} />)

    await screen.findAllByText('Project files')
    const list = screen.getByTestId('clipboard-item-list')
    list.scrollTop = 173
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    await screen.findByText('New item')
    expect(screen.getByTestId('clipboard-item-list')).toBe(list)
    expect(list.scrollTop).toBe(173)
  })

  it('merges stream V2 status and renders concrete transfer progress', async () => {
    api.getClipboardStatus.mockResolvedValue({
      diagnostics: {
        stream_v2: [{
          item_id: 'item-1', state: 'receiving', percent: 50,
          bytes_done: 2048, total_bytes: 4096, rate_bytes_per_s: 1024,
          eta_seconds: 2, file_index: 2, file_ordinal: 2, file_count: 3,
          current_file: 'project/two.bin', current_file_bytes: 256,
          current_file_size: 1024, current_file_percent: 25,
          last_file_duration_seconds: 4,
        }],
      },
    })
    api.getClipboardItem.mockResolvedValue({
      item: firstItems[0], kind: 'file_batch', file_manifest: { entries: [] },
    })
    render(<ClipboardView status={status} />)

    expect(await screen.findByText('Current 2/3: project/two.bin · 25% · 256 B/1.0 KB')).toBeVisible()
    expect(screen.getByText(/Total · 50% · 2\.0 KB\/4\.0 KB · 1\.0 KB\/s · ETA 00:00:02 · file 2\/3 · last file 00:00:04/)).toBeVisible()
    expect(screen.getAllByTestId('clip-progress')).toHaveLength(2)
    fireEvent.click(screen.getAllByTestId('clipboard-card')[0])
    await waitFor(() => expect(api.getClipboardItem).toHaveBeenCalledWith('device:alpha', 'item-1'))
    expect(screen.getAllByTestId('detail-progress')).toHaveLength(2)
    expect(api.getClipboardProgress).toHaveBeenCalled()
    expect(api.getClipboardStatus).toHaveBeenCalledWith('device:alpha')
  })

  it('keeps the list mounted when a refresh returns no items', async () => {
    api.getClipboardItems
      .mockResolvedValueOnce({ items: firstItems })
      .mockResolvedValueOnce({ items: [] })
    render(<ClipboardView status={status} />)
    await screen.findAllByText('Project files')
    const list = screen.getByTestId('clipboard-item-list')

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText('No clipboard items yet. Copy something to fill your local history.')).toBeVisible()
    expect(screen.getByTestId('clipboard-item-list')).toBe(list)
    await waitFor(() => expect(api.getClipboardItems).toHaveBeenCalledTimes(2))
  })

  it('defaults to the local history when no peer is configured', async () => {
    api.getClipboardProfiles.mockResolvedValue({
      profiles: [{ identity: 'local', label: 'This PC', connected: true }],
    })
    render(<ClipboardView status={{ peers: [] }} />)
    await waitFor(() => expect(api.getClipboardItems).toHaveBeenCalledWith('local'))
    expect(await screen.findAllByTestId('clipboard-card')).toHaveLength(2)
    expect(api.getClipboardItems).toHaveBeenCalledWith('local')
  })

  it('does not let an old profile request overwrite the selected profile', async () => {
    let resolveAlpha
    let resolveBeta
    api.getClipboardItems.mockImplementation((profile) => new Promise((resolve) => {
      if (profile === 'device:alpha') resolveAlpha = resolve
      else resolveBeta = resolve
    }))
    const twoPeers = { peers: [
      ...status.peers,
      { identity: 'device:beta', name: 'Beta', host: '10.0.0.3', connected: true },
    ] }
    render(<ClipboardView status={twoPeers} />)
    await waitFor(() => expect(resolveAlpha).toBeTypeOf('function'))

    fireEvent.change(screen.getByLabelText('Clipboard profile'), { target: { value: 'device:beta' } })
    await waitFor(() => expect(resolveBeta).toBeTypeOf('function'))
    resolveBeta({ items: [{ item_id: 'beta-item', kind: 'text', preview_text: 'Beta current', available: true }] })
    expect(await screen.findByText('Beta current')).toBeVisible()
    resolveAlpha({ items: [{ item_id: 'alpha-item', kind: 'text', preview_text: 'Alpha stale', available: true }] })
    await Promise.resolve()
    expect(screen.queryByText('Alpha stale')).not.toBeInTheDocument()
    expect(screen.getByText('Beta current')).toBeVisible()
  })

  it('uses the same Recent and Pinned cards with safe batch copy actions', async () => {
    api.getClipboardItems.mockResolvedValue({ items: [
      { item_id: 'recent-text', kind: 'text', preview_text: 'A readable note', size: 15, available: true, pinned: false },
      {
        item_id: 'pinned-batch', kind: 'file_batch', display_name: 'Assets', available: true, pinned: true,
        file_card: { root_names: ['Assets'], file_names: ['hero.png', 'map.json'], relative_paths: ['Assets/hero.png', 'Assets/map.json'], total_files: 2, total_size: 90, truncated: false },
      },
    ] })
    render(<ClipboardView status={status} />)

    expect(await screen.findByText('A readable note')).toBeVisible()
    expect(screen.queryByText('Assets/hero.png, Assets/map.json')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Pinned 1/ }))
    expect(screen.getByText('Assets/hero.png, Assets/map.json')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Copy file names' }))
    await waitFor(() => expect(api.copyItemCardText).toHaveBeenCalledWith(
      'device:alpha', 'pinned-batch', 'file_names'))
  })

  it('clears detail selection when switching clipboard views', async () => {
    api.getClipboardItems.mockResolvedValue({ items: [
      { item_id: 'recent-text', kind: 'text', preview_text: 'Selected note', size: 15, available: true, pinned: false },
      { item_id: 'pinned-text', kind: 'text', preview_text: 'Pinned note', size: 12, available: true, pinned: true },
    ] })
    api.getClipboardItem.mockResolvedValue({
      item: { item_id: 'recent-text', kind: 'text', preview_text: 'Selected note', available: true },
      kind: 'text', text: 'Detail content',
    })
    render(<ClipboardView status={status} />)

    fireEvent.click(await screen.findByText('Selected note'))
    expect(await screen.findByText('Detail content')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: /Pinned 1/ }))
    expect(screen.queryByText('Detail content')).not.toBeInTheDocument()
    expect(screen.getByText('Select an item')).toBeVisible()
  })

  it('ignores a stale detail response after switching views', async () => {
    let resolveDetail
    api.getClipboardItems.mockResolvedValue({ items: [
      { item_id: 'recent-text', kind: 'text', preview_text: 'Slow note', size: 15, available: true, pinned: false },
      { item_id: 'pinned-text', kind: 'text', preview_text: 'Pinned note', size: 12, available: true, pinned: true },
    ] })
    api.getClipboardItem.mockReturnValue(new Promise((resolve) => { resolveDetail = resolve }))
    render(<ClipboardView status={status} />)

    fireEvent.click(await screen.findByText('Slow note'))
    fireEvent.click(screen.getByRole('button', { name: /Pinned 1/ }))
    resolveDetail({
      item: { item_id: 'recent-text', kind: 'text', preview_text: 'Slow note', available: true },
      kind: 'text', text: 'Late detail',
    })
    await Promise.resolve()
    expect(screen.queryByText('Late detail')).not.toBeInTheDocument()
    expect(screen.getByText('Select an item')).toBeVisible()
  })

  it('deletes the selected items at once, without asking, and ignores stale refreshes', async () => {
    let resolveStale
    api.getClipboardItems
      .mockResolvedValueOnce({ items: firstItems })
      .mockReturnValueOnce(new Promise((resolve) => { resolveStale = resolve }))
      .mockResolvedValue({ items: [] })
    api.deleteItems.mockResolvedValue({ deleted: 2, item_ids: ['item-1', 'item-2'] })
    const confirmSpy = vi.spyOn(globalThis, 'confirm').mockReturnValue(false)
    render(<ClipboardView status={status} />)
    await screen.findByText('Second item')
    expect(screen.queryByRole('button', { name: 'Clear' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete selected' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(resolveStale).toBeTypeOf('function'))
    fireEvent.click(screen.getByRole('button', { name: 'All' }))
    expect(screen.getByTestId('selection-count')).toHaveTextContent('2 of 2 selected')
    fireEvent.click(screen.getByRole('button', { name: 'Delete selected' }))
    await waitFor(() => expect(api.deleteItems).toHaveBeenCalledWith('device:alpha', ['item-1', 'item-2']))
    resolveStale({ items: firstItems })
    await Promise.resolve()
    await waitFor(() => expect(screen.queryByText('Second item')).not.toBeInTheDocument())
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(api.deleteItem).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('All selects pinned entries too and Delete empties the whole list without asking', async () => {
    const mixed = [
      { item_id: 'recent-text', kind: 'text', preview_text: 'Recent note', size: 11, available: true, pinned: false },
      { item_id: 'pinned-a', kind: 'text', preview_text: 'Pinned A', size: 8, available: true, pinned: true },
      { item_id: 'pinned-b', kind: 'text', preview_text: 'Pinned B', size: 8, available: true, pinned: true },
    ]
    let stored = mixed
    api.getClipboardItems.mockImplementation(async () => ({ items: stored }))
    api.deleteItems.mockImplementation(async (_profile, ids) => {
      stored = stored.filter((it) => !ids.includes(it.item_id))
      return { deleted: ids.length, item_ids: ids }
    })
    const confirmSpy = vi.spyOn(globalThis, 'confirm').mockReturnValue(false)
    render(<ClipboardView status={status} />)
    await screen.findByText('Recent note')
    expect(screen.getByTestId('selection-count')).toHaveTextContent('0 of 3 selected')

    fireEvent.click(screen.getByRole('button', { name: 'All' }))
    expect(screen.getByTestId('selection-count')).toHaveTextContent('3 of 3 selected')
    fireEvent.click(screen.getByRole('button', { name: /Pinned 2/ }))
    const pinnedCards = screen.getAllByTestId('clipboard-card')
    expect(pinnedCards).toHaveLength(2)
    for (const card of pinnedCards) expect(card).toHaveAttribute('data-checked', 'true')

    fireEvent.click(screen.getByRole('button', { name: 'Delete selected' }))
    await waitFor(() => expect(api.deleteItems).toHaveBeenCalledWith('device:alpha', ['recent-text', 'pinned-a', 'pinned-b']))
    await waitFor(() => expect(screen.queryAllByTestId('clipboard-card')).toHaveLength(0))
    expect(screen.getByRole('button', { name: /Recent 0/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Pinned 0/ })).toBeInTheDocument()
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(api.deleteItem).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('selects with Ctrl-click without opening the detail and keeps plain click for details', async () => {
    api.getClipboardItem.mockResolvedValue({
      item: { item_id: 'item-2', kind: 'text', preview_text: 'Second item', available: true },
      kind: 'text', text: 'Second item detail',
    })
    render(<ClipboardView status={status} />)
    const cards = await screen.findAllByTestId('clipboard-card')
    fireEvent.click(cards[0], { ctrlKey: true })
    expect(cards[0]).toHaveAttribute('data-checked', 'true')
    expect(api.getClipboardItem).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Invert' }))
    expect(cards[0]).toHaveAttribute('data-checked', 'false')
    expect(cards[1]).toHaveAttribute('data-checked', 'true')
    fireEvent.click(cards[1])
    await waitFor(() => expect(api.getClipboardItem).toHaveBeenCalledWith('device:alpha', 'item-2'))
    expect(api.pasteItem).not.toHaveBeenCalled()
  })
})
