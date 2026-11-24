import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import DisplayConfig from './DisplayConfig.jsx'

vi.mock('../api.js', () => ({
  getDisplayLayout: vi.fn(() => Promise.resolve({ layout: {}, peers: [], warnings: [] })),
  saveDisplayLayout: vi.fn(),
}))

const statusWith = (enabled) => ({
  device_name: 'Laptop',
  peers: [],
  display_layout: { enabled, edges: { east: { peer_identity: 'gone', target_entry_edge: 'west' } } },
  edge_switching: { enabled, warnings: ['unknown peer identity for east: gone'] },
})

describe('DisplayConfig layout warnings', () => {
  it('is a neutral note while edge switching is off', () => {
    render(<DisplayConfig status={statusWith(false)} onRefresh={() => {}} />)
    const note = screen.getByTestId('display-layout-warnings')
    expect(note).toHaveTextContent('unknown peer identity for east')
    expect(note).toHaveClass('msg-box', 'info')
    expect(note).not.toHaveClass('error')
  })

  it('stays a red warning while edge switching is on', () => {
    render(<DisplayConfig status={statusWith(true)} onRefresh={() => {}} />)
    expect(screen.getByTestId('display-layout-warnings')).toHaveClass('msg-box', 'error')
  })

  it('names the edge switching toggle', () => {
    render(<DisplayConfig status={statusWith(true)} onRefresh={() => {}} />)
    expect(screen.getByLabelText('Edge switching enabled')).toBeChecked()
  })
})
