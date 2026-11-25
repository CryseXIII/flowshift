import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import {
  invertIds, pruneIds, rangeIds, selectAllIds, selectionKeyAction, toggleId, useClipboardSelection,
} from './clipboardSelection.js'

const visible = [{ item_id: 'a' }, { item_id: 'b' }, { item_id: 'c', pinned: true }, { item_id: 'd' }]
const sorted = (set) => [...set].sort()

describe('clipboardSelection', () => {
  it('select all takes every entry, pinned ones included', () => {
    expect(sorted(selectAllIds(new Set(), visible))).toEqual(['a', 'b', 'c', 'd'])
    expect(sorted(selectAllIds(new Set(['c', 'gone']), visible))).toEqual(['a', 'b', 'c', 'd'])
  })

  it('invert flips every entry, pinned ones included', () => {
    expect(sorted(invertIds(new Set(['a']), visible))).toEqual(['b', 'c', 'd'])
    expect(sorted(invertIds(new Set(['a', 'c']), visible))).toEqual(['b', 'd'])
    expect(sorted(invertIds(new Set(), visible))).toEqual(['a', 'b', 'c', 'd'])
  })

  it('range, toggle and prune', () => {
    expect(sorted(rangeIds(new Set(), visible, 'd', 'a'))).toEqual(['a', 'b', 'c', 'd'])
    expect(sorted(toggleId(new Set(['a']), 'a'))).toEqual([])
    const keep = new Set(['a'])
    expect(pruneIds(keep, visible)).toBe(keep)
    expect(sorted(pruneIds(new Set(['a', 'gone']), visible))).toEqual(['a'])
  })

  describe('Shift-click range (Viktor 25.11.: every entry in between, not only both ends)', () => {
    const items = ['a', 'b', 'c', 'd', 'e'].map((id) => ({ item_id: id }))
    const byId = (id) => items.find((item) => item.item_id === id)
    const shift = { shiftKey: true }

    it('checks every entry between the last click and the Shift-click, in both directions', () => {
      const { result } = renderHook(() => useClipboardSelection(items))
      act(() => result.current.check(byId('b'), {}))
      act(() => result.current.check(byId('d'), shift))
      expect(result.current.selectedIds).toEqual(['b', 'c', 'd'])

      act(() => result.current.clear())
      act(() => result.current.check(byId('e'), {}))
      act(() => result.current.check(byId('a'), shift))
      expect(result.current.selectedIds).toEqual(['a', 'b', 'c', 'd', 'e'])
    })

    it('keeps the anchor of the click before, even when React runs both updates in one render', () => {
      // Both clicks land in one batch, so the range updater runs only after
      // check() has moved the anchor: the 0.7.0 code then checked only a and d.
      const { result } = renderHook(() => useClipboardSelection(items))
      act(() => {
        result.current.check(byId('a'), {})
        result.current.check(byId('d'), shift)
      })
      expect(result.current.selectedIds).toEqual(['a', 'b', 'c', 'd'])
    })

    it('ranges over the shown tab only and adds to an existing selection', () => {
      const shown = items.filter((item) => item.item_id !== 'c')
      const { result } = renderHook(() => useClipboardSelection(items, shown))
      act(() => result.current.check(byId('e'), {}))
      act(() => result.current.check(byId('a'), {}))
      act(() => result.current.check(byId('d'), shift))
      expect(result.current.selectedIds).toEqual(['a', 'b', 'd', 'e'])
    })
  })

  it('maps keys outside text fields', () => {
    const body = document.body
    expect(selectionKeyAction({ key: 'a', ctrlKey: true, target: body })).toBe('all')
    expect(selectionKeyAction({ key: 'A', ctrlKey: true, shiftKey: true, target: body })).toBe('none')
    expect(selectionKeyAction({ key: 'i', ctrlKey: true, target: body })).toBe('invert')
    expect(selectionKeyAction({ key: 'Delete', target: body })).toBe('delete')
    const search = document.createElement('input')
    search.type = 'search'
    expect(selectionKeyAction({ key: 'a', ctrlKey: true, target: search })).toBeNull()
    expect(selectionKeyAction({ key: 'Delete', target: search })).toBeNull()
  })
})
