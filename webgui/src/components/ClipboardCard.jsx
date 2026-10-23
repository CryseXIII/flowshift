import { fmtSize, progressLine } from '../clipboardFormat.js'

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

export default function ClipboardCard({
  item,
  progress,
  busy = false,
  selected = false,
  thumbnail = null,
  testId,
  onSelect,
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
  const line = progressLine(progress)
  const primaryLabel = item.available ? 'Set clipboard' : 'Download'
  const title = isBatch
    ? (files?.root_names?.join(', ') || item.display_name || 'File batch')
    : isFile
      ? (files?.file_names?.[0] || item.display_name || 'File')
      : (item.display_name || config.label)
  const preview = item.preview_text || item.display_name || `(${config.label})`
  const relativePath = files?.relative_paths?.[0]

  return (
    <article
      className={`clipboard-card ${selected ? 'clipboard-card--selected' : ''} ${item.available ? '' : 'clipboard-card--missing'} ${busy ? 'clipboard-card--busy' : ''}`}
      data-testid={testId}
      data-item={item.item_id}
      onClick={onSelect}
      onMouseEnter={onHover}
      onDoubleClick={(event) => {
        if (!event.target.closest('button')) onPrimary?.()
      }}
    >
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

        {isFile && (
          <div className="clipboard-card-file">
            {relativePath && relativePath !== title && <span title={relativePath}>{relativePath}</span>}
            <CopyField field="file_names" label="File name" values={files?.file_names} disabled={busy} onCopy={onCopy} />
            <CopyField field="relative_paths" label="Relative path" values={files?.relative_paths} disabled={busy} onCopy={onCopy} />
          </div>
        )}

        {isBatch && files && (
          <div className="clipboard-card-batch">
            <div className="clipboard-card-file">
              <span className="clipboard-card-list-label">Root</span>
              <span title={files?.root_names?.join('\n')}>{files?.root_names?.join(', ') || title}</span>
              <CopyField field="root_names" label="Root names" values={files?.root_names} truncated={files?.root_names_truncated} disabled={busy} onCopy={onCopy} />
            </div>
            <div className="clipboard-card-file">
              <span className="clipboard-card-list-label">Names</span>
              <span title={files?.file_names?.join('\n')}>{files?.file_names?.join(', ') || 'No file names'}</span>
              <CopyField field="file_names" label="File names" values={files?.file_names} truncated={files?.truncated} disabled={busy} onCopy={onCopy} />
            </div>
            <div className="clipboard-card-file">
              <span className="clipboard-card-list-label">Paths</span>
              <span title={files?.relative_paths?.join('\n')}>{files?.relative_paths?.join(', ') || 'No relative paths'}</span>
              <CopyField field="relative_paths" label="Relative paths" values={files?.relative_paths} truncated={files?.truncated} disabled={busy} onCopy={onCopy} />
            </div>
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

        {line && (
          <div className={`clipboard-card-progress clipboard-card-progress--${line.kind}`} data-testid="clip-progress">
            {line.percent != null && (
              <div className="clipboard-card-bar"><div style={{ width: `${line.percent}%` }} /></div>
            )}
            <span>{line.text}</span>
          </div>
        )}
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
