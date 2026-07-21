import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SoftwareUpdateSection from './SoftwareUpdateSection.jsx'
import * as api from '../api.js'

vi.mock('../api.js', () => ({
  getUpdateStatus: vi.fn(),
  checkForUpdates: vi.fn(),
  downloadUpdate: vi.fn(),
  installUpdate: vi.fn(),
  saveUpdateSettings: vi.fn(),
}))

function update(overrides = {}) {
  return {
    state: 'update_available',
    current_version: '0.4.0',
    latest_version: '0.5.0',
    channel: 'stable',
    policy: 'notify',
    enabled: true,
    check_on_start: true,
    last_check_at: '2026-07-21T10:00:00Z',
    last_successful_check_at: '2026-07-21T10:00:00Z',
    release_notes: '<b>Security fixes</b>\nSecond line',
    release_url: 'https://github.com/CryseXIII/flowshift/releases/tag/v0.5.0',
    downloaded_asset: null,
    progress: {
      bytes_downloaded: 0,
      bytes_total: 2097152,
      percentage: 0,
      bytes_per_second: 0,
      eta_seconds: null,
    },
    can_install: false,
    operation_active: false,
    blocked_reason: 'update_not_downloaded',
    development_mode: false,
    last_error: null,
    last_update_result: null,
    recovery_notices: [],
    ...overrides,
  }
}

function response(overrides) {
  return { ok: true, update: update(overrides) }
}

describe('SoftwareUpdateSection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getUpdateStatus.mockResolvedValue(response())
    api.checkForUpdates.mockResolvedValue({ status: 'queued', message: 'Check queued' })
    api.downloadUpdate.mockResolvedValue({ status: 'queued', message: 'Download queued' })
    api.installUpdate.mockResolvedValue({ status: 'queued', message: 'Install queued' })
    api.saveUpdateSettings.mockResolvedValue({ ok: true })
  })

  it('renders installed/latest status, controls, and plaintext release notes', async () => {
    render(<SoftwareUpdateSection />)

    expect(await screen.findByText('0.4.0')).toBeVisible()
    expect(screen.getByText('0.5.0')).toBeVisible()
    expect(screen.getByText('Update available')).toBeVisible()
    expect(screen.getByLabelText('Channel')).toHaveValue('stable')
    expect(screen.getByLabelText('Policy')).toHaveValue('notify')
    expect(screen.getByText('<b>Security fixes</b>', { exact: false })).toBeVisible()
    expect(document.querySelector('.update-release-notes b')).toBeNull()
  })

  it('shows status API errors', async () => {
    api.getUpdateStatus.mockRejectedValueOnce(new Error('runtime unavailable'))
    render(<SoftwareUpdateSection />)

    expect(await screen.findByText('Could not load update status: runtime unavailable')).toBeVisible()
  })

  it('enables only state-valid operation buttons', async () => {
    render(<SoftwareUpdateSection />)
    await screen.findByText('Update available')

    expect(screen.getByRole('button', { name: 'Check' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Install' })).toBeDisabled()
  })

  it('keeps controls disabled while the manager has admitted an operation', async () => {
    api.getUpdateStatus.mockResolvedValue(response({ operation_active: true }))
    render(<SoftwareUpdateSection />)
    await screen.findByText('Update available')

    expect(screen.getByRole('button', { name: 'Check' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Download' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Install' })).toBeDisabled()
  })

  it('does not submit duplicate download requests', async () => {
    let resolveDownload
    api.downloadUpdate.mockReturnValue(new Promise((resolve) => { resolveDownload = resolve }))
    render(<SoftwareUpdateSection />)
    const button = await screen.findByRole('button', { name: 'Download' })

    fireEvent.click(button)
    fireEvent.click(button)
    expect(api.downloadUpdate).toHaveBeenCalledTimes(1)

    resolveDownload({ status: 'queued', message: 'Download queued' })
    await screen.findByText('Download queued')
  })

  it('renders real byte progress, percentage, speed, and ETA', async () => {
    api.getUpdateStatus.mockResolvedValue(response({
      state: 'downloading',
      progress: {
        bytes_downloaded: 1048576,
        bytes_total: 2097152,
        percentage: 50,
        bytes_per_second: 524288,
        eta_seconds: 3.2,
      },
    }))
    render(<SoftwareUpdateSection />)

    expect(await screen.findByText('1.0 MB / 2.0 MB')).toBeVisible()
    expect(screen.getByText('50.0%')).toBeVisible()
    expect(screen.getByText('0.5 MB/s')).toBeVisible()
    expect(screen.getByText('ETA 4s')).toBeVisible()
  })

  it('clearly renders waiting_for_idle and disables operations', async () => {
    api.getUpdateStatus.mockResolvedValue(response({ state: 'waiting_for_idle' }))
    render(<SoftwareUpdateSection />)

    expect((await screen.findAllByText('Waiting for idle')).length).toBeGreaterThan(0)
    expect(screen.getByText('Update downloaded. Installation will start when FlowShift is idle.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Check' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Download' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Install' })).toBeDisabled()
  })

  it('explains development mode and keeps install disabled', async () => {
    api.getUpdateStatus.mockResolvedValue(response({
      state: 'downloaded',
      downloaded_asset: { version: '0.5.0' },
      development_mode: true,
      blocked_reason: 'development_checkout',
      can_install: false,
    }))
    render(<SoftwareUpdateSection />)

    expect(await screen.findByText('Development checkout')).toBeVisible()
    expect(screen.getByText(/Automatic installation unavailable in development checkout/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Install' })).toBeDisabled()
  })

  it('saves the complete validated settings object explicitly', async () => {
    render(<SoftwareUpdateSection />)
    await screen.findByText('Update available')

    fireEvent.click(screen.getByLabelText('Enable automatic updates'))
    fireEvent.click(screen.getByLabelText('Check on start'))
    fireEvent.change(screen.getByLabelText('Policy'), { target: { value: 'install' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save Update Settings' }))

    await waitFor(() => expect(api.saveUpdateSettings).toHaveBeenCalledWith({
      enabled: false,
      check_on_start: false,
      channel: 'stable',
      policy: 'install',
    }))
    expect(await screen.findByText('Update settings saved.')).toBeVisible()
  })
})
