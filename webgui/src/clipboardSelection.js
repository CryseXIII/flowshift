import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

// Multi-selection for clipboard lists (overlay and Clipboard tab).
// Rules (Viktor 23.11.): the selection spans the whole clipboard of the
// profile, Recent and Pinned alike. "All", "Invert" and "None" act on every
// entry without exception; the Recent/Pinned tabs only change what is shown.
// The search field narrows the scope to the matching entries. Entries that
// leave the scope drop out of the selection, so the count in the bar is
// exactly what "Delete" removes.

export function selectAllIds(selected, scope) {
  return new Set(scope.map((item) => item.item_id))
}

export function invertIds(selected, scope) {
  const next = new Set()
  for (const item of scope) {
    if (!selected.has(item.item_id)) next.add(item.item_id)
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

// `scope` is every entry the selection may cover (all tabs, after search),
// `visible` the rows shown in the current tab (for Shift-click ranges).
export function useClipboardSelection(scope, visible = scope) {
  const [selected, setSelected] = useState(() => new Set())
  const anchorRef = useRef(null)

  useEffect(() => {
    setSelected((prev) => pruneIds(prev, scope))
  }, [scope])

  // Checkbox click or Ctrl-click toggles one item, Shift-click adds the range
  // from the last clicked item. The anchor is read before it moves on: React
  // may run the updater only at the next render, when anchorRef already holds
  // the clicked item (then only both ends were checked, 0.7.0).
  const check = useCallback((item, event) => {
    const id = item.item_id
    const anchor = anchorRef.current
    if (event?.shiftKey && anchor) {
      setSelected((prev) => rangeIds(prev, visible, anchor, id))
    } else {
      setSelected((prev) => toggleId(prev, id))
    }
    anchorRef.current = id
  }, [visible])

  const selectAll = useCallback(() => { setSelected((prev) => selectAllIds(prev, scope)) }, [scope])
  const invert = useCallback(() => { setSelected((prev) => invertIds(prev, scope)) }, [scope])
  const clear = useCallback(() => { anchorRef.current = null; setSelected(new Set()) }, [])

  const selectedIds = useMemo(
    () => scope.filter((item) => selected.has(item.item_id)).map((item) => item.item_id),
    [selected, scope],
  )

  return {
    selected,
    selectedIds,
    count: selectedIds.length,
    total: scope.length,
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
