// Selection commands shared by the clipboard overlay and the Clipboard tab.
// "Delete" acts at once: no confirmation, no second click.
export default function ClipboardSelectionBar({ selection, total, busy = false, onDelete, className = '' }) {
  const { count, selectableCount } = selection
  return (
    <div className={`clipboard-selection ${className}`} role="toolbar" aria-label="Selection">
      <span className="clipboard-selection-count" data-testid="selection-count" aria-live="polite">
        {count} of {total} selected
      </span>
      <button type="button" disabled={busy || selectableCount === 0} onClick={selection.selectAll}
        title={selectableCount === 0 ? 'Pinned items are selected one by one' : 'Select all (Ctrl+A), pinned items excluded'}>
        All
      </button>
      <button type="button" disabled={busy || selectableCount === 0} onClick={selection.invert}
        title="Invert selection (Ctrl+I), pinned items excluded">
        Invert
      </button>
      <button type="button" disabled={busy || count === 0} onClick={selection.clear}
        title="Deselect all (Ctrl+Shift+A)">
        None
      </button>
      <button type="button" className="clipboard-selection-delete" disabled={busy || count === 0}
        onClick={onDelete} aria-label="Delete selected" title="Delete the selected items now (Del)">
        Delete{count ? ` ${count}` : ''}
      </button>
    </div>
  )
}
