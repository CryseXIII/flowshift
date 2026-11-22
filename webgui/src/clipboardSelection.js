import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

// Multi-selection for clipboard lists (overlay and Clipboard tab).
// Rules (Viktor 22.11.): "Select all" and "Invert" never pick pinned items;
// pinned items are only selected one by one (checkbox, Ctrl-click,
// Shift-click range). Only visible items can stay selected, so what is
// checked on screen is exactly what "Delete" removes.

// Hand-picked pinned items stay selected; every unpinned item is added.
export function selectAllIds(selected, visible) {
  const next = new Set()
  for (const item of visible) {
    if (!item.pinned || selected.has(item.item_id)) next.add(item.item_id)
  }
  return next
}

export function invertIds(selected, visible) {
  const next = new Set()
  for (const item of visible) {
    const isSelected = selected.has(item.item_id)
    if (item.pinned ? isSelected : !isSelected) next.add(item.item_id)
  }
  return next
}

export function rangeIds(selected, visible, anchorId, targetId) {
  const ids = visible.map((item) => item.item_id)
  const from = ids.indexOf(anchorId)
  const to = ids.indexOf(targetId)
  const next = new Set(selected)
  if (from < 0 || to < 0) {
    next.add(targetId)
    return next
  }
  const [start, end] = from <= to ? [from, to] : [to, from]
  for (let index = start; index <= end; index += 1) next.add(ids[index])
  return next
}

export function toggleId(selected, id) {
  const next = new Set(selected)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  return next
}

export function pruneIds(selected, visible) {
  const ids = new Set(visible.map((item) => item.item_id))
  let changed = false
  const next = new Set()
  for (const id of selected) {
    if (ids.has(id)) next.add(id)
    else changed = true
  }
  return changed ? next : selected
}

export function isTextInput(target) {
  if (!target || typeof target.closest !== 'function') return false
  if (target.isContentEditable) return true
  const tag = target.tagName
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true
  if (tag !== 'INPUT') return false
  return !['checkbox', 'radio', 'button', 'submit'].includes(String(target.type || '').toLowerCase())
}

export function useClipboardSelection(visible) {
  const [selected, setSelected] = useState(() => new Set())
  const anchorRef = useRef(null)

  useEffect(() => {
    setSelected((prev) => pruneIds(prev, visible))
  }, [visible])

  // Checkbox click or Ctrl-click toggles one item, Shift-click adds the range
  // from the last clicked item.
  const check = useCallback((item, event) => {
    const id = item.item_id
    if (event?.shiftKey && anchorRef.current) {
      setSelected((prev) => rangeIds(prev, visible, anchorRef.current, id))
    } else {
      setSelected((prev) => toggleId(prev, id))
    }
    anchorRef.current = id
  }, [visible])

  const selectAll = useCallback(() => { setSelected((prev) => selectAllIds(prev, visible)) }, [visible])
  const invert = useCallback(() => { setSelected((prev) => invertIds(prev, visible)) }, [visible])
  const clear = useCallback(() => { anchorRef.current = null; setSelected(new Set()) }, [])

  const selectedIds = useMemo(
    () => visible.filter((item) => selected.has(item.item_id)).map((item) => item.item_id),
    [selected, visible],
  )
  const selectableCount = useMemo(() => visible.filter((item) => !item.pinned).length, [visible])

  return {
    selected,
    selectedIds,
    count: selectedIds.length,
    selectableCount,
    isSelected: (id) => selected.has(id),
    check,
    selectAll,
    invert,
    clear,
  }
}

// Keyboard shortcuts while the list is shown and focus is not in a text field:
// Ctrl+A select all, Ctrl+I invert, Ctrl+Shift+A deselect all, Delete removes
// the selection. Space on a focused checkbox toggles it (browser default).
export function selectionKeyAction(event) {
  if (isTextInput(event.target)) return null
  const key = String(event.key || '').toLowerCase()
  const ctrl = event.ctrlKey || event.metaKey
  if (ctrl && key === 'a') return event.shiftKey ? 'none' : 'all'
  if (ctrl && key === 'i' && !event.shiftKey) return 'invert'
  if (!ctrl && !event.altKey && key === 'delete') return 'delete'
  return null
}
