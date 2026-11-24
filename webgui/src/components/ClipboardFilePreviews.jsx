import { useEffect, useRef, useState } from 'react'
import {
  PREVIEW_POLL_LIMIT, VIDEO_CLIP_SECONDS, fetchFilePreviews, hasPending, nextPollDelay,
  previewLabel, previewUrl,
} from '../clipboardPreview.js'

// Loads the preview descriptors of a file card once it scrolls into view and
// asks again while the backend still generates. Never blocks the list.
export function useFilePreviews(itemId, profile, count, enabled) {
  const [data, setData] = useState(null)
  const ref = useRef(null)

  useEffect(() => {
    if (!enabled || !profile || !itemId) return undefined
    let cancelled = false
    let timer = null
    let attempt = 0
    let observer = null
    const load = async () => {
      try {
        const next = await fetchFilePreviews(profile, itemId, count)
        if (cancelled) return
        setData(next)
        if (hasPending(next) && attempt < PREVIEW_POLL_LIMIT) {
          timer = setTimeout(load, nextPollDelay(attempt))
          attempt += 1
        }
      } catch {
        if (!cancelled) setData({ previews: [], failed: true })
      }
    }
    const node = ref.current
    if (node && typeof IntersectionObserver === 'function') {
      observer = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          observer.disconnect()
          load()
        }
      }, { rootMargin: '200px' })
      observer.observe(node)
    } else {
      load()
    }
    return () => {
      cancelled = true
      clearTimeout(timer)
      observer?.disconnect()
    }
  }, [itemId, profile, count, enabled])

  return [data, ref]
}

// One preview tile. Missing, moved or locked files, unknown types and
// failed image loads show the same neutral symbol with the extension.
// Copied pictures (source "clipboard") are shown whole on a checkerboard,
// so transparent parts stay visible as such.
export function FilePreviewTile({ preview, profile, itemId, label, hovered = false, animation = 'always', videoHover = true, ext = '' }) {
  const [broken, setBroken] = useState(false)
  const fromClipboard = preview?.source === 'clipboard'
  const shownExt = (fromClipboard ? 'img' : (preview?.ext || ext || '')).slice(0, 4)
  if (!preview || broken || !preview.still) {
    return (
      <span
        className={`clip-preview-tile clip-preview-tile--neutral ${preview?.state === 'pending' ? 'clip-preview-tile--pending' : ''}`}
        data-testid="clip-preview-tile"
        data-kind="neutral"
        data-state={preview?.state || 'pending'}
        title={label}
      >
        <span className="clip-preview-ext">{shownExt || 'FILE'}</span>
      </span>
    )
  }
  const url = (variant) => previewUrl(profile, itemId, preview.index, variant, preview.key)
  const animate = preview.anim && (animation === 'always' || hovered)
  const playClip = preview.kind === 'video' && preview.video && videoHover && hovered
  return (
    <span className={`clip-preview-tile clip-preview-tile--${preview.kind} ${fromClipboard ? 'clip-preview-tile--picture' : ''}`} data-testid="clip-preview-tile" data-kind={preview.kind} data-state={preview.state} title={label}>
      <img
        src={url(animate ? 'anim' : 'still')}
        alt=""
        loading="lazy"
        decoding="async"
        draggable={false}
        onError={() => setBroken(true)}
      />
      {playClip && (
        <video
          src={url('video')}
          muted
          autoPlay
          loop
          playsInline
          preload="metadata"
          aria-hidden="true"
          onTimeUpdate={(event) => {
            // A short muted clip: the first seconds in a loop.
            if (event.currentTarget.currentTime > VIDEO_CLIP_SECONDS) event.currentTarget.currentTime = 0
          }}
          onError={(event) => { event.currentTarget.style.display = 'none' }}
        />
      )}
      {preview.kind === 'video' && <span className="clip-preview-play" aria-hidden="true">▶</span>}
    </span>
  )
}

// Row of previews for a multi-file card: the first files and "+N" for the rest.
export default function ClipboardFileThumbs({ data, files, field, profile, itemId, hovered, totalFiles }) {
  const rows = Array.isArray(data?.previews) ? data.previews : []
  const shown = rows.length || Math.min(files?.file_names?.length || 0, 4)
  const total = Number(totalFiles || data?.total_files || 0)
  const rest = Math.max(0, total - shown)
  const tiles = rows.length
    ? rows
    : Array.from({ length: shown }, (_, index) => ({
      index, state: data?.failed ? 'missing' : 'pending', ext: extOf(files?.file_names?.[index]),
    }))
  if (!tiles.length) return null
  return (
    <div className="clip-preview-row" data-testid="clip-preview-row">
      {tiles.map((preview) => (
        <FilePreviewTile
          key={preview.index}
          preview={preview}
          profile={profile}
          itemId={itemId}
          label={previewLabel(files, field, preview.index)}
          hovered={hovered}
          animation={data?.animation}
          videoHover={data?.video_hover !== false}
        />
      ))}
      {rest > 0 && <span className="clip-preview-more" data-testid="clip-preview-more" title={`${rest} more file${rest === 1 ? '' : 's'}`}>+{rest}</span>}
    </div>
  )
}

export function extOf(name) {
  const base = String(name || '').split('/').pop()
  const dot = base.lastIndexOf('.')
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : ''
}
