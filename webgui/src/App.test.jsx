import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App.jsx'
import * as api from './api.js'

vi.mock('./api.js', () => ({
  getStatus: vi.fn(),
  subscribeSSE: vi.fn(),
}))
vi.mock('./components/ErrorBoundary.jsx', () => ({ default: ({ children }) => children }))
vi.mock('./components/Dashboard.jsx', () => ({ default: () => <div data-testid="dashboard-view" /> }))
vi.mock('./components/ClipboardView.jsx', () => ({ default: () => <div data-testid="clipboard-view" /> }))
vi.mock('./components/SettingsPanel.jsx', () => ({ default: () => <div data-testid="settings-view" /> }))
vi.mock('./components/DisplayConfig.jsx', () => ({ default: () => <div data-testid="display-view" /> }))
vi.mock('./components/PeersPanel.jsx', () => ({ default: () => <div data-testid="peers-view" /> }))
vi.mock('./components/DiagnosticsPanel.jsx', () => ({ default: () => <div data-testid="diagnostics-view" /> }))
vi.mock('./components/EventLog.jsx', () => ({ default: () => <div data-testid="log-view" /> }))

describe('App routes', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getStatus.mockResolvedValue({ device_name: 'Local' })
    api.subscribeSSE.mockReturnValue(vi.fn())
  })

  afterEach(() => window.history.replaceState(null, '', '/'))

  it('opens the clipboard view directly at /clipboard', () => {
    window.history.replaceState(null, '', '/clipboard')
    render(<App />)
    expect(screen.getByTestId('clipboard-view')).toBeVisible()
    expect(screen.queryByTestId('dashboard-view')).not.toBeInTheDocument()
  })

  it('falls back to the dashboard for an unknown route', () => {
    window.history.replaceState(null, '', '/unknown')
    render(<App />)
    expect(screen.getByTestId('dashboard-view')).toBeVisible()
  })

  it('updates the URL when selecting a tab', () => {
    window.history.replaceState(null, '', '/')
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
    expect(screen.getByTestId('settings-view')).toBeVisible()
    expect(window.location.pathname).toBe('/settings')
  })
})
