import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import CommandWheel from './CommandWheel.jsx'
import { MAX_SLOTS, labelPosition, nextPage, normalizeWheelData, sectorAngles, sectorPath } from './wheelGeometry.js'

const ACTIONS = {
  copy: { label: 'Copy' }, paste: { label: 'Paste' }, cut: { label: 'Cut' }, delete: { label: 'Delete' },
  select_all: { label: 'Select all' }, undo: { label: 'Undo' }, redo: { label: 'Redo' },
  open_clipboard: { label: 'Clipboard' }, clipboard_sync: { label: 'Sync clipboard' },
}
const PAGES = [
  ['copy', 'paste', 'cut', 'delete', 'select_all', 'undo', 'redo', 'open_clipboard'],
  ['clipboard_sync'],
]

function sectors() {
  return screen.queryAllByRole('menuitem')
}

describe('wheelGeometry', () => {
  it('splits the circle into equal sectors starting at the top', () => {
    const first = sectorAngles(0, 8)
    expect(first.step).toBe(45)
    expect(first.mid).toBe(-90)
    expect(sectorAngles(7, 8).end).toBeCloseTo(first.start + 360, 5)
    const positions = Array.from({ length: 8 }, (_, i) => labelPosition(i, 8))
    const distinct = new Set(positions.map((p) => `${p.x},${p.y}`))
    expect(distinct.size).toBe(8)
    expect(positions[0].y).toBeLessThan(positions[4].y)
  })

  it('renders every sector as a closed annular path and a ring for one slot', () => {
    for (let n = 2; n <= MAX_SLOTS; n += 1) {
      for (let i = 0; i < n; i += 1) {
        const path = sectorPath(i, n)
        expect(path.startsWith('M ')).toBe(true)
        expect(path.endsWith('Z')).toBe(true)
        expect((path.match(/A /g) || []).length).toBe(2)
      }
    }
    expect((sectorPath(0, 1).match(/A /g) || []).length).toBe(4)
  })

  it('pages cyclically in both directions', () => {
    expect(nextPage(0, 3, 1)).toBe(1)
    expect(nextPage(2, 3, 1)).toBe(0)
    expect(nextPage(0, 3, -1)).toBe(2)
    expect(nextPage(1, 3, 0)).toBe(1)
    expect(nextPage(0, 1, 1)).toBe(0)
  })

  it('normalizes host payloads and drops unknown actions or overlong pages', () => {
    const data = normalizeWheelData({
      pages: [['copy', 'bogus', 'paste'], 'not-a-page', [], Array(10).fill('cut')],
      actions: ACTIONS,
    })
    expect(data.pages).toEqual([['copy', 'paste'], Array(8).fill('cut')])
    expect(normalizeWheelData(null).pages).toEqual([])
    expect(normalizeWheelData({ pages: 'x' }).pages).toEqual([])
  })
})

describe('CommandWheel', () => {
  it('renders up to 8 sectors, page dots, and cycles pages with the mouse wheel', () => {
    render(<CommandWheel pages={PAGES} actions={ACTIONS} onExecute={vi.fn()} />)
    const wheel = screen.getByTestId('command-wheel')
    expect(sectors()).toHaveLength(8)
    expect(sectors().map((s) => s.getAttribute('data-action'))).toEqual(PAGES[0])
    const dots = screen.getAllByTestId('wheel-dot')
    expect(dots).toHaveLength(2)
    expect(dots[0]).toHaveClass('wheel-dot--active')
    expect(dots[1]).not.toHaveClass('wheel-dot--active')

    fireEvent.wheel(wheel, { deltaY: 120 })
    expect(wheel).toHaveAttribute('data-page', '1')
    expect(sectors()).toHaveLength(1)
    expect(sectors()[0]).toHaveAttribute('data-action', 'clipboard_sync')
    expect(screen.getAllByTestId('wheel-dot')[1]).toHaveClass('wheel-dot--active')

    fireEvent.wheel(wheel, { deltaY: 120 })
    expect(wheel).toHaveAttribute('data-page', '0')
    expect(sectors()).toHaveLength(8)

    fireEvent.wheel(wheel, { deltaY: -120 })
    expect(wheel).toHaveAttribute('data-page', '1')
  })

  it('spotlights the hovered sector and dims the others', () => {
    render(<CommandWheel pages={PAGES} actions={ACTIONS} onExecute={vi.fn()} />)
    const wheel = screen.getByTestId('command-wheel')
    expect(wheel).not.toHaveClass('wheel--spotlight')
    const items = sectors()
    fireEvent.mouseEnter(items[2])
    expect(wheel).toHaveClass('wheel--spotlight')
    expect(items[2]).toHaveClass('sector--hot')
    expect(items[2]).not.toHaveClass('sector--dim')
    items.filter((_, i) => i !== 2).forEach((item) => {
      expect(item).toHaveClass('sector--dim')
      expect(item).not.toHaveClass('sector--hot')
    })
    fireEvent.mouseEnter(items[5])
    expect(items[5]).toHaveClass('sector--hot')
    expect(items[2]).toHaveClass('sector--dim')
    fireEvent.mouseLeave(wheel)
    expect(wheel).not.toHaveClass('wheel--spotlight')
    expect(items[5]).not.toHaveClass('sector--hot')
  })

  it('executes the clicked action once and surfaces a runtime refusal', async () => {
    let resolve
    const onExecute = vi.fn(() => new Promise((r) => { resolve = r }))
    render(<CommandWheel pages={PAGES} actions={ACTIONS} onExecute={onExecute} />)
    const items = sectors()
    fireEvent.click(items[1])
    fireEvent.click(items[3])
    expect(onExecute).toHaveBeenCalledTimes(1)
    expect(onExecute).toHaveBeenCalledWith('paste')
    resolve({ ok: false, reason: 'overlay_busy' })
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('overlay_busy'))
    fireEvent.click(items[3])
    await waitFor(() => expect(onExecute).toHaveBeenCalledTimes(2))
    expect(onExecute).toHaveBeenLastCalledWith('delete')
  })

  it('shows an empty state without pages', () => {
    render(<CommandWheel pages={[]} actions={{}} onExecute={vi.fn()} />)
    expect(screen.getByTestId('command-wheel')).toHaveTextContent('No actions configured')
    expect(sectors()).toHaveLength(0)
  })
})
