import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import PeersPanel, { EMPTY_SCAN_HINT, reachText } from './PeersPanel.jsx'
import * as api from '../api.js'

vi.mock('../api.js', () => ({
  getSettings: vi.fn(),
  getPeers: vi.fn(),
  scanNetwork: vi.fn(),
  addPeer: vi.fn(),
  removePeer: vi.fn(),
  editPeer: vi.fn(),
  pingPeer: vi.fn(),
  acceptPairRequest: vi.fn(),
  dismissPairRequest: vi.fn(),
}))

const THIS_PC = {
  name: 'laptop', device_id: 'aaaa1111', port: 45781,
  addresses: [{ address: '192.168.0.50', kind: 'local' }, { address: '100.101.1.1', kind: 'tailscale' }],
  tailscale_name: 'laptop.tail1234.ts.net',
}

const VPN_HINT = 'No FlowShift PC answered. Over a VPN this is normal: the search call reaches only PCs in the same local network, WireGuard and Tailscale do not pass it on. FlowShift also asked 1 Tailscale device(s) directly, but none answered. Enter the other PC\'s address below: its IP address or name (with Tailscale the 100.x.y.z address from the Tailscale app). The port is 45781.'

describe('PeersPanel over a VPN', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getSettings.mockResolvedValue({ peers: [{ name: 'pc2', host: '100.101.1.2', port: 45781 }] })
    api.getPeers.mockResolvedValue({ ok: true, peers: [], pair_requests: [], this_pc: THIS_PC })
    api.addPeer.mockResolvedValue({ ok: true })
  })

  it('explains an empty scan and offers the address field with port 45781', async () => {
    api.scanNetwork.mockResolvedValue({ peers: [], hint: VPN_HINT, this_pc: THIS_PC })
    render(<PeersPanel status={{}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Scan Network/ }))
    expect(await screen.findByTestId('scan-hint')).toHaveTextContent('Over a VPN this is normal')
    expect(screen.getByTestId('scan-hint')).toHaveTextContent('asked 1 Tailscale device(s)')
    expect(screen.getByLabelText('Address of the other PC')).toHaveAttribute('placeholder', 'IP address or name *')
    expect(screen.getByLabelText('Port')).toHaveValue(45781)
  })

  it('uses its own hint when an older runtime sends none', async () => {
    api.scanNetwork.mockResolvedValue({ peers: [] })
    render(<PeersPanel status={{}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Scan Network/ }))
    expect(await screen.findByTestId('scan-hint')).toHaveTextContent(EMPTY_SCAN_HINT)
    expect(EMPTY_SCAN_HINT).toMatch(/WireGuard and Tailscale do not pass it on/)
  })

  it('lists a device found over Tailscale like a broadcast one', async () => {
    api.scanNetwork.mockResolvedValue({ peers: [{ name: 'pc3', host: '100.101.1.3', port: 45781, device_id: 'cccc3333', via: 'tailscale' }] })
    render(<PeersPanel status={{}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Scan Network/ }))
    expect(await screen.findByText('pc3')).toBeInTheDocument()
    expect(screen.getByText('Tailscale')).toBeInTheDocument()
    expect(screen.queryByTestId('scan-hint')).toBeNull()
  })

  it('shows this PC with address, port and device ID and says both PCs need the entry', async () => {
    render(<PeersPanel status={{}} />)
    const box = await screen.findByTestId('this-pc')
    expect(box).toHaveTextContent('100.101.1.1')
    expect(box).toHaveTextContent('aaaa1111')
    expect(box).toHaveTextContent('45781')
    fireEvent.click(screen.getByRole('button', { name: /Add Peer/ }))
    fireEvent.change(screen.getByLabelText('Address of the other PC'), { target: { value: 'pc2.tail1234.ts.net' } })
    fireEvent.click(screen.getByRole('button', { name: /Save/ }))
    await waitFor(() => expect(api.addPeer).toHaveBeenCalled())
    expect(api.addPeer.mock.calls[0][0]).toMatchObject({ host: 'pc2.tail1234.ts.net', port: 45781 })
    expect(await screen.findByTestId('peer-added-note')).toHaveTextContent('Both PCs need each other in their list')
  })

  it('adds the PC that added this PC only after the click', async () => {
    api.getPeers.mockResolvedValue({
      ok: true, peers: [], this_pc: THIS_PC,
      pair_requests: [{ key: 'device:bbbb2222', name: 'pc1', host: '100.101.1.9', port: 45781, device_id: 'bbbb2222' }],
    })
    api.acceptPairRequest.mockResolvedValue({ ok: true })
    render(<PeersPanel status={{}} />)
    expect(await screen.findByTestId('pair-request')).toHaveTextContent('pc1 (100.101.1.9) has added this PC')
    expect(api.acceptPairRequest).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /Add here too/ }))
    await waitFor(() => expect(api.acceptPairRequest).toHaveBeenCalledWith('device:bbbb2222'))
  })

  it('names the likely cause of a failed ping', async () => {
    const error = new Error('ping failed: timed out')
    error.detail = 'No answer from 100.101.1.2 on port 45781. Most likely Windows Firewall on 100.101.1.2 blocks FlowShift.'
    api.pingPeer.mockRejectedValue(error)
    render(<PeersPanel status={{}} />)
    fireEvent.click(await screen.findByRole('button', { name: /Ping/ }))
    expect(await screen.findByTestId('ping-hint')).toHaveTextContent('Ping to pc2 failed. No answer from 100.101.1.2 on port 45781. Most likely Windows Firewall')
  })
})

describe('PeersPanel connection column', () => {
  const failing = {
    identity: 'device:98194f67', connected: false,
    reach: {
      last_ok: { host: '100.101.7.9', port: 45781, via: 'tailscale', at: 1764230400 },
      failing_since: 1764234000,
      failure: { at: 1764234060, text: '192.168.8.18 (LAN): no answer (timeout): the PC is off.', tried: [] },
    },
  }

  beforeEach(() => {
    vi.clearAllMocks()
    api.getSettings.mockResolvedValue({ peers: [{ name: 'Laptop-Bettina', host: '192.168.8.18', port: 45781, identity: 'device:98194f67' }] })
    api.getPeers.mockResolvedValue({ ok: true, peers: [], pair_requests: [] })
  })

  it('says since when and why a peer is not connected, and the last way that worked', async () => {
    render(<PeersPanel status={{ peers: [failing] }} />)
    const cell = await screen.findByTestId('peer-reach')
    expect(cell).toHaveTextContent('Not connected since')
    expect(cell).toHaveTextContent('192.168.8.18 (LAN): no answer (timeout)')
    expect(cell).toHaveTextContent('via 100.101.7.9:45781 (Tailscale)')
  })

  it('names the address of a connected peer', () => {
    expect(reachText({ connected: true, remote: ['100.101.7.9', 45781], reach: {} })).toBe('Connected via 100.101.7.9:45781.')
    expect(reachText({ connected: false, reach: {} })).toBe('Not connected.')
    expect(reachText(null)).toBe('')
  })
})
