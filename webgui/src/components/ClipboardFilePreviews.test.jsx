import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ClipboardCard from './ClipboardCard.jsx'
import { fetchFilePreviews, hasPending, nextPollDelay, previewLabel, previewUrl } from '../clipboardPreview.js'

vi.mock('../clipboardPreview.js', async (importOriginal) => ({
  ...(await importOriginal()),
  fetchFilePreviews: vi.fn(),
}))

const FILES = {
  root_names: ['Media'],
  file_names: ['cat.gif', 'trip.mp4', 'gone.png', 'notes.txt'],
  relative_paths: ['Media/cat.gif', 'Media/trip.mp4', 'Media/gone.png', 'Media/notes.txt'],
  total_files: 6,
  total_size: 900,
  truncated: true,
}

function batch(overrides = {}) {
  return { item_id: 'batch-1', kind: 'file_batch', display_name: '6 Dateien', available: true, pinned: false, file_card: FILES, ...overrides }
}

function single(name = 'photo.jpg') {
  return {
    item_id: 'file-1', kind: 'file', display_name: name, available: true, pinned: false,
    file_card: { root_names: [name], file_names: [name], relative_paths: [name], total_files: 1, total_size: 5, truncated: false },
  }
}

const READY = {
  ok: true,
  total_files: 6,
  animation: 'hover',
  video_hover: true,
  previews: [
    { index: 0, ext: 'gif', state: 'ready', kind: 'animated', key: 'k0', still: true, anim: true, video: false },
    { index: 1, ext: 'mp4', state: 'ready', kind: 'video', key: 'k1', still: true, anim: false, video: true },
    { index: 2, ext: 'png', state: 'missing', kind: 'none', key: null, still: false, anim: false, video: false },
    { index: 3, ext: 'txt', state: 'ready', kind: 'icon', key: 'k3', still: true, anim: false, video: false },
  ],
}

const tileImg = (tile) => tile.querySelector('img')?.getAttribute('src') || ''

describe('clipboardPreview helpers', () => {
  it('labels a tile in the chosen path display', () => {
    expect(previewLabel(FILES, 'file_names', 1)).toBe('trip.mp4')
    expect(previewLabel(FILES, 'relative_paths', 1)).toBe('Media/trip.mp4')
    expect(previewLabel(FILES, 'root_names', 1)).toBe('Media › trip.mp4')
    expect(previewLabel({ file_names: ['a.txt'], relative_paths: ['a.txt'] }, 'root_names', 0)).toBe('a.txt')
  })

  it('builds cache-keyed URLs and backs off while pending', () => {
    expect(previewUrl('device:peer a', 'id-1', 2, 'anim', 'abc'))
      .toBe('/api/clipboard/file-preview/id-1/2/anim?profile=device%3Apeer+a&v=abc')
    expect([0, 1, 2, 3, 6].map(nextPollDelay)).toEqual([300, 600, 1200, 2400, 2500])
    expect(hasPending({ previews: [{ state: 'ready' }, { state: 'pending' }] })).toBe(true)
    expect(hasPending({ previews: [{ state: 'missing' }] })).toBe(false)
  })
})

describe('file previews on clipboard cards', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows the first files as a row with a count for the rest and asks again while pending', async () => {
    fetchFilePreviews
      .mockResolvedValueOnce({ ...READY, previews: READY.previews.map((row) => ({ ...row, state: 'pending', still: false, key: null })) })
      .mockResolvedValue(READY)
    render(<ClipboardCard item={batch()} previewProfile="local" />)

    await waitFor(() => expect(fetchFilePreviews).toHaveBeenCalledTimes(2))
    expect(fetchFilePreviews).toHaveBeenCalledWith('local', 'batch-1', 4)
    const tiles = await screen.findAllByTestId('clip-preview-tile')
    await waitFor(() => expect(tiles[0].getAttribute('data-kind')).toBe('animated'))
    const current = screen.getAllByTestId('clip-preview-tile')
    expect(current).toHaveLength(4)
    expect(screen.getByTestId('clip-preview-more')).toHaveTextContent('+2')
    // Hover mode: still image until the mouse is over the card.
    expect(tileImg(current[0])).toContain('/batch-1/0/still?profile=local&v=k0')
    expect(current[0]).toHaveAttribute('title', 'Media/cat.gif')
    expect(current[1].querySelector('video')).toBeNull()
    // Missing file: neutral symbol with its extension, no error.
    expect(current[2]).toHaveAttribute('data-kind', 'neutral')
    expect(current[2]).toHaveTextContent('png')
    expect(current[3]).toHaveAttribute('data-kind', 'icon')

    fireEvent.mouseEnter(screen.getByRole('article'))
    expect(tileImg(screen.getAllByTestId('clip-preview-tile')[0])).toContain('/0/anim')
    const video = screen.getAllByTestId('clip-preview-tile')[1].querySelector('video')
    expect(video.getAttribute('src')).toContain('/batch-1/1/video')
    expect(video.muted).toBe(true)

    fireEvent.mouseLeave(screen.getByRole('article'))
    expect(tileImg(screen.getAllByTestId('clip-preview-tile')[0])).toContain('/0/still')
    expect(screen.getAllByTestId('clip-preview-tile')[1].querySelector('video')).toBeNull()
  })

  it('plays animations all the time when set to always and skips video clips when off', async () => {
    fetchFilePreviews.mockResolvedValue({ ...READY, animation: 'always', video_hover: false })
    render(<ClipboardCard item={batch()} previewProfile="local" />)
    await waitFor(() => expect(tileImg(screen.getAllByTestId('clip-preview-tile')[0])).toContain('/0/anim'))
    fireEvent.mouseEnter(screen.getByRole('article'))
    expect(screen.getAllByTestId('clip-preview-tile')[1].querySelector('video')).toBeNull()
  })

  it('puts a single file preview into the icon slot and falls back to the neutral symbol', async () => {
    fetchFilePreviews.mockResolvedValue({
      total_files: 1, animation: 'always', video_hover: true,
      previews: [{ index: 0, ext: 'jpg', state: 'ready', kind: 'image', key: 'k9', still: true, anim: false, video: false }],
    })
    render(<ClipboardCard item={single()} previewProfile="local" />)
    await waitFor(() => expect(fetchFilePreviews).toHaveBeenCalledWith('local', 'file-1', 1))
    const tile = await screen.findByTestId('clip-preview-tile')
    await waitFor(() => expect(tile).toHaveAttribute('data-kind', 'image'))
    expect(screen.queryByTestId('clip-preview-row')).not.toBeInTheDocument()
    fireEvent.error(tile.querySelector('img'))
    expect(screen.getByTestId('clip-preview-tile')).toHaveAttribute('data-kind', 'neutral')
    expect(screen.getByTestId('clip-preview-tile')).toHaveTextContent('jpg')
  })

  it('shows neutral tiles when the descriptor request fails', async () => {
    fetchFilePreviews.mockRejectedValue(new Error('gone'))
    render(<ClipboardCard item={batch()} previewProfile="local" />)
    await waitFor(() => expect(screen.getAllByTestId('clip-preview-tile')[0]).toHaveAttribute('data-state', 'missing'))
    expect(screen.getAllByTestId('clip-preview-tile')).toHaveLength(4)
  })

  it('does not ask for previews without a profile or for non-file items', () => {
    render(<ClipboardCard item={batch()} />)
    render(<ClipboardCard item={{ item_id: 't', kind: 'text', preview_text: 'x', available: true }} previewProfile="local" />)
    expect(fetchFilePreviews).not.toHaveBeenCalled()
    expect(screen.queryByTestId('clip-preview-tile')).not.toBeInTheDocument()
  })
})
