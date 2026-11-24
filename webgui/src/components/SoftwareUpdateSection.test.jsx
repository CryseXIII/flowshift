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
  saveDevelopmentAccess: vi.fn(),
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

  it('shows channel, source and last check and stores the Forgejo access write-only', async () => {
    api.getUpdateStatus.mockResolvedValue(response({
      channel: 'development',
      state: 'error',
      source: { source: 'forgejo', label: 'Forgejo private-platform/flowshift', url: 'http://forgejo.thegoodhunter.vps/private-platform/flowshift/releases' },
      development_access: { configured: false, secure_store: true, error: null },
      last_error: { code: 'source_unreachable', message: 'raw' },
    }))
    api.saveDevelopmentAccess.mockResolvedValue({ ok: true })
    render(<SoftwareUpdateSection />)

    expect(await screen.findByTestId('update-channel')).toHaveTextContent('Development')
    expect(screen.getByTestId('update-source')).toHaveTextContent('Forgejo private-platform/flowshift')
    expect(screen.getByText('Last successful check')).toBeVisible()
    expect(screen.getByText(/needs your own network or VPN/)).toBeVisible()
    expect(screen.getByText('Forgejo access (missing)')).toBeVisible()

    fireEvent.change(screen.getByLabelText('Forgejo access token'), { target: { value: 'attrappe-zugang' } })
    const saveAccessButton = screen.getByRole('button', { name: 'Save access' })
    expect(saveAccessButton).toHaveAttribute('aria-label', 'Save access')
    expect(saveAccessButton).toHaveAttribute('title', 'Save access')
    // Drawn inline: stays visible when the Font Awesome stylesheet is missing.
    expect(saveAccessButton.querySelector('svg.button-icon[data-icon="save"]')).not.toBeNull()
    expect(saveAccessButton).toHaveTextContent('')
    expect(screen.queryByRole('button', { name: 'Remove access' })).toBeNull()
    fireEvent.click(saveAccessButton)
    await waitFor(() => expect(api.saveDevelopmentAccess).toHaveBeenCalledWith('attrappe-zugang'))
    expect(await screen.findByText('Forgejo access stored securely.')).toBeVisible()
    expect(screen.getByLabelText('Forgejo access token')).toHaveValue('')
  })

  it('explains that another installation was running and nothing was changed', async () => {
    api.getUpdateStatus.mockResolvedValue(response({
      channel: 'development',
      state: 'error',
      latest_version: '0.7.0-dev.35',
      source: { source: 'forgejo', label: 'Forgejo private-platform/flowshift (main)', url: 'http://forgejo.thegoodhunter.vps/private-platform/flowshift/src/branch/main' },
      development_access: { configured: true, secure_store: true, error: null },
      last_error: { code: 'install_busy', message: 'raw' },
    }))
    render(<SoftwareUpdateSection />)

    expect(await screen.findByText(/Another FlowShift installation or update was running, so nothing was changed/)).toBeVisible()
  })

  it('explains a missing Node.js and a held automatic install of a failed version', async () => {
    api.getUpdateStatus.mockResolvedValue(response({
      channel: 'development',
      state: 'error',
      latest_version: '0.7.0-dev.30',
      source: { source: 'forgejo', label: 'Forgejo private-platform/flowshift (main)', url: 'http://forgejo.thegoodhunter.vps/private-platform/flowshift/src/branch/main' },
      development_access: { configured: true, secure_store: true, error: null },
      last_error: { code: 'node_missing', message: 'raw' },
      automatic_install_held: true,
    }))
    render(<SoftwareUpdateSection />)

    expect(await screen.findByText(/Install Node.js LTS/)).toBeVisible()
    expect(screen.getByTestId('update-held')).toHaveTextContent('0.7.0-dev.30')
    expect(screen.getByTestId('update-source')).toHaveTextContent('(main)')
    const removeAccessButton = screen.getByRole('button', { name: 'Remove access' })
    expect(removeAccessButton).toHaveAttribute('title', 'Remove access')
    expect(removeAccessButton.querySelector('svg.button-icon[data-icon="trash"]')).not.toBeNull()
    expect(removeAccessButton).toHaveTextContent('')
  })

  it('explains a failed install in plain words and hides updater bookkeeping', async () => {
    api.getUpdateStatus.mockResolvedValue(response({
      channel: 'development',
      state: 'error',
      current_version: '0.7.0-dev.29',
      latest_version: '0.7.0-dev.30',
      last_error: { code: 'update_install_failed', message: 'raw', cause: 'program_folder_in_use' },
      last_install: {
        from_version: '0.7.0-dev.29',
        to_version: '0.7.0-dev.30',
        result: 'failed',
        finished_at: '2026-11-20T15:56:13Z',
        installed: false,
        cause: 'program_folder_in_use',
      },
      recovery_notices: [
        { code: 'update_result_present', message: 'An updater result file is present' },
        { code: 'interrupted_operation', message: 'Recovered interrupted update state: installing' },
      ],
    }))
    render(<SoftwareUpdateSection />)

    const box = await screen.findByTestId('update-last-install')
    expect(box).toHaveTextContent('Update to 0.7.0-dev.30 was not installed')
    expect(box).toHaveTextContent('program folder')
    expect(box).toHaveTextContent('FlowShift 0.7.0-dev.29 keeps running as before.')
    expect(box).toHaveTextContent('What to do:')
    expect(screen.queryByText('raw')).toBeNull()
    expect(screen.queryByText(/An updater result file is present/)).toBeNull()
    expect(screen.queryByText(/Recovered interrupted update state/)).toBeNull()
  })

  it('shows check, download and install as three steps with a plain stable error', async () => {
    api.getUpdateStatus.mockResolvedValue(response({
      state: 'error',
      latest_version: null,
      source: { source: 'github', label: 'GitHub CryseXIII/flowshift', url: 'https://github.com/CryseXIII/flowshift/releases' },
      last_error: { code: 'offline', message: 'raw', operation: 'check', at: '2026-11-24T10:00:00Z' },
    }))
    render(<SoftwareUpdateSection />)

    expect(await screen.findByText(/No internet connection/)).toBeVisible()
    const steps = screen.getByTestId('update-steps').querySelectorAll('li')
    expect([...steps].map((step) => step.dataset.status)).toEqual(['failed', 'idle', 'idle'])
    expect(steps[0]).toHaveTextContent('Check failed')
    expect(screen.queryByText('raw')).toBeNull()
    expect(screen.queryByText(/Forgejo/)).toBeNull()
  })

  it('reports the result of a clicked check once it has finished', async () => {
    api.getUpdateStatus
      .mockResolvedValueOnce(response({ state: 'up_to_date', latest_version: '0.4.0', last_check_at: '2026-07-20T08:00:00Z' }))
      .mockResolvedValue(response())
    render(<SoftwareUpdateSection />)
    fireEvent.click(await screen.findByRole('button', { name: 'Check' }))

    expect(await screen.findByText('Check finished: FlowShift 0.5.0 is available. Next step: Download.')).toBeVisible()
    const steps = screen.getByTestId('update-steps').querySelectorAll('li')
    expect([...steps].map((step) => step.dataset.status)).toEqual(['done', 'next', 'idle'])
    expect(steps[0]).toHaveTextContent('Found 0.5.0')
  })

  it('reports a failed download with what to do', async () => {
    api.getUpdateStatus
      .mockResolvedValueOnce(response())
      .mockResolvedValue(response({
        state: 'error',
        last_error: { code: 'hash_mismatch', message: 'raw', operation: 'download', at: '2026-11-24T10:01:00Z' },
      }))
    render(<SoftwareUpdateSection />)
    fireEvent.click(await screen.findByRole('button', { name: 'Download' }))

    expect(await screen.findByText(/^Download failed: The downloaded installer did not match the published checksum/)).toBeVisible()
    expect(screen.getByTestId('update-steps').querySelectorAll('li')[1].dataset.status).toBe('failed')
  })

  it('says that FlowShift restarts after a clicked install', async () => {
    const ready = { state: 'downloaded', downloaded_asset: { version: '0.5.0' }, can_install: true, blocked_reason: null }
    api.getUpdateStatus
      .mockResolvedValueOnce(response(ready))
      .mockResolvedValueOnce(response({ ...ready, state: 'installing', can_install: false }))
      .mockRejectedValue(new Error('Failed to fetch'))
    render(<SoftwareUpdateSection />)
    fireEvent.click(await screen.findByRole('button', { name: 'Install' }))

    expect(await screen.findByText(/^Installing FlowShift 0.5.0: FlowShift closes now/)).toBeVisible()
    expect(await screen.findByText('FlowShift is restarting for the update. This page reconnects by itself.', {}, { timeout: 3000 })).toBeVisible()
  })

  it('shows no install failure once the new version runs', async () => {
    api.getUpdateStatus.mockResolvedValue(response({
      state: 'up_to_date',
      current_version: '0.7.0-dev.31',
      latest_version: '0.7.0-dev.31',
      last_install: {
        from_version: '0.7.0-dev.29',
        to_version: '0.7.0-dev.31',
        result: 'success',
        finished_at: '2026-11-21T10:00:00Z',
        installed: true,
        cause: null,
      },
    }))
    render(<SoftwareUpdateSection />)

    expect(await screen.findByText('Up to date')).toBeVisible()
    expect(screen.queryByTestId('update-last-install')).toBeNull()
    expect(screen.getByTestId('update-last-install-ok')).toHaveTextContent('FlowShift 0.7.0-dev.31 was installed')
  })
})
