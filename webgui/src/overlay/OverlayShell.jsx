import { useEffect, useState } from 'react'

const VALID_MODES = new Set(['clipboard', 'command_wheel'])
const VALID_TARGET_KINDS = new Set(['local', 'remote'])
const MAX_IDENTITY_LENGTH = 160

const initialState = {
  connection: 'waiting',
  mode: null,
  target: null,
  x: null,
  y: null,
  dpi: null,
  scale: null,
}

function sanitizeIdentity(value) {
  if (typeof value !== 'string') return null

  const identity = value.replace(/[\u0000-\u001f\u007f]/g, '').trim()
  if (!identity || identity.length > MAX_IDENTITY_LENGTH) return null
  return identity
}

function sanitizeOverlayState(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  if (!VALID_MODES.has(value.mode)) return null

  const target = value.target
  if (!target || typeof target !== 'object' || Array.isArray(target)) return null
  if (!VALID_TARGET_KINDS.has(target.kind)) return null

  const identity = sanitizeIdentity(target.identity)
  if (!identity) return null
  if (target.kind === 'local' && identity !== 'local') return null
  if (target.kind === 'remote' && identity === 'local') return null

  if (!Number.isInteger(value.x) || !Number.isInteger(value.y)) return null
  if (!Number.isFinite(value.dpi) || value.dpi <= 0) return null

  const scale = value.scale ?? value.dpi / 96
  if (!Number.isFinite(scale) || scale <= 0) return null

  return {
    connection: 'connected',
    mode: value.mode,
    target: { kind: target.kind, identity },
    x: value.x,
    y: value.y,
    dpi: value.dpi,
    scale,
  }
}

function formatNumber(value, maximumFractionDigits = 2) {
  if (value === null) return 'Waiting for host'
  return new Intl.NumberFormat(undefined, { maximumFractionDigits }).format(value)
}

function OverlayShell() {
  const [overlayState, setOverlayState] = useState(initialState)

  useEffect(() => {
    const api = {
      update(value) {
        const nextState = sanitizeOverlayState(value)
        if (!nextState) {
          setOverlayState((current) => ({ ...current, connection: 'invalid' }))
          return false
        }

        setOverlayState(nextState)
        return true
      },
    }

    window.flowshiftOverlay = api

    const handleKeyDown = (event) => {
      if (event.key !== 'Escape') return
      event.preventDefault()

      try {
        const result = window.pywebview?.api?.hide_overlay?.()
        if (result && typeof result.catch === 'function') result.catch(() => {})
      } catch {
        // The diagnostic shell also runs in a regular browser without pywebview.
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => {
      window.removeEventListener('keydown', handleKeyDown)
      if (window.flowshiftOverlay === api) delete window.flowshiftOverlay
    }
  }, [])

  const status = {
    waiting: ['Waiting', 'Waiting for the first verified host update'],
    connected: ['Connected', 'Host diagnostic data received'],
    invalid: ['Update rejected', 'Last valid diagnostic data is shown'],
  }[overlayState.connection]

  const target = overlayState.target
    ? `${overlayState.target.kind} / ${overlayState.target.identity}`
    : 'Waiting for host'

  return (
    <main className="overlay-stage">
      <section className="diagnostic-card" aria-labelledby="overlay-title">
        <header className="diagnostic-header">
          <div>
            <p className="phase-label">Phase 1 diagnostic shell</p>
            <h1 id="overlay-title">FlowShift Overlay</h1>
          </div>
          <div className={`connection connection--${overlayState.connection}`} aria-live="polite">
            <span className="connection-dot" aria-hidden="true" />
            <span>{status[0]}</span>
          </div>
        </header>

        <dl className="diagnostic-grid">
          <div className="diagnostic-field">
            <dt>Mode</dt>
            <dd>{overlayState.mode ?? 'Waiting for host'}</dd>
          </div>
          <div className="diagnostic-field">
            <dt>Target</dt>
            <dd title={target}>{target}</dd>
          </div>
          <div className="diagnostic-field">
            <dt>Physical Position</dt>
            <dd>
              {overlayState.x === null
                ? 'Waiting for host'
                : `x ${formatNumber(overlayState.x, 0)}, y ${formatNumber(overlayState.y, 0)} px`}
            </dd>
          </div>
          <div className="diagnostic-field">
            <dt>DPI / Scale</dt>
            <dd>
              {overlayState.dpi === null
                ? 'Waiting for host'
                : `${formatNumber(overlayState.dpi)} DPI / ${formatNumber(overlayState.scale)}x`}
            </dd>
          </div>
        </dl>

        <footer className="diagnostic-footer">
          <span>{status[1]}</span>
          <kbd>Esc</kbd>
          <span>hide</span>
        </footer>
      </section>
    </main>
  )
}

export default OverlayShell
