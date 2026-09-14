// Pure geometry for the radial Command Wheel (no DOM, unit-testable).

export const MAX_SLOTS = 8
export const WHEEL_SIZE = 360
export const CENTER = WHEEL_SIZE / 2
export const OUTER_RADIUS = 166
export const INNER_RADIUS = 60
export const SECTOR_GAP_DEG = 2.5

function polar(radius, angleDeg) {
  const rad = (angleDeg * Math.PI) / 180
  return [CENTER + radius * Math.cos(rad), CENTER + radius * Math.sin(rad)]
}

function fmt(n) {
  return Number(n.toFixed(3))
}

// Sector i of n starts at the top (-90deg) and runs clockwise. Angles are
// returned in degrees so labels can be positioned on the same basis.
export function sectorAngles(index, count) {
  const step = 360 / count
  const start = -90 - step / 2 + index * step
  return { start, end: start + step, mid: start + step / 2, step }
}

// SVG path for one annular sector. A single slot renders as a full ring
// (two arcs) because an arc of exactly 360 degrees collapses in SVG.
export function sectorPath(index, count) {
  if (count === 1) {
    const [ox1, oy1] = polar(OUTER_RADIUS, -90)
    const [ox2, oy2] = polar(OUTER_RADIUS, 90)
    const [ix1, iy1] = polar(INNER_RADIUS, -90)
    const [ix2, iy2] = polar(INNER_RADIUS, 90)
    return [
      `M ${fmt(ox1)} ${fmt(oy1)}`,
      `A ${OUTER_RADIUS} ${OUTER_RADIUS} 0 1 1 ${fmt(ox2)} ${fmt(oy2)}`,
      `A ${OUTER_RADIUS} ${OUTER_RADIUS} 0 1 1 ${fmt(ox1)} ${fmt(oy1)}`,
      `M ${fmt(ix1)} ${fmt(iy1)}`,
      `A ${INNER_RADIUS} ${INNER_RADIUS} 0 1 0 ${fmt(ix2)} ${fmt(iy2)}`,
      `A ${INNER_RADIUS} ${INNER_RADIUS} 0 1 0 ${fmt(ix1)} ${fmt(iy1)}`,
      'Z',
    ].join(' ')
  }
  const { start, end } = sectorAngles(index, count)
  const gap = SECTOR_GAP_DEG / 2
  const a0 = start + gap
  const a1 = end - gap
  const large = a1 - a0 > 180 ? 1 : 0
  const [ox0, oy0] = polar(OUTER_RADIUS, a0)
  const [ox1, oy1] = polar(OUTER_RADIUS, a1)
  const [ix1, iy1] = polar(INNER_RADIUS, a1)
  const [ix0, iy0] = polar(INNER_RADIUS, a0)
  return [
    `M ${fmt(ox0)} ${fmt(oy0)}`,
    `A ${OUTER_RADIUS} ${OUTER_RADIUS} 0 ${large} 1 ${fmt(ox1)} ${fmt(oy1)}`,
    `L ${fmt(ix1)} ${fmt(iy1)}`,
    `A ${INNER_RADIUS} ${INNER_RADIUS} 0 ${large} 0 ${fmt(ix0)} ${fmt(iy0)}`,
    'Z',
  ].join(' ')
}

// Label anchor in the radial middle of the sector.
export function labelPosition(index, count) {
  const { mid } = sectorAngles(index, count)
  const [x, y] = polar((OUTER_RADIUS + INNER_RADIUS) / 2, mid)
  return { x: fmt(x), y: fmt(y) }
}

// Cyclic paging: last -> first and first -> last.
export function nextPage(current, pageCount, direction) {
  if (pageCount <= 0) return 0
  const delta = direction > 0 ? 1 : direction < 0 ? -1 : 0
  return (((current + delta) % pageCount) + pageCount) % pageCount
}

// Normalize the host payload into renderable pages; invalid input yields [].
export function normalizeWheelData(data) {
  if (!data || typeof data !== 'object' || !Array.isArray(data.pages)) return { pages: [], actions: {} }
  const actions = data.actions && typeof data.actions === 'object' ? data.actions : {}
  const pages = []
  for (const page of data.pages) {
    if (!Array.isArray(page)) continue
    const slots = page
      .filter((id) => typeof id === 'string' && id && actions[id])
      .slice(0, MAX_SLOTS)
    if (slots.length) pages.push(slots)
  }
  return { pages, actions }
}
