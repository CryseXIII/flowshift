import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import EventLog from './EventLog.jsx'
import * as api from '../api.js'
import { STORAGE_KEY, defaultFilter, filterEntries } from '../eventLogFilter.js'

vi.mock('../api.js', () => ({ getEventLog: vi.fn() }))

const NOW = Date.now() / 1000
const ENTRIES = [
  { id: 1, level: 'info', kind: 'connection', device: 'Buero', text: 'peer Buero connected', ts: NOW - 30 },
  { id: 2, level: 'warning', kind: 'edge', device: 'Viktor-Laptop', text: 'edge switch did not happen', ts: NOW - 120 },
  { id: 3, level: 'error', kind: 'web_api', device: '', text: 'web_api GET /api/status: boom', detail: 'Traceback (most recent call last)', ts: NOW - 7200 },
  { id: 4, level: 'info', kind: 'clipboard', device: 'Buero', text: 'clipboard item received', ts: NOW - 5 },
]

function rows() {
  return screen.queryAllByTestId('event-row').map((row) => row.textContent)
}

describe('EventLog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    api.getEventLog.mockResolvedValue({ entries: ENTRIES, last_id: 4 })
  })

  it('shows level, kind and device of every entry', async () => {
    render(<EventLog />)
    await waitFor(() => expect(rows()).toHaveLength(4))
    expect(rows()[1]).toContain('Warning')
    expect(rows()[1]).toContain('Edge')
    expect(rows()[1]).toContain('Viktor-Laptop')
    expect(api.getEventLog).toHaveBeenCalledWith(0)
  })

  it('combines level, kind, device and text filters', async () => {
    render(<EventLog />)
    await waitFor(() => expect(rows()).toHaveLength(4))
    fireEvent.click(screen.getByLabelText('Warning'))
    fireEvent.click(screen.getByLabelText('Error'))
    expect(rows()).toHaveLength(2)
    fireEvent.change(screen.getByTestId('event-log-device'), { target: { value: 'Buero' } })
    expect(rows()).toHaveLength(2)
    fireEvent.click(screen.getByLabelText('Clipboard'))
    expect(rows()).toHaveLength(1)
    expect(rows()[0]).toContain('peer Buero connected')
    fireEvent.change(screen.getByLabelText('Search events'), { target: { value: 'nothing like this' } })
    expect(screen.getByText('No events match the filter.')).toBeInTheDocument()
    expect(screen.getByTestId('event-log-count')).toHaveTextContent('0 of 4 events')
  })

  it('keeps the filter after a reload and resets it on request', async () => {
    const { unmount } = render(<EventLog />)
    await waitFor(() => expect(rows()).toHaveLength(4))
    fireEvent.click(screen.getByLabelText('Info'))
    fireEvent.change(screen.getByTestId('event-log-range'), { target: { value: '1h' } })
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY)).range).toBe('1h')
    unmount()

    render(<EventLog />)
    await waitFor(() => expect(rows()).toHaveLength(1))
    expect(rows()[0]).toContain('edge switch did not happen')
    expect(screen.getByLabelText('Info')).not.toBeChecked()
    expect(screen.getByTestId('event-log-range')).toHaveValue('1h')

    const reset = screen.getByRole('button', { name: /Reset filter/ })
    fireEvent.click(reset)
    expect(rows()).toHaveLength(4)
    expect(reset).toBeDisabled()
  })

  it('searches the details of an error', async () => {
    render(<EventLog />)
    await waitFor(() => expect(rows()).toHaveLength(4))
    fireEvent.change(screen.getByLabelText('Search events'), { target: { value: 'traceback' } })
    expect(rows()).toHaveLength(1)
    expect(rows()[0]).toContain('web_api GET /api/status: boom')
  })

  it('filters a custom time range', () => {
    const from = new Date((NOW - 200) * 1000)
    const pad = (n) => String(n).padStart(2, '0')
    const local = `${from.getFullYear()}-${pad(from.getMonth() + 1)}-${pad(from.getDate())}T${pad(from.getHours())}:${pad(from.getMinutes())}`
    const ids = filterEntries(ENTRIES, { ...defaultFilter(), range: 'custom', from: local }, NOW).map((e) => e.id)
    expect(ids).toEqual([1, 2, 4])
  })

  it('names every button with text', async () => {
    render(<EventLog />)
    await waitFor(() => expect(rows()).toHaveLength(4))
    for (const button of screen.getAllByRole('button')) {
      expect(button.textContent.trim()).not.toBe('')
    }
  })
})
