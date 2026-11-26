import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import PeerUpdatePanel from './PeerUpdatePanel.jsx'
import * as api from '../api.js'

vi.mock('../api.js', () => ({
  getPeerUpdate: vi.fn(),
  savePeerUpdateSettings: vi.fn(),
  peerUpdateAction: vi.fn(),
}))

function status(overrides = {}) {
  return {
    ok: true,
    peer_update: {
      state: 'idle',
      installed_version: '0.7.0-dev.40',
      channel: 'stable',
      policy: 'notify',
      allow_development: false,
      automatic_enabled: true,
      development_mode: false,
      offer: null,
      progress: null,
      install_at: null,
      wait_reason: null,
      error: null,
      last: null,
      log: [],
      ...overrides,
    },
  }
}

const OFFER = { version: '0.7.0-dev.90', size: 5 * 1024 * 1024, peer_name: 'Viktor-PC', development: true, received_at: '2026-11-25T16:00:00Z' }

describe('PeerUpdatePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getPeerUpdate.mockResolvedValue(status())
    api.savePeerUpdateSettings.mockResolvedValue({ ok: true })
  })

  it('defaults to notify only without development versions', async () => {
    render(<PeerUpdatePanel />)
    expect(await screen.findByTestId('peer-update-policy')).toHaveValue('notify')
    expect(screen.getByTestId('peer-update-development')).not.toBeChecked()
    expect(screen.getByText(/Nothing is transferred before that/)).toBeInTheDocument()
    // No offer: no download/install buttons at all, the state line explains why.
    expect(screen.getByTestId('peer-update-state')).toHaveTextContent('No connected device has a newer version.')
    expect(screen.queryByTestId('peer-update-actions')).toBeNull()
    expect(screen.queryByLabelText('Download from the connected device')).toBeNull()
    expect(screen.queryByLabelText('Install from the connected device and restart')).toBeNull()
    // Nothing changed yet: Save is shown with its text but disabled.
    expect(screen.getByLabelText('Save setting for connected devices')).toHaveTextContent('Save')
    expect(screen.getByLabelText('Save setting for connected devices')).toBeDisabled()
  })

  it('draws every button with an inline symbol and visible text (Viktor 26.11.: empty squares)', async () => {
    api.getPeerUpdate.mockResolvedValue(status({ state: 'downloading', offer: OFFER, progress: { bytes: 1, total: 2 } }))
    render(<PeerUpdatePanel />)
    const buttons = {
      'Download from the connected device': ['download', 'Download'],
      'Install from the connected device and restart': ['install', 'Install'],
      'Cancel the update from the connected device': ['xmark', 'Cancel'],
      'Save setting for connected devices': ['save', 'Save'],
    }
    for (const [label, [icon, text]] of Object.entries(buttons)) {
      const button = await screen.findByRole('button', { name: label })
      expect(button).toHaveAttribute('title')
      expect(button.textContent.trim()).toBe(text)
      expect(button.querySelector(`svg.button-icon[data-icon="${icon}"]`)).not.toBeNull()
      expect(button.querySelector('i.fas')).toBeNull()
    }
  })

  it('shows the offer and installs on click', async () => {
    api.getPeerUpdate.mockResolvedValue(status({ state: 'available', offer: OFFER }))
    api.peerUpdateAction.mockResolvedValue(status({ state: 'downloading', offer: OFFER, progress: { bytes: 1, total: 2 } }))
    render(<PeerUpdatePanel />)
    expect(await screen.findByTestId('peer-update-offer')).toHaveTextContent('Viktor-PC offers FlowShift 0.7.0-dev.90')
    fireEvent.click(screen.getByLabelText('Install from the connected device and restart'))
    await waitFor(() => expect(api.peerUpdateAction).toHaveBeenCalledWith('install'))
    expect(await screen.findByLabelText('Cancel the update from the connected device')).toBeEnabled()
  })

  it('saves the one setting that turns on automatic install and restart', async () => {
    render(<PeerUpdatePanel />)
    fireEvent.change(await screen.findByTestId('peer-update-policy'), { target: { value: 'install' } })
    fireEvent.click(screen.getByTestId('peer-update-development'))
    expect(screen.getByText(/You are told a minute before and can cancel/)).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Save setting for connected devices'))
    await waitFor(() => expect(api.savePeerUpdateSettings).toHaveBeenCalledWith({ policy: 'install', allow_development: true }))
  })

  it('explains a refused package and the last result', async () => {
    api.getPeerUpdate.mockResolvedValue(status({
      state: 'failed',
      offer: OFFER,
      error: { code: 'file_damaged', message: 'x' },
      last: { from_version: '0.7.0-dev.30', to_version: '0.7.0-dev.40', peer_name: 'Viktor-PC', handed_off_at: '2026-11-24T10:00:00Z', installed: true, result: 'success' },
      log: [{ at: '2026-11-25T16:00:00Z', level: 'WARN', text: 'stopped: file_damaged' }],
    }))
    render(<PeerUpdatePanel />)
    expect(await screen.findByTestId('peer-update-error')).toHaveTextContent('nothing was installed')
    expect(screen.getByTestId('peer-update-last')).toHaveTextContent('installed and running')
    expect(screen.getByText(/What happened \(1\)/)).toBeInTheDocument()
  })
})
