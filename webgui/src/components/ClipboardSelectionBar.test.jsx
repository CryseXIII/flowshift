import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import ClipboardSelectionBar from './ClipboardSelectionBar.jsx'

const makeSelection = (count, selectableCount = 3) => ({
  count,
  selectableCount,
  selectAll: vi.fn(),
  invert: vi.fn(),
  clear: vi.fn(),
})

describe('ClipboardSelectionBar', () => {
  it('renders icon buttons with aria-label and title, the count stays as text', () => {
    render(<ClipboardSelectionBar selection={makeSelection(0)} total={3} onDelete={vi.fn()} />)
    for (const name of ['All', 'Invert', 'None', 'Delete selected']) {
      const button = screen.getByRole('button', { name })
      expect(button.querySelector('svg')).not.toBeNull()
      expect(button.getAttribute('title')).toBeTruthy()
      expect(button.textContent.trim()).toBe('')
    }
    expect(screen.getByTestId('selection-count')).toHaveTextContent('0 of 3 selected')
    expect(screen.getByRole('button', { name: 'Delete selected' })).toBeDisabled()
  })

  it('shows the selected count as a badge next to the delete icon and deletes at once', () => {
    const selection = makeSelection(2)
    const onDelete = vi.fn()
    render(<ClipboardSelectionBar selection={selection} total={3} onDelete={onDelete} />)
    const del = screen.getByRole('button', { name: 'Delete selected' })
    expect(del).toBeEnabled()
    expect(del).toHaveTextContent('2')
    fireEvent.click(del)
    expect(onDelete).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'All' }))
    fireEvent.click(screen.getByRole('button', { name: 'Invert' }))
    fireEvent.click(screen.getByRole('button', { name: 'None' }))
    expect(selection.selectAll).toHaveBeenCalledTimes(1)
    expect(selection.invert).toHaveBeenCalledTimes(1)
    expect(selection.clear).toHaveBeenCalledTimes(1)
  })
})
