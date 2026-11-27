import { useEffect, useRef, useState } from 'react'

// Two separate clear buttons shared by the clipboard overlay and the Clipboard
// tab, one under each tab: "Clear clipboard" empties only the entries that are
// not pinned (Recent), "Clear pinned" only the pinned ones. No button removes
// both; All + Delete selected stays the way to do that. Each asks first, with
// the number of entries and the name of the area, and only then calls
// onClear('unpinned' | 'pinned') for the selected profile.
export const CLEAR_AREAS = {
  unpinned: { name: 'Clipboard', label: 'Clear clipboard', aria: 'Clear clipboard (not pinned)', countKey: 'recent' },
  pinned: { name: 'Pinned', label: 'Clear pinned', aria: 'Clear pinned', countKey: 'pinned' },
}

export function clearQuestion(scope, count) {
  const area = CLEAR_AREAS[scope]
  const entries = `${count} item${count === 1 ? '' : 's'}`
  return scope === 'pinned'
    ? `Delete all ${entries} in ${area.name}? Clipboard (not pinned) stays.`
    : `Delete all ${entries} in ${area.name} (not pinned)? Pinned stays.`
}

export default function ClipboardClearBar({ counts, busy = false, onClear, className = '' }) {
  const [asking, setAsking] = useState(null)
  const cancelRef = useRef(null)

  useEffect(() => {
    if (asking) cancelRef.current?.focus()
  }, [asking])

  // An area that became empty meanwhile (poll, other window) has nothing to ask about.
  useEffect(() => {
    if (asking && !(counts[CLEAR_AREAS[asking.scope].countKey] > 0)) setAsking(null)
  }, [asking, counts])

  const ask = (scope) => {
    const count = counts[CLEAR_AREAS[scope].countKey] || 0
    if (busy || count === 0) return
    setAsking({ scope, count })
  }

  const confirm = () => {
    const scope = asking?.scope
    setAsking(null)
    if (scope) onClear(scope)
  }

  const onDialogKeyDown = (event) => {
    if (event.key !== 'Escape') return
    // Escape only closes the question, not the overlay window around it.
    event.preventDefault()
    event.stopPropagation()
    setAsking(null)
  }

  return (
    <div className={`clipboard-clear ${className}`}>
      <div className="clipboard-clear-buttons">
        {['unpinned', 'pinned'].map((scope) => {
          const area = CLEAR_AREAS[scope]
          const count = counts[area.countKey] || 0
          return (
            <button key={scope} type="button" className="clipboard-clear-button"
              data-testid={`clear-${scope}`}
              disabled={busy || count === 0 || asking !== null}
              onClick={() => ask(scope)} aria-label={area.aria}
              title={count === 0 ? `${area.name} is empty` : `Delete all ${count} ${area.name} items (asks first)`}>
              {area.label}
            </button>
          )
        })}
      </div>
      {asking && (
        <div className="clipboard-clear-dialog" role="alertdialog" aria-modal="false"
          aria-labelledby="clipboard-clear-question" onKeyDown={onDialogKeyDown}>
          <p id="clipboard-clear-question">{clearQuestion(asking.scope, asking.count)}</p>
          <div className="clipboard-clear-dialog-actions">
            <button type="button" className="clipboard-clear-confirm" onClick={confirm}
              aria-label={`Delete ${asking.count} ${CLEAR_AREAS[asking.scope].name} item${asking.count === 1 ? '' : 's'}`}>
              Delete {asking.count}
            </button>
            <button type="button" ref={cancelRef} onClick={() => setAsking(null)}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  )
}
