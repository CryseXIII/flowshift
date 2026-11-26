import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api.js'
import EventLog from './EventLog.jsx'
import { explainLogLine } from '../logExplain.js'

vi.mock('../api.js', () => ({
  getLogs: vi.fn(),
  openLogFolder: vi.fn(),
  subscribeSSE: vi.fn(),
}))

// Viktor 26.11. 10:47: an edge switch was refused, only flowshift.log said so.
const REFUSED = { seq: 2, time: 1795000000, level: 'WARN', text: 'edge_enter rejected: invalid_target' }

describe('EventLog', () => {
  let onEvent
  beforeEach(() => {
    vi.clearAllMocks()
    onEvent = null
    api.subscribeSSE.mockImplementation((cb) => { onEvent = cb; return vi.fn() })
    api.getLogs.mockResolvedValue({
      ok: true,
      capacity: 500,
      log_folder: 'C:\\ProgramData\\FlowShift\\logs',
      log_file: 'flowshift.log',
      entries: [
        { seq: 1, time: 1794999990, level: 'INFO', text: 'edge-watch trigger north -> Laptop-Bettina' },
        REFUSED,
      ],
    })
  })

  it('shows a WARN from before the page was opened, coloured and in plain words', async () => {
    render(<EventLog />)
    const rows = await screen.findAllByTestId('log-row')
    const warn = rows.find((r) => r.dataset.level === 'WARN')
    expect(warn).toBeDefined()
    expect(warn).toHaveClass('event-row--warn')
    expect(warn).toHaveTextContent('WARN')
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
      onEvent({ type: 'log', seq: 3, time: 1795000005, level: 'ERROR', text: 'web_api POST /api/settings: boom' })
      onEvent({ type: 'status_update' })
    })
    expect(screen.getAllByTestId('log-row')).toHaveLength(3)
    expect(screen.queryByText('status_update')).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Show level'), { target: { value: 'warn' } })
    expect(screen.getAllByTestId('log-row').map((r) => r.dataset.level)).toEqual(['WARN', 'ERROR'])

    fireEvent.change(screen.getByLabelText('Show level'), { target: { value: 'error' } })
    const [error] = screen.getAllByTestId('log-row')
    expect(screen.getAllByTestId('log-row')).toHaveLength(1)
    expect(error).toHaveClass('event-row--error')
    expect(error).toHaveTextContent('A request from this page failed inside FlowShift')

    fireEvent.change(screen.getByLabelText('Show level'), { target: { value: 'all' } })
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
})
