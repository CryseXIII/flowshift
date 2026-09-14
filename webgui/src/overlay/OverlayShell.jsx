import { useCallback, useEffect, useState } from 'react'
import CommandWheel from './CommandWheel.jsx'
import ClipboardOverlay from './ClipboardOverlay.jsx'
import DiagnosticCard from './DiagnosticCard.jsx'
import { normalizeWheelData } from './wheelGeometry.js'
import * as api from '../api.js'

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
  data: {},
  generation: 0,
}

function sanitizeIdentity(value) {
  if (typeof value !== 'string') return null

  const identity = value.replace(/[\u0000-\u001f\u007f]/g, '').trim()
  if (!identity || identity.length > MAX_IDENTITY_LENGTH) return null
  return identity
}

export function sanitizeOverlayState(value) {
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

  const data = value.data && typeof value.data === 'object' && !Array.isArray(value.data) ? value.data : {}

  return {
    connection: 'connected',
    mode: value.mode,
    target: { kind: target.kind, identity },
    x: value.x,
    y: value.y,
    dpi: value.dpi,
    scale,
    data,
  }
}

// Ask the host to hide; safe in a plain browser without pywebview.
export function requestHide() {
  try {
    const result = window.pywebview?.api?.hide_overlay?.()
    if (result && typeof result.catch === 'function') result.catch(() => {})
  } catch {
    // The shell also runs in a regular browser for development.
  }
}

function OverlayShell() {
  const [overlayState, setOverlayState] = useState(initialState)

  useEffect(() => {
    const bridge = {
      update(value) {
        const nextState = sanitizeOverlayState(value)
        if (!nextState) {
          setOverlayState((current) => ({ ...current, connection: 'invalid' }))
          return false
        }

        setOverlayState((current) => ({ ...nextState, generation: current.generation + 1 }))
        return true
      },
    }

    window.flowshiftOverlay = bridge

    const handleKeyDown = (event) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      requestHide()
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => {
      window.removeEventListener('keydown', handleKeyDown)
      if (window.flowshiftOverlay === bridge) delete window.flowshiftOverlay
    }
  }, [])

  const execute = useCallback(async (actionId, context) => {
    const result = await api.executeAction(actionId, { source: 'command_wheel', ...(context || {}) })
    if (result && result.ok) requestHide()
    return result
  }, [])

  const { mode, data, connection, generation } = overlayState
  const diagnostic = connection !== 'connected' || data.diagnostic === true

  if (diagnostic) {
    return <DiagnosticCard overlayState={overlayState} />
  }

  if (mode === 'command_wheel') {
    const wheel = normalizeWheelData(data)
    return (
      <main className="overlay-stage overlay-stage--wheel">
        <CommandWheel key={generation} pages={wheel.pages} actions={wheel.actions} onExecute={execute} />
      </main>
    )
  }

  return (
    <main className="overlay-stage overlay-stage--clipboard">
      <ClipboardOverlay key={generation} data={data} onClose={requestHide} />
    </main>
  )
}

export default OverlayShell
