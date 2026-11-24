// File previews on clipboard file cards: descriptor fetch, image URLs and
// the tooltip label per file in the chosen path display. The backend answers
// at once ("pending" while it still generates) and the card asks again.

export const PREVIEW_POLL_LIMIT = 8
export const VIDEO_CLIP_SECONDS = 4

export async function fetchFilePreviews(profile, itemId, count) {
  const q = `profile=${encodeURIComponent(profile)}&count=${encodeURIComponent(count)}`
  const r = await fetch(`/api/clipboard/file-previews/${encodeURIComponent(itemId)}?${q}`)
  if (!r.ok) throw new Error(r.statusText || 'preview unavailable')
  return r.json()
}

export function previewUrl(profile, itemId, index, variant, key) {
  const q = new URLSearchParams({ profile })
  if (key) q.set('v', key)
  return `/api/clipboard/file-preview/${encodeURIComponent(itemId)}/${index}/${variant}?${q}`
}

// Back-off for "pending": 300 ms, 600 ms, 1.2 s, then every 2.5 s.
export function nextPollDelay(attempt) {
  return Math.min(300 * 2 ** attempt, 2500)
}

export function hasPending(data) {
  return Array.isArray(data?.previews) && data.previews.some((row) => row?.state === 'pending')
}

// Copied HTML is shown in the detail view without loading anything from the
// network: image addresses, fonts and styles from other hosts stay blocked,
// only embedded data: images and inline styles render.
export const OFFLINE_HTML_POLICY = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:"

export function offlineHtml(html) {
  return `<meta http-equiv="Content-Security-Policy" content="${OFFLINE_HTML_POLICY}">${html || ''}`
}

// Tooltip of one preview tile in the card's path display.
export function previewLabel(files, field, index) {
  const name = files?.file_names?.[index] || ''
  const rel = files?.relative_paths?.[index] || name
  if (field === 'relative_paths') return rel
  if (field === 'root_names') {
    const root = rel.split('/')[0]
    return root && root !== name ? `${root} › ${name}` : name
  }
  return name
}
