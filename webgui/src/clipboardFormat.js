// Shared, pure helpers for clipboard list rendering (overlay and WebGUI).

export function fmtSize(bytes) {
  if (!bytes || bytes <= 0) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} MB`
  return `${(bytes / 1073741824).toFixed(2)} GB`
}

export function fmtRate(bps) {
  if (!bps || bps <= 0) return ''
  if (bps < 1024) return `${bps.toFixed(0)} B/s`
  if (bps < 1048576) return `${(bps / 1024).toFixed(1)} KB/s`
  return `${(bps / 1048576).toFixed(1)} MB/s`
}

export function fmtEta(sec) {
  if (sec == null || sec < 0 || !isFinite(sec)) return ''
  if (sec < 60) return `${Math.round(sec)}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`
  return `${Math.floor(sec / 3600)}h ${Math.round((sec % 3600) / 60)}m`
}

const V2_ACTIVE = new Set(['created', 'preflight', 'accepted', 'sending_manifest', 'receiving',
  'transferring', 'verifying', 'finalizing', 'resuming'])
const V2_WAITING = new Set(['paused', 'waiting_reconnect'])

// Merge the legacy progress snapshot (item_id -> job) with stream V2 session
// records into one item_id -> {status, percent, done, total, rate, eta, label}
// map. V2 wins when both exist for the same item.
export function mergeTransferProgress(legacy, streamV2) {
  const out = {}
  if (legacy && typeof legacy === 'object') {
    for (const [itemId, job] of Object.entries(legacy)) {
      if (!job || typeof job !== 'object') continue
      out[itemId] = {
        status: job.status,
        percent: Number(job.percent) || 0,
        done: job.received_bytes ?? job.sent_bytes ?? null,
        total: job.total_bytes ?? null,
        rate: job.bytes_per_second ?? null,
        eta: job.eta_seconds ?? null,
        error: job.error || null,
        retry: job.retry_count || 0,
        strategy: 'legacy',
      }
    }
  }
  if (Array.isArray(streamV2)) {
    for (const rec of streamV2) {
      if (!rec || typeof rec !== 'object' || !rec.item_id) continue
      const state = String(rec.state || '')
      let status
      if (state === 'completed') status = 'completed'
      else if (state === 'cancelled') status = 'cancelled'
      else if (state === 'failed') status = 'failed'
      else if (V2_WAITING.has(state)) status = 'paused'
      else if (V2_ACTIVE.has(state)) status = 'running'
      else status = 'pending'
      out[rec.item_id] = {
        status,
        percent: Number(rec.percent) || 0,
        done: rec.bytes_done ?? null,
        total: rec.total_bytes ?? null,
        rate: rec.rate_bytes_per_s ?? null,
        eta: rec.eta_seconds ?? null,
        error: rec.error_code || null,
        retry: rec.retry_count || 0,
        strategy: 'stream_v2',
        state,
        currentFile: rec.current_file || null,
        fileIndex: rec.file_index,
        fileCount: rec.file_count,
      }
    }
  }
  return out
}

export function progressLine(p) {
  if (!p) return null
  if (p.status === 'completed') return null
  if (p.status === 'cancelled') return { kind: 'cancelled', text: 'Cancelled' }
  if (p.status === 'failed') return { kind: 'failed', text: `Failed${p.error ? `: ${p.error}` : ''}` }
  if (p.status === 'waiting_manual') return { kind: 'manual', text: 'Manual download required' }
  if (p.status === 'pending') return { kind: 'pending', text: 'Queued' }
  if (p.status === 'paused') return { kind: 'paused', text: `Paused ${Math.round(p.percent)}%`, percent: p.percent }
  const parts = [`${Math.round(p.percent)}%`]
  if (p.done != null && p.total != null) parts.push(`${fmtSize(p.done) || '0 B'}/${fmtSize(p.total)}`)
  const rate = fmtRate(p.rate)
  if (rate) parts.push(rate)
  const eta = fmtEta(p.eta)
  if (eta) parts.push(`ETA ${eta}`)
  if (p.fileCount > 1 && p.fileIndex != null) parts.push(`file ${p.fileIndex + 1}/${p.fileCount}`)
  return { kind: 'running', text: parts.join(' · '), percent: Math.min(100, Math.max(0, p.percent)) }
}
