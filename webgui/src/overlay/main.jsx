import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import OverlayShell from './OverlayShell.jsx'
import './overlay.css'

createRoot(document.getElementById('overlay-root')).render(
  <StrictMode>
    <OverlayShell />
  </StrictMode>,
)
