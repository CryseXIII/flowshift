import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  WHEEL_SIZE, MAX_DOTS, labelPosition, nextPage, sectorPath,
} from './wheelGeometry.js'

// Radial Command Wheel: up to 8 sectors per page, mouse wheel cycles pages,
// dots in the hub show the page, the hovered sector is spotlighted while the
// rest dim. Only the sectors (and the page pill) are drawn: the host clips the
// native window to exactly these shapes, everything else stays see-through.
// While the trigger is held the host passes ``aim`` (sector in the swipe
// direction); it wins over mouse hover.
export default function CommandWheel({ pages, actions, aim = null, onExecute, onPageChange }) {
  const [page, setPage] = useState(0)
  const [hot, setHot] = useState(null)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  const pageCount = pages.length
  const safePage = pageCount ? Math.min(page, pageCount - 1) : 0
  const slots = pageCount ? pages[safePage] : []

  useEffect(() => {
    if (pageCount) onPageChange?.(safePage)
  }, [safePage, pageCount, onPageChange])

  const handleWheel = useCallback((event) => {
    if (pageCount < 2) return
    if (!event.deltaY) return
    setHot(null)
    setPage((current) => nextPage(current, pageCount, event.deltaY))
  }, [pageCount])

  const run = useCallback(async (actionId) => {
    if (busy) return
    setBusy(actionId)
    setError(null)
    try {
      const result = await onExecute(actionId)
      if (result && result.ok === false) setError({ id: actionId, text: result.reason || 'rejected' })
    } catch (e) {
      setError({ id: actionId, text: e?.message || 'failed' })
    } finally {
      setBusy(null)
    }
  }, [busy, onExecute])

  const sectors = useMemo(() => slots.map((id, index) => ({
    id,
    index,
    path: sectorPath(index, slots.length),
    label: labelPosition(index, slots.length),
    meta: actions[id] || { label: id },
  })), [slots, actions])

  if (!pageCount) {
    return (
      <div className="wheel wheel--empty" data-testid="command-wheel">
        <p>No actions configured</p>
      </div>
    )
  }

  const aimed = Number.isInteger(aim) && aim < sectors.length ? aim : null
  const active = aimed !== null ? aimed : hot
  const spotlight = active !== null
  return (
    <div
      className={`wheel ${spotlight ? 'wheel--spotlight' : ''}`}
      data-testid="command-wheel"
      data-page={safePage}
      data-aim={aimed === null ? undefined : aimed}
      onWheel={handleWheel}
      onMouseLeave={() => setHot(null)}
    >
      <svg viewBox={`0 0 ${WHEEL_SIZE} ${WHEEL_SIZE}`} className="wheel-svg" role="menu" aria-label="Command wheel">
        <defs>
          <radialGradient id="wheel-spot" cx="50%" cy="50%" r="60%">
            <stop offset="0%" stopColor="rgba(255,255,255,0.55)" />
            <stop offset="45%" stopColor="rgba(120,180,255,0.35)" />
            <stop offset="100%" stopColor="rgba(40,80,140,0.12)" />
          </radialGradient>
        </defs>
        {sectors.map((sector) => {
          const isHot = active === sector.index
          const state = spotlight ? (isHot ? 'sector--hot' : 'sector--dim') : ''
          const failed = error && error.id === sector.id
          return (
            <g
              key={`${safePage}-${sector.id}`}
              className={`sector ${state} ${busy === sector.id ? 'sector--busy' : ''}`}
              role="menuitem"
              aria-label={sector.meta.label}
              data-action={sector.id}
              tabIndex={-1}
              onMouseEnter={() => setHot(sector.index)}
              onMouseMove={() => hot !== sector.index && setHot(sector.index)}
              onClick={() => run(sector.id)}
            >
              <path className="sector-shape" d={sector.path} />
              <text
                className={`sector-label ${failed ? 'sector-label--error' : ''}`}
                x={sector.label.x}
                y={sector.label.y}
                textAnchor="middle"
                dominantBaseline="middle"
                role={failed ? 'alert' : undefined}
              >
                {failed ? error.text : sector.meta.label}
              </text>
            </g>
          )
        })}
      </svg>
      {pageCount > 1 && (
        <div className="wheel-dots" aria-label={`Page ${safePage + 1} of ${pageCount}`}>
          {pageCount > MAX_DOTS
            ? <span className="wheel-page-text">{`${safePage + 1}/${pageCount}`}</span>
            : pages.map((_, index) => (
              <span key={index} className={`wheel-dot ${index === safePage ? 'wheel-dot--active' : ''}`} data-testid="wheel-dot" />
            ))}
        </div>
      )}
    </div>
  )
}
