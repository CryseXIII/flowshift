import { useCallback, useMemo, useState } from 'react'
import {
  WHEEL_SIZE, CENTER, INNER_RADIUS, labelPosition, nextPage, sectorPath,
} from './wheelGeometry.js'

// Radial Command Wheel: up to 8 sectors per page, mouse wheel cycles pages,
// dots show the page, the hovered sector is spotlighted while the rest dim.
export default function CommandWheel({ pages, actions, onExecute }) {
  const [page, setPage] = useState(0)
  const [hot, setHot] = useState(null)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  const pageCount = pages.length
  const safePage = pageCount ? Math.min(page, pageCount - 1) : 0
  const slots = pageCount ? pages[safePage] : []

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
      if (result && result.ok === false) setError(result.reason || 'rejected')
    } catch (e) {
      setError(e?.message || 'failed')
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

  const spotlight = hot !== null
  return (
    <div
      className={`wheel ${spotlight ? 'wheel--spotlight' : ''}`}
      data-testid="command-wheel"
      data-page={safePage}
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
        <circle className="wheel-hub" cx={CENTER} cy={CENTER} r={INNER_RADIUS - 6} />
        {sectors.map((sector) => {
          const isHot = hot === sector.index
          const state = spotlight ? (isHot ? 'sector--hot' : 'sector--dim') : ''
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
              <text className="sector-label" x={sector.label.x} y={sector.label.y} textAnchor="middle" dominantBaseline="middle">
                {sector.meta.label}
              </text>
            </g>
          )
        })}
        <text className="wheel-hub-text" x={CENTER} y={CENTER} textAnchor="middle" dominantBaseline="middle">
          {hot !== null && sectors[hot] ? sectors[hot].meta.label : `${safePage + 1}/${pageCount}`}
        </text>
      </svg>
      <div className="wheel-dots" aria-label={`Page ${safePage + 1} of ${pageCount}`}>
        {pages.map((_, index) => (
          <span key={index} className={`wheel-dot ${index === safePage ? 'wheel-dot--active' : ''}`} data-testid="wheel-dot" />
        ))}
      </div>
      {error && <div className="wheel-error" role="alert">{error}</div>}
    </div>
  )
}
