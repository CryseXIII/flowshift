import { useState } from 'react'
import { FILE_COPY_FIELDS, LOCAL_PATHS_ONLY, fmtSize, progressRows } from '../clipboardFormat.js'
import { previewLabel } from '../clipboardPreview.js'
import ClipboardFileThumbs, { FilePreviewTile, extOf, useFilePreviews } from './ClipboardFilePreviews.jsx'

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

// Copy actions of a file card: full path, folder path, file name, root name
// and relative path, each as text on the Windows clipboard (one line per
// file). FlowShift recognizes its own write, so no new history item appears.
// "Set" next to Pin/Delete still puts the files themselves on the clipboard.
export function FileCopyActions({ files, disabled, onCopy }) {
  if (!files || !onCopy) return null
  const options = FILE_COPY_FIELDS.filter((option) => option.local || files[option.field]?.length)
  if (!options.length) return null
  return (
    <div className="clipboard-card-copies" role="group" aria-label="Copy as text">
      <span className="clipboard-card-list-label">Copy</span>
      {options.map((option) => {
        const unavailable = option.local && !files.local_paths
        const truncated = option.field === 'root_names' ? files.root_names_truncated
          : (!option.local && files.truncated)
        const hint = unavailable ? `${option.hint}. ${LOCAL_PATHS_ONLY}.`
          : truncated ? `${option.hint} (only the visible entries)` : option.hint
        return (
          <button
            key={option.field}
            type="button"
            className={`clipboard-card-copy ${unavailable ? 'clipboard-card-copy--unavailable' : ''}`}
            data-field={option.field}
            disabled={disabled}
            aria-disabled={unavailable ? 'true' : undefined}
            aria-label={`Copy ${option.label}`}
            title={hint}
            onClick={(event) => {
              event.stopPropagation()
              if (!unavailable) onCopy(option.field)
            }}
          >
            {option.button}
          </button>
        )
      })}
    </div>
  )
}

export default function ClipboardCard({
  item,
  progress,
  busy = false,
  selected = false,
  checked = false,
  thumbnail = null,
  previewProfile = null,
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
  const fileNames = Array.isArray(files?.file_names) ? files.file_names.filter(Boolean) : []
  const title = isBatch
    ? (item.display_name || 'File batch')
    : isFile
      ? (fileNames[0] || item.display_name || 'File')
      : (item.display_name || config.label)
  const preview = item.preview_text || item.display_name || `(${config.label})`
  const modified = (event) => event.ctrlKey || event.metaKey || event.shiftKey
  // File previews: one tile in the icon slot for a single file, a row with
  // "+N" for several files. Animations and video clips react to hover.
  const [hovered, setHovered] = useState(false)
  const withPreviews = Boolean(previewProfile) && (isFile || isBatch) && Boolean(files)
  const [previews, previewRef] = useFilePreviews(item.item_id, previewProfile, isBatch ? 4 : 1, withPreviews)

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
      onMouseEnter={(event) => { setHovered(true); onHover?.(event) }}
      onMouseLeave={() => setHovered(false)}
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
      <div className={`clipboard-card-icon ${withPreviews && isFile ? 'clipboard-card-icon--preview' : ''}`} aria-hidden="true" ref={previewRef}>
        {withPreviews && isFile ? (
          <FilePreviewTile
            preview={previews?.previews?.[0] || { index: 0, state: previews?.failed ? 'missing' : 'pending' }}
            ext={extOf(files.file_names?.[0])}
            profile={previewProfile}
            itemId={item.item_id}
            label={previewLabel(files, 'relative_paths', 0)}
            hovered={hovered}
            animation={previews?.animation}
            videoHover={previews?.video_hover !== false}
          />
        ) : isImage && thumbnail
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

        {withPreviews && isBatch && (
          <ClipboardFileThumbs
            data={previews}
            files={files}
            field="relative_paths"
            profile={previewProfile}
            itemId={item.item_id}
            hovered={hovered}
            totalFiles={files.total_files}
          />
        )}

        {isBatch && files && (
          <div className="clipboard-card-file">
            <span className="clipboard-card-list-label">Files</span>
            <span title={(files.relative_paths || fileNames).join('\n')}>{fileNames.join(', ') || 'No file names'}</span>
          </div>
        )}

        {(isFile || isBatch) && <FileCopyActions files={files} disabled={busy} onCopy={onCopy} />}

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
