import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ClipboardView from './ClipboardView.jsx'
import * as api from '../api.js'

vi.mock('../api.js', () => ({
  getClipboardItems: vi.fn(),
  getClipboardItem: vi.fn(),
  getClipboardProgress: vi.fn(),
  getClipboardStatus: vi.fn(),
  getThumbnail: vi.fn(),
  pasteItem: vi.fn(),
  deleteItem: vi.fn(),
  pinItem: vi.fn(),
  requestItem: vi.fn(),
  syncClipboard: vi.fn(),
  clearClipboard: vi.fn(),
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
    api.getClipboardItems.mockResolvedValue({ items: firstItems })
    api.getClipboardProgress.mockResolvedValue({})
    api.getClipboardStatus.mockResolvedValue({ diagnostics: { stream_v2: [] } })
    api.subscribeSSE.mockReturnValue(vi.fn())
  })

  it('keeps the same list node and scroll offset across an item refresh', async () => {
    api.getClipboardItems
      .mockResolvedValueOnce({ items: firstItems })
      .mockResolvedValueOnce({ items: [...firstItems, { item_id: 'item-3', kind: 'text', preview_text: 'New item', available: true }] })
    render(<ClipboardView status={status} />)

    await screen.findByText('Project files')
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
          eta_seconds: 2, file_index: 1, file_count: 3,
        }],
      },
    })
    render(<ClipboardView status={status} />)

    expect(await screen.findByText(/50% · 2\.0 KB\/4\.0 KB · 1\.0 KB\/s · ETA 2s · file 2\/3/)).toBeVisible()
    expect(api.getClipboardProgress).toHaveBeenCalled()
    expect(api.getClipboardStatus).toHaveBeenCalledWith('device:alpha')
  })

  it('keeps the list mounted when a refresh returns no items', async () => {
    api.getClipboardItems
      .mockResolvedValueOnce({ items: firstItems })
      .mockResolvedValueOnce({ items: [] })
    render(<ClipboardView status={status} />)
    await screen.findByText('Project files')
    const list = screen.getByTestId('clipboard-item-list')

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText('No clipboard items yet. Copy something on the remote machine.')).toBeVisible()
    expect(screen.getByTestId('clipboard-item-list')).toBe(list)
    await waitFor(() => expect(api.getClipboardItems).toHaveBeenCalledTimes(2))
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
})
