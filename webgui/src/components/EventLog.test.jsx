import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api.js'
import EventLog from './EventLog.jsx'
import { explainLogLine } from '../logExplain.js'
import { STORAGE_KEY, defaultFilter, filterEntries, levelId } from '../eventLogFilter.js'

vi.mock('../api.js', () => ({
  getLogs: vi.fn(),
  openLogFolder: vi.fn(),
  subscribeSSE: vi.fn(),
}))

// Viktor 26.11. 10:47: an edge switch was refused, only flowshift.log said so.
const REFUSED = { seq: 2, time: 1795000000, level: 'WARN', kind: 'edge', device: '', text: 'edge_enter rejected: invalid_target' }

const NOW = Date.now() / 1000
// Viktor 26.11.: "der event log sollte auch einen gescheiten filter haben".
const ENTRIES = [
  { seq: 1, level: 'INFO', kind: 'connection', device: 'Buero', text: 'peer Buero connected', time: NOW - 30 },
  { seq: 2, level: 'WARN', kind: 'edge', device: 'Viktor-Laptop', text: 'edge switch did not happen', time: NOW - 120 },
  { seq: 3, level: 'ERROR', kind: 'web_api', device: '', text: 'web_api GET /api/status: boom\nTraceback (most recent call last)', time: NOW - 7200 },
  { seq: 4, level: 'INFO', kind: 'clipboard', device: 'Buero', text: 'clipboard item received', time: NOW - 5 },
]

function rows() {
  return screen.queryAllByTestId('log-row').map((row) => row.textContent)
}

describe('EventLog', () => {
  let onEvent
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    onEvent = null
    api.subscribeSSE.mockImplementation((cb) => { onEvent = cb; return vi.fn() })
    api.getLogs.mockResolvedValue({
      ok: true,
      capacity: 500,
      log_folder: 'C:\\ProgramData\\FlowShift\\logs',
      log_file: 'flowshift.log',
      entries: [
        { seq: 1, time: 1794999990, level: 'INFO', kind: 'edge', device: 'Laptop-Bettina', text: 'edge-watch trigger north -> Laptop-Bettina' },
        REFUSED,
      ],
    })
  })

  it('shows a WARN from before the page was opened, coloured and in plain words', async () => {
    render(<EventLog />)
    const all = await screen.findAllByTestId('log-row')
    const warn = all.find((r) => r.dataset.level === 'WARN')
    expect(warn).toBeDefined()
    expect(warn).toHaveClass('event-row--warn')
    expect(warn).toHaveTextContent('WARN')
    expect(warn).toHaveTextContent('Edge')
    expect(warn).toHaveTextContent('The other PC refused the edge switch because it is not paired by device ID')
    expect(warn).toHaveTextContent('edge_enter rejected: invalid_target')
    expect(screen.getByText(/2 entries · 1 warning · 0 errors/)).toBeInTheDocument()
    expect(api.getLogs).toHaveBeenCalledWith(0)
  })

  it('adds live log events once and filters by level', async () => {
    render(<EventLog />)
    await screen.findAllByTestId('log-row')
    act(() => {
      onEvent({ type: 'log', ...REFUSED })                        // already loaded
      onEvent({ type: 'log', seq: 3, time: 1795000005, level: 'ERROR', kind: 'web_api', device: '', text: 'web_api POST /api/settings: boom' })
      onEvent({ type: 'status_update' })
    })
    expect(screen.getAllByTestId('log-row')).toHaveLength(3)
    expect(screen.queryByText('status_update')).not.toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('Info'))
    expect(screen.getAllByTestId('log-row').map((r) => r.dataset.level)).toEqual(['WARN', 'ERROR'])

    fireEvent.click(screen.getByLabelText('Warning'))
    const [error] = screen.getAllByTestId('log-row')
    expect(screen.getAllByTestId('log-row')).toHaveLength(1)
    expect(error).toHaveClass('event-row--error')
    expect(error).toHaveTextContent('A request from this page failed inside FlowShift')

    fireEvent.click(screen.getByLabelText('Also internal events'))
    expect(screen.getByText('status_update')).toBeInTheDocument()
  })

  it('shows the log file path and opens the folder', async () => {
    api.openLogFolder.mockResolvedValue({ ok: true, opened: true, log_folder: 'C:\\ProgramData\\FlowShift\\logs' })
    render(<EventLog />)
    await waitFor(() => expect(screen.getByTestId('log-folder')).toHaveTextContent('C:\\ProgramData\\FlowShift\\logs\\flowshift.log'))
    fireEvent.click(screen.getByRole('button', { name: /Open log folder/ }))
    expect(await screen.findByRole('status')).toHaveTextContent('The log folder was opened in Explorer.')
    expect(api.openLogFolder).toHaveBeenCalledTimes(1)
  })

  it('explains the common technical lines', () => {
    expect(explainLogLine('edge_enter rejected: busy')).toMatch(/already in another switch/)
    expect(explainLogLine('edge trigger ignored: no peer for west -> device:ab12')).toMatch(/west edge/)
    expect(explainLogLine('edge trigger connection failed: connection timeout')).toMatch(/could not connect/)
    expect(explainLogLine('something nobody translated')).toBeNull()
  })

  describe('filters', () => {
    beforeEach(() => {
      api.getLogs.mockResolvedValue({ ok: true, capacity: 500, log_folder: '', log_file: 'flowshift.log', entries: ENTRIES })
    })

    it('shows level, kind and device of every entry', async () => {
      render(<EventLog />)
      await waitFor(() => expect(rows()).toHaveLength(4))
      expect(rows()[1]).toContain('WARN')
      expect(rows()[1]).toContain('Edge')
      expect(rows()[1]).toContain('Viktor-Laptop')
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
      expect(screen.getByText('No entries match the filter.')).toBeInTheDocument()
      expect(screen.getByTestId('event-log-count')).toHaveTextContent('0 of 4 shown')
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

    it('searches the traceback of an error', async () => {
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
      const seqs = filterEntries(ENTRIES, { ...defaultFilter(), range: 'custom', from: local }, NOW).map((e) => e.seq)
      expect(seqs).toEqual([1, 2, 4])
    })

    it('maps the runtime levels to the filter levels', () => {
      expect(['INFO', 'WARN', 'ERROR', 'odd'].map(levelId)).toEqual(['info', 'warning', 'error', 'info'])
    })

    it('names every button with text', async () => {
      render(<EventLog />)
      await waitFor(() => expect(rows()).toHaveLength(4))
      for (const button of screen.getAllByRole('button')) {
        expect(button.textContent.trim()).not.toBe('')
      }
    })
  })
})
