// Selection commands shared by the clipboard overlay and the Clipboard tab.
// "All", "Invert" and "None" cover every entry of the clipboard, Recent and
// Pinned alike. "Delete" acts at once: no confirmation, no second click.
// Icons are drawn inline (Font Awesome style) because the overlay window does
// not load the Font Awesome stylesheet the WebGUI uses.
const ICON_PATHS = {
  all: <><rect x="2.5" y="2.5" width="11" height="11" rx="2" /><path d="M5.2 8.2l2 2 3.8-4.2" /></>,
  invert: <path d="M3 5.5h10M10.5 3L13 5.5 10.5 8M13 10.5H3M5.5 8L3 10.5 5.5 13" />,
  none: <rect x="2.5" y="2.5" width="11" height="11" rx="2" />,
  delete: <path d="M2.5 4.5h11M6.3 4.5V2.8h3.4v1.7M4.2 4.5l.7 8.7h6.2l.7-8.7M6.8 7v4M9.2 7v4" />,
}

function SelectionIcon({ name }) {
  return (
    <svg className="clipboard-selection-icon" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"
      focusable="false" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      {ICON_PATHS[name]}
    </svg>
  )
}

export default function ClipboardSelectionBar({ selection, busy = false, onDelete, className = '' }) {
  const { count, total } = selection
  return (
    <div className={`clipboard-selection ${className}`} role="toolbar" aria-label="Selection">
      <span className="clipboard-selection-count" data-testid="selection-count" aria-live="polite">
        {count} of {total} selected
      </span>
      <button type="button" disabled={busy || total === 0} onClick={selection.selectAll}
        aria-label="All" title="Select every entry, Recent and Pinned (Ctrl+A)">
        <SelectionIcon name="all" />
      </button>
      <button type="button" disabled={busy || total === 0} onClick={selection.invert}
        aria-label="Invert" title="Invert selection, Recent and Pinned (Ctrl+I)">
        <SelectionIcon name="invert" />
      </button>
      <button type="button" disabled={busy || count === 0} onClick={selection.clear}
        aria-label="None" title="Deselect all (Ctrl+Shift+A)">
        <SelectionIcon name="none" />
      </button>
      <button type="button" className="clipboard-selection-delete" disabled={busy || count === 0}
        onClick={onDelete} aria-label="Delete selected" title="Delete the selected items now (Del)">
        <SelectionIcon name="delete" />
        {count > 0 && <span className="clipboard-selection-badge" aria-hidden="true">{count}</span>}
      </button>
    </div>
  )
}
