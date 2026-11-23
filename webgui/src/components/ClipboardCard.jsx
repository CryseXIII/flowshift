import { FILE_PATH_DISPLAYS, fmtSize, normalizePathDisplay, progressRows } from '../clipboardFormat.js'

const KIND = {
  text: { icon: 'T', label: 'Text' },
  html: { icon: '</>', label: 'HTML' },
  image: { icon: 'IMG', label: 'Image' },
  gif: { icon: 'GIF', label: 'GIF' },
  file: { icon: 'FILE', label: 'File' },
  file_batch: { icon: 'FILES', label: 'Files' },
  audio: { icon: 'AUDIO', label: 'Audio' },
  binary: { icon: 'BIN', label: 'Binary' },
}

// Puts the names shown on the card on the Windows clipboard. FlowShift
// recognizes its own write, so no new history item appears.
function CopyField({ field, label, values, truncated, disabled, onCopy }) {
  if (!values?.length || !onCopy) return null
  return (
    <button
      type="button"
      className="clipboard-card-copy"
      disabled={disabled}
      aria-label={`Copy ${label.toLowerCase()}`}
      title={truncated ? `Copy the ${values.length} visible ${label.toLowerCase()}` : `Copy ${label.toLowerCase()}`}
      onClick={(event) => { event.stopPropagation(); onCopy(field) }}
    >
      Copy
    </button>
  )
}

// Names of a file card in the chosen display, with the matching labels.
export function fileCardNames(files, pathDisplay) {
  const field = normalizePathDisplay(pathDisplay)
  const option = FILE_PATH_DISPLAYS.find((entry) => entry.value === field)
  const values = Array.isArray(files?.[field]) ? files[field].filter(Boolean) : []
  const truncated = field === 'root_names' ? files?.root_names_truncated : files?.truncated
  return { field, values, truncated: Boolean(truncated), option }
}

export default function ClipboardCard({
  item,
  progress,
  busy = false,
  selected = false,
  checked = false,
  thumbnail = null,
  pathDisplay = 'file_names',
  testId,
  onSelect,
  onCheck,
  onHover,
  onPrimary,
  onPin,
  onDelete,
  onCopy,
}) {
  const kind = item.kind || 'binary'
  const config = KIND[kind] || KIND.binary
  const files = item.file_card
  const isFile = kind === 'file'
  const isBatch = kind === 'file_batch'
  const isText = kind === 'text'
  const isImage = kind === 'image' || kind === 'gif'
  const progressLines = progressRows(progress, kind)
  const primaryLabel = item.available ? 'Set clipboard' : 'Download'
  const names = fileCardNames(files, pathDisplay)
  const title = isBatch
    ? (item.display_name || 'File batch')
    : isFile
      ? (names.values[0] || item.display_name || 'File')
      : (item.display_name || config.label)
  const preview = item.preview_text || item.display_name || `(${config.label})`
  const copyLabel = isFile && names.values.length === 1 ? names.option.one : names.option.label
  const modified = (event) => event.ctrlKey || event.metaKey || event.shiftKey

  return (
    <article
      className={`clipboard-card ${onCheck ? 'clipboard-card--checkable' : ''} ${selected ? 'clipboard-card--selected' : ''} ${checked ? 'clipboard-card--checked' : ''} ${item.available ? '' : 'clipboard-card--missing'} ${busy ? 'clipboard-card--busy' : ''}`}
      data-testid={testId}
      data-item={item.item_id}
      data-checked={checked ? 'true' : 'false'}
      onMouseDown={(event) => {
        // Shift-click selects a range of cards, not page text.
        if (onCheck && event.shiftKey && !event.target.closest('button')) event.preventDefault()
      }}
      onClick={(event) => {
        if (onCheck && modified(event) && !event.target.closest('button, .clipboard-card-check')) {
          onCheck(event)
          return
        }
        if (!event.target.closest('.clipboard-card-check')) onSelect?.(event)
      }}
      onMouseEnter={onHover}
      onDoubleClick={(event) => {
        // Ctrl/Shift clicks only select; they never set the clipboard.
        if (modified(event) || event.target.closest('button, .clipboard-card-check')) return
        onPrimary?.()
      }}
    >
      {onCheck && (
        <label className="clipboard-card-check" onClick={(event) => event.stopPropagation()}>
          <input
            type="checkbox"
            checked={checked}
            aria-label={`Select ${title}`}
            onChange={() => {}}
            onClick={(event) => { event.stopPropagation(); onCheck(event) }}
          />
        </label>
      )}
      <div className="clipboard-card-icon" aria-hidden="true">
        {isImage && thumbnail
          ? <img src={`data:image/x-portable-pixmap;base64,${thumbnail}`} alt="" />
          : <span>{config.icon}</span>}
      </div>

      <div className="clipboard-card-body">
        <div className="clipboard-card-heading">
          <strong title={title}>{title}</strong>
          <span className="clipboard-card-kind">{config.label}</span>
          {item.is_current && <span className="clipboard-card-current">Current</span>}
        </div>

        {isText && <div className="clipboard-card-text">{preview}</div>}

        {(kind === 'html' || kind === 'audio' || kind === 'binary' || isImage) && (
          <div className="clipboard-card-preview">{preview}</div>
        )}

        {(isFile || isBatch) && files && (
          <div className="clipboard-card-file" data-path-display={names.field}>
            <span className="clipboard-card-list-label">{names.option.short}</span>
            {/* A single file already shows its name in the heading. */}
            {isBatch && (
              <span title={names.values.join('\n')}>{names.values.join(', ') || `No ${names.option.label.toLowerCase()}`}</span>
            )}
            <CopyField field={names.field} label={copyLabel} values={names.values} truncated={names.truncated} disabled={busy} onCopy={onCopy} />
          </div>
        )}

        <div className="clipboard-card-meta">
          {item.pinned && <span>Pinned</span>}
          {!item.available && <span>Download required</span>}
          {(isFile || isBatch) && files?.total_files > 0 && <span>{files.total_files} file{files.total_files === 1 ? '' : 's'}</span>}
          {fmtSize(isFile || isBatch ? (files?.total_size || item.total_file_size || item.size) : item.size) && (
            <span>{fmtSize(isFile || isBatch ? (files?.total_size || item.total_file_size || item.size) : item.size)}{isText ? ' UTF-8' : ''}</span>
          )}
          {(files?.truncated || files?.root_names_truncated) && <span>Preview limited</span>}
        </div>

        {progressLines.map((line, index) => (
          <div className={`clipboard-card-progress clipboard-card-progress--${line.kind}`} data-testid="clip-progress" key={`${line.kind}-${index}`}>
            {line.percent != null && (
              <div className="clipboard-card-bar"><div style={{ width: `${line.percent}%` }} /></div>
            )}
            <span>{line.text}</span>
          </div>
        ))}
      </div>

      <div className="clipboard-card-actions">
        <button type="button" disabled={busy} className="clipboard-card-primary" aria-label={primaryLabel}
          onClick={(event) => { event.stopPropagation(); onPrimary?.() }}>
          {item.available ? 'Set' : 'Get'}
        </button>
        <button type="button" disabled={busy} aria-label={item.pinned ? 'Unpin' : 'Pin'}
          onClick={(event) => { event.stopPropagation(); onPin?.() }}>
          {item.pinned ? 'Unpin' : 'Pin'}
        </button>
        <button type="button" disabled={busy} className="clipboard-card-delete" aria-label="Delete"
          onClick={(event) => { event.stopPropagation(); onDelete?.() }}>
          Delete
        </button>
      </div>
    </article>
  )
}
