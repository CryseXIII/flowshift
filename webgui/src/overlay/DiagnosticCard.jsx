function formatNumber(value, maximumFractionDigits = 2) {
  if (value === null) return 'Waiting for host'
  return new Intl.NumberFormat(undefined, { maximumFractionDigits }).format(value)
}

// Host lifecycle diagnostics; shown only before the first valid update or when
// the runtime explicitly requests `data.diagnostic === true`.
export default function DiagnosticCard({ overlayState }) {
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
            <p className="phase-label">Overlay diagnostic</p>
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
