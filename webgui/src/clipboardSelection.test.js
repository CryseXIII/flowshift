import { describe, expect, it } from 'vitest'
import { invertIds, pruneIds, rangeIds, selectAllIds, selectionKeyAction, toggleId } from './clipboardSelection.js'

const visible = [{ item_id: 'a' }, { item_id: 'b' }, { item_id: 'c', pinned: true }, { item_id: 'd' }]
const sorted = (set) => [...set].sort()

describe('clipboardSelection', () => {
  it('select all skips pinned items but keeps hand-picked ones', () => {
    expect(sorted(selectAllIds(new Set(), visible))).toEqual(['a', 'b', 'd'])
    expect(sorted(selectAllIds(new Set(['c']), visible))).toEqual(['a', 'b', 'c', 'd'])
  })

  it('invert flips unpinned items only', () => {
    expect(sorted(invertIds(new Set(['a']), visible))).toEqual(['b', 'd'])
    expect(sorted(invertIds(new Set(['a', 'c']), visible))).toEqual(['b', 'c', 'd'])
  })

  it('range, toggle and prune', () => {
    expect(sorted(rangeIds(new Set(), visible, 'd', 'a'))).toEqual(['a', 'b', 'c', 'd'])
    expect(sorted(toggleId(new Set(['a']), 'a'))).toEqual([])
    const keep = new Set(['a'])
    expect(pruneIds(keep, visible)).toBe(keep)
    expect(sorted(pruneIds(new Set(['a', 'gone']), visible))).toEqual(['a'])
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
