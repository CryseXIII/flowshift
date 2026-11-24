import { useEffect, useRef, useState } from 'react'
import * as api from '../api.js'

// Software Update on the development PC: one ZIP of the installed version for
// a USB stick. Other PCs unpack it and double-click install_flowshift.bat;
// they need no Git, no Node.js, no Forgejo access and no internet.
function formatSize(bytes) {
  if (typeof bytes !== 'number' || bytes <= 0) return null
  return `${(bytes / (1024 * 1024)).toFixed(0)} MB`
}

export default function UsbPackagePanel() {
  const [info, setInfo] = useState(null)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState(null)
  const mountedRef = useRef(false)
  const runningRef = useRef(false)

  const load = async () => {
    try {
      const next = await api.getUsbPackage()
      if (!mountedRef.current) return
      runningRef.current = next?.package?.state === 'running'
      setInfo(next)
    } catch (err) {
      if (mountedRef.current) setError(`Could not load the USB package state: ${err.message}`)
    }
  }

  useEffect(() => {
    mountedRef.current = true
    load()
    const timer = window.setInterval(() => {
      if (runningRef.current) load()
    }, 1500)
    return () => {
      mountedRef.current = false
      window.clearInterval(timer)
    }
  }, [])

  const create = async (options = {}) => {
    if (starting) return
    setStarting(true)
    setError(null)
    try {
      const next = await api.createUsbPackage(options)
      if (!mountedRef.current) return
      runningRef.current = next?.package?.state === 'running'
      setInfo(next)
    } catch (err) {
      if (mountedRef.current) setError(`The USB package was not started: ${err.message}`)
    } finally {
      if (mountedRef.current) setStarting(false)
    }
  }

  const pkg = info?.package
  const running = pkg?.state === 'running'
  const size = formatSize(pkg?.size)
  // After a failed python.org download: save the same file with the browser,
  // then let the build use it (its SHA-256 is checked the same way).
  const fallback = pkg?.state === 'failed' ? pkg.python_fallback : null

  return (
    <div className="update-notice" data-testid="usb-package">
      <strong>USB package for other PCs</strong>
      <span>
        Saves one ZIP of the version installed here (with the WebGUI, Python and all packages) in your
        Downloads folder. On the other PC: unpack it and double-click install_flowshift.bat; no Git,
        Node.js or Forgejo needed, an older FlowShift there is updated.
      </span>
      <div className="update-actions">
        <button
          type="button"
          className="btn btn-outline btn-sm"
          onClick={() => create()}
          disabled={starting || running || !info?.available}
          title="Create the USB package of the installed version"
        >
          <i className={`fas ${starting || running ? 'fa-spinner fa-spin' : 'fa-file-zipper'}`} aria-hidden="true" />
          {' '}{running ? 'Creating USB package...' : 'Create USB package'}
        </button>
      </div>
      {running && pkg.step && <span aria-live="polite">{pkg.step}...</span>}
      {pkg?.state === 'done' && (
        <span className="update-usb-result" aria-live="polite" data-testid="usb-package-done">
          Saved: {pkg.path}{size ? ` (${size})` : ''}
        </span>
      )}
      {pkg?.state === 'failed' && (
        <span className="msg-box error" aria-live="polite">{pkg.message}</span>
      )}
      {fallback && (
        <div className="update-usb-fallback" data-testid="usb-package-python-fallback">
          <span>
            1. Download{' '}
            <a href={fallback.url} target="_blank" rel="noreferrer">{fallback.file}</a>
            {' '}from python.org with your browser and save it in {fallback.folder || 'your Downloads folder'}.
            {' '}2. Click the button below. FlowShift checks the file the same way; a wrong file is refused.
          </span>
          <div className="update-actions">
            <button
              type="button"
              className="btn btn-outline btn-sm"
              onClick={() => create({ pythonInstaller: 'downloads' })}
              disabled={starting || running || !info?.available}
            >
              <i className="fas fa-file-import" aria-hidden="true" />
              {' '}Use downloaded Python installer
            </button>
          </div>
        </div>
      )}
      {error && <span className="msg-box error" aria-live="polite">{error}</span>}
    </div>
  )
}
