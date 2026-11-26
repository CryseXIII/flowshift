"""Finding the other FlowShift PC over WireGuard and Tailscale (Viktor 26.11.).

Broadcasts do not pass a VPN tunnel. These tests prove the direct (unicast)
part of the scan: which addresses are asked (Tailscale devices, VPN networks),
that a real ``discover`` reply to a unicast call shows up like a broadcast
reply, the plain hint text of an empty scan, the likely cause of a failed
ping, and the "add this PC here too" requests after the other PC dialed in.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config_schema
import peer_discovery as pd
import web_api


TAILSCALE_STATUS = {
    "Self": {"HostName": "laptop", "DNSName": "laptop.tail1234.ts.net.",
             "TailscaleIPs": ["100.101.1.1", "fd7a:115c:a1e0::1"], "Online": True},
    "Peer": {
        "nodekey:a": {"HostName": "pc2", "DNSName": "pc2.tail1234.ts.net.",
                      "TailscaleIPs": ["fd7a:115c:a1e0::2", "100.101.1.2"], "Online": True},
        "nodekey:b": {"HostName": "phone", "TailscaleIPs": ["100.101.1.3"], "Online": False},
        "nodekey:c": {"HostName": "nas", "TailscaleIPs": ["100.101.1.4"]},
        "nodekey:d": {"HostName": "v6only", "TailscaleIPs": ["fd7a:115c:a1e0::5"], "Online": True},
    },
}

# Windows view of a laptop with WireGuard into the home network and Tailscale.
INTERFACES = {
    "addresses": [
        {"IPAddress": "192.168.0.50", "PrefixLength": 24, "InterfaceAlias": "WLAN", "InterfaceIndex": 7},
        {"IPAddress": "10.6.0.2", "PrefixLength": 24, "InterfaceAlias": "Heimnetz", "InterfaceIndex": 21},
        {"IPAddress": "100.101.1.1", "PrefixLength": 32, "InterfaceAlias": "Tailscale", "InterfaceIndex": 30},
    ],
    "routes": [
        {"DestinationPrefix": "0.0.0.0/0", "InterfaceIndex": 7},
        {"DestinationPrefix": "192.168.0.0/24", "InterfaceIndex": 7},
        {"DestinationPrefix": "10.6.0.0/24", "InterfaceIndex": 21},
        {"DestinationPrefix": "192.168.178.0/24", "InterfaceIndex": 21},
        {"DestinationPrefix": "10.6.0.1/32", "InterfaceIndex": 21},
        {"DestinationPrefix": "100.64.0.0/10", "InterfaceIndex": 30},
        {"DestinationPrefix": "224.0.0.0/4", "InterfaceIndex": 21},
    ],
    "adapters": [
        {"ifIndex": 7, "InterfaceDescription": "Intel(R) Wi-Fi 6 AX201"},
        {"ifIndex": 21, "InterfaceDescription": "WireGuard Tunnel"},
        {"ifIndex": 30, "InterfaceDescription": "Tailscale Tunnel"},
    ],
}


class Proc:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class TailscaleTests(unittest.TestCase):
    def test_reads_status_only_when_the_command_exists(self):
        calls = []

        def run(*args, **kwargs):
            calls.append(args[0])
            return Proc(json.dumps(TAILSCALE_STATUS))

        missing = pd.read_tailscale_status(run=run, which=lambda name: None,
                                           exists=lambda path: False, environ={})
        self.assertIsNone(missing)
        self.assertEqual(calls, [])
        status = pd.read_tailscale_status(run=run, which=lambda name: None,
                                          exists=lambda path: path.endswith("tailscale.exe"),
                                          environ={"ProgramFiles": r"C:\Program Files"})
        self.assertEqual(status["Self"]["HostName"], "laptop")
        self.assertEqual(calls[0][1:], ["status", "--json"])
        self.assertTrue(calls[0][0].endswith("tailscale.exe"))

    def test_stopped_or_broken_tailscale_gives_nothing(self):
        which = lambda name: "/usr/bin/tailscale"
        self.assertIsNone(pd.read_tailscale_status(run=lambda *a, **k: Proc("", 1), which=which))
        self.assertIsNone(pd.read_tailscale_status(run=lambda *a, **k: Proc("not json"), which=which))

        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("tailscale", 4)
        self.assertIsNone(pd.read_tailscale_status(run=timeout, which=which))

    def test_targets_are_the_online_peers_ipv4(self):
        self.assertEqual(pd.tailscale_targets(TAILSCALE_STATUS), [
            {"host": "100.101.1.2", "name": "pc2"},
            {"host": "100.101.1.4", "name": "nas"},
        ])
        self.assertEqual(pd.tailscale_self(TAILSCALE_STATUS),
                         {"address": "100.101.1.1", "dns_name": "laptop.tail1234.ts.net"})
        self.assertEqual(pd.tailscale_targets({"Peer": "bad"}), [])
        self.assertEqual(pd.tailscale_targets(None), [])


class VpnTargetTests(unittest.TestCase):
    def test_walks_the_wireguard_networks_but_not_lan_default_or_cgnat(self):
        hosts, names = pd.vpn_targets(INTERFACES)
        self.assertEqual(names, ["Heimnetz"])  # Tailscale gave no address to walk
        self.assertIn("10.6.0.1", hosts)
        self.assertIn("10.6.0.254", hosts)
        self.assertIn("192.168.178.20", hosts)
        self.assertNotIn("10.6.0.2", hosts)  # own address
        self.assertFalse([h for h in hosts if h.startswith("192.168.0.")])  # plain LAN: broadcast
        self.assertFalse([h for h in hosts if h.startswith("224.") or h.startswith("100.")])
        self.assertEqual(len(hosts), 253 + 254)
        self.assertEqual(len(set(hosts)), len(hosts))

    def test_large_vpn_network_is_cut_to_the_own_slash_24(self):
        hosts, _ = pd.vpn_targets({"addresses": [
            {"IPAddress": "10.8.3.7", "PrefixLength": 16, "InterfaceAlias": "OpenVPN TAP", "InterfaceIndex": 3}]})
        self.assertEqual(hosts[0], "10.8.3.1")
        self.assertEqual(len(hosts), 253)
        self.assertTrue(all(h.startswith("10.8.3.") for h in hosts))

    def test_limit_and_garbage(self):
        hosts, _ = pd.vpn_targets(INTERFACES, limit=10)
        self.assertEqual(len(hosts), 10)
        self.assertEqual(pd.vpn_targets(None), ([], []))
        self.assertEqual(pd.vpn_targets({"addresses": {"IPAddress": "x", "InterfaceAlias": "wg0"}}),
                         ([], []))

    def test_interfaces_are_read_only_on_windows(self):
        self.assertIsNone(pd.read_interfaces(run=lambda *a, **k: self.fail("ran"), platform="posix"))
        got = pd.read_interfaces(run=lambda *a, **k: Proc(json.dumps(INTERFACES)), platform="nt")
        self.assertEqual(got["adapters"][1]["InterfaceDescription"], "WireGuard Tunnel")

    def test_collect_puts_tailscale_first_and_counts_what_was_asked(self):
        targets, summary = pd.collect_unicast_targets(read_tailscale=lambda: TAILSCALE_STATUS,
                                                      read_ifaces=lambda: INTERFACES)
        self.assertEqual(targets[:2], [
            {"host": "100.101.1.2", "via": "tailscale", "name": "pc2"},
            {"host": "100.101.1.4", "via": "tailscale", "name": "nas"}])
        self.assertEqual(targets[2]["via"], "vpn")
        self.assertEqual(summary["tailscale"]["asked"], 2)
        self.assertTrue(summary["tailscale"]["available"])
        self.assertEqual(summary["tailscale"]["self"]["address"], "100.101.1.1")
        self.assertEqual(summary["vpn"]["asked"], 253 + 254)
        self.assertEqual(summary["vpn"]["interfaces"], ["Heimnetz"])

        targets, summary = pd.collect_unicast_targets(read_tailscale=lambda: None,
                                                      read_ifaces=lambda: None)
        self.assertEqual(targets, [])
        self.assertFalse(summary["tailscale"]["available"])


class UnicastScanTests(unittest.TestCase):
    """A real ``discover`` sent to one address, answered like tray.discovery_thread does."""

    def setUp(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.received = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.answer, daemon=True)
        self.thread.start()
        web_api._refs = {"get_local_ipv4s": lambda: []}

    def tearDown(self):
        self.stop.set()
        self.thread.join(2)
        self.listener.close()
        web_api._refs = {}

    def answer(self):
        try:
            while not self.stop.is_set():
                try:
                    data, addr = self.listener.recvfrom(4096)
                except socket.timeout:
                    continue
                request = json.loads(data.decode("utf-8"))
                self.received.append(request)
                if request.get("type") != "discover":
                    continue
                reply = {"type": "discover_reply", "device_id": "BBBB2222",
                         "display_name": "pc2", "port": 45781}
                self.listener.sendto(json.dumps(reply).encode("utf-8"), addr)
        except OSError:
            return

    def test_unicast_reply_is_listed_like_a_broadcast_reply(self):
        found = web_api._scan_broadcast(self.port, 1.0, "aaaa1111",
                                        [{"host": "127.0.0.1", "via": "tailscale"},
                                         {"host": "127.0.0.1", "via": "vpn"}])
        self.assertEqual(found, [{"name": "pc2", "host": "127.0.0.1", "port": 45781,
                                  "device_id": "bbbb2222", "adapter": "direct",
                                  "via": "tailscale"}])
        # Asked once, with the request of the broadcast scan.
        self.assertEqual(self.received, [{"type": "discover", "reply_port": self.port}])

    def test_own_reply_is_left_out(self):
        found = web_api._scan_broadcast(self.port, 0.5, "bbbb2222", [{"host": "127.0.0.1"}])
        self.assertEqual(found, [])

    def test_without_unicast_targets_nothing_is_sent_to_the_address(self):
        found = web_api._scan_broadcast(self.port, 0.3, "aaaa1111")
        self.assertEqual(found, [])
        self.assertEqual(self.received, [])


class HintTextTests(unittest.TestCase):
    def test_no_hint_when_something_was_found(self):
        self.assertIsNone(pd.scan_hint([{"host": "x"}], {}))

    def test_empty_scan_explains_vpn_and_offers_the_address(self):
        text = pd.scan_hint([], {"tailscale": {"available": False, "asked": 0},
                                 "vpn": {"interfaces": [], "asked": 0}})
        self.assertIn("Over a VPN this is normal", text)
        self.assertIn("WireGuard and Tailscale do not pass it on", text)
        self.assertIn("Enter the other PC's address below: its IP address or name", text)
        self.assertIn("The port is 45781.", text)
        self.assertNotIn("asked", text)

    def test_empty_scan_names_what_was_asked_directly(self):
        text = pd.scan_hint([], {"tailscale": {"available": True, "asked": 2},
                                 "vpn": {"interfaces": ["Heimnetz"], "asked": 253}}, 45790)
        self.assertIn("FlowShift also asked 2 Tailscale device(s) and 253 address(es) in the "
                      "VPN network (Heimnetz) directly, but none answered", text)
        self.assertIn("Windows Firewall on that PC blocks port 45790", text)
        self.assertIn("The port is 45790.", text)


class PingHintTests(unittest.TestCase):
    def test_timeout_names_the_firewall_and_the_public_profile(self):
        text = pd.ping_failure_hint(socket.timeout("timed out"), "100.101.1.2", 45781)
        self.assertIn("No answer from 100.101.1.2 on port 45781", text)
        self.assertIn("Windows Firewall", text)
        self.assertIn("'Public' network", text)
        self.assertIn("port 45781 for UDP and TCP for all network profiles", text)
        self.assertIn("In Tailscale, the access rules", text)
        lan = pd.ping_failure_hint(TimeoutError(), "192.168.178.20", 45781)
        self.assertIn("Windows Firewall", lan)
        self.assertNotIn("Tailscale, the access rules", lan)

    def test_other_causes(self):
        self.assertIn("FlowShift does not run there",
                      pd.ping_failure_hint(ConnectionRefusedError(), "10.6.0.5", 45781))
        self.assertIn("is unknown on this PC",
                      pd.ping_failure_hint(socket.gaierror(11001, "getaddrinfo failed"), "pc2", 45781))
        unreachable = OSError(10065, "unreachable")
        self.assertIn("the VPN (WireGuard or Tailscale) is not connected here",
                      pd.ping_failure_hint(unreachable, "192.168.178.20", 45781))
        self.assertIn("not as FlowShift",
                      pd.ping_failure_hint(ValueError("unexpected reply: hello"), "10.6.0.5", 45781))
        self.assertIn("Windows Firewall", pd.ping_failure_hint(RuntimeError("x"), None, None))


class PairRequestTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.requests = pd.PairRequests(clock=lambda: self.now[0])

    def test_unknown_pc_is_offered_with_its_source_address(self):
        got = self.requests.note("AAAA1111", "pc1", "100.101.1.1", 45781, [])
        self.assertEqual(got["key"], "device:aaaa1111")
        self.assertEqual(self.requests.pending([]), [
            {"key": "device:aaaa1111", "name": "pc1", "host": "100.101.1.1", "port": 45781,
             "device_id": "aaaa1111", "seen": 1000.0}])

    def test_known_or_invalid_pc_is_not_offered(self):
        peers = [{"name": "pc1", "host": "10.0.0.1", "device_id": "aaaa1111"}]
        self.assertIsNone(self.requests.note("aaaa1111", "pc1", "100.101.1.1", 45781, peers))
        self.assertIsNone(self.requests.note("", "pc1", "10.0.0.1", 45781, peers))
        self.assertIsNone(self.requests.note("bad id!", "x", "10.0.0.2", 45781, []))
        self.assertIsNone(self.requests.note("cccc", "x", "not-an-ip", 45781, []))
        self.assertEqual(self.requests.pending([]), [])

    def test_untrusted_fields_are_bounded_and_requests_expire(self):
        got = self.requests.note("dddd", "x\x00" * 100, "10.0.0.4", 99999, [])
        self.assertEqual(len(got["name"]), 64)
        self.assertNotIn("\x00", got["name"])
        self.assertEqual(got["port"], 45781)
        for i in range(30):
            self.requests.note(f"e{i}", "x", f"10.0.1.{i + 1}", 45781, [])
        self.assertEqual(len(self.requests.pending([])), pd.MAX_PAIR_REQUESTS)
        self.now[0] += pd.PAIR_REQUEST_TTL + 1
        self.assertEqual(self.requests.pending([]), [])

    def test_added_or_dismissed_pc_disappears(self):
        self.requests.note("aaaa1111", "pc1", "100.101.1.1", 45781, [])
        self.assertEqual(self.requests.pending([{"device_id": "aaaa1111"}]), [])
        self.assertTrue(self.requests.dismiss("device:aaaa1111"))
        self.assertIsNone(self.requests.note("aaaa1111", "pc1", "100.101.1.1", 45781, []))


class FakeState:
    def __init__(self, config):
        self.lock = threading.RLock()
        self.config = config
        self.active_peer = None
        self.peers = {}


class PeersApiTests(unittest.TestCase):
    """The Peers page's routes: scan with hint, ping with cause, add on both PCs."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temporary.name) / "config.json"
        config_schema.save_config(self.config_path, {
            "device_name": "laptop", "device_id": "aaaa1111", "port": 45781,
            "peers": [{"name": "pc2", "host": "100.101.1.2", "port": 45781}]})
        self.state = FakeState(config_schema.load_config(self.config_path))
        self.requests = pd.PairRequests()
        self.ping_error = None
        self.scans = []

        def ping(ref):
            raise self.ping_error

        web_api._refs = {}
        web_api.init(istate=self.state,
                     save_config=lambda c: config_schema.save_config(self.config_path, c),
                     ping_peer=ping, pair_requests=self.requests,
                     get_local_ipv4s=lambda: ["192.168.0.50", "100.101.1.1"])
        self.originals = (web_api._scan_broadcast, web_api._unicast_targets)
        web_api._unicast_targets = lambda: ([{"host": "100.101.1.2", "via": "tailscale"}],
                                            {"tailscale": {"available": True, "asked": 1,
                                                           "self": {"address": "100.101.1.1",
                                                                    "dns_name": "laptop.tail1234.ts.net"}},
                                             "vpn": {"interfaces": [], "asked": 0}})

        def scan(port, timeout, local_id, targets=(), searched=None):
            self.scans.append((port, local_id, list(targets)))
            return []
        web_api._scan_broadcast = scan
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        web_api._scan_broadcast, web_api._unicast_targets = self.originals
        web_api._refs = {}
        web_api._last_tailscale_self.clear()
        self.temporary.cleanup()

    def request(self, method, path, body=None):
        port = self.server.server_address[1]
        connection = HTTPConnection("127.0.0.1", port, timeout=3)
        headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}
        connection.request(method, path, body=json.dumps(body) if body is not None else None,
                           headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_empty_scan_asks_tailscale_and_returns_hint_and_own_address(self):
        status, body = self.request("POST", "/api/peers/scan", {"timeout": 0.2})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.scans, [(45781, "aaaa1111", [{"host": "100.101.1.2", "via": "tailscale"}])])
        self.assertEqual(body["peers"], [])
        self.assertIn("Over a VPN this is normal", body["hint"])
        self.assertIn("asked 1 Tailscale device(s)", body["hint"])
        self.assertEqual(body["this_pc"]["device_id"], "aaaa1111")
        self.assertEqual(body["this_pc"]["port"], 45781)
        self.assertEqual(body["this_pc"]["addresses"], [
            {"address": "192.168.0.50", "kind": "local"},
            {"address": "100.101.1.1", "kind": "tailscale"}])
        self.assertEqual(body["this_pc"]["tailscale_name"], "laptop.tail1234.ts.net")

    def test_failed_ping_names_the_likely_cause(self):
        error = TimeoutError("timed out")
        error.ping_target = ("100.101.1.2", 45781)
        self.ping_error = error
        status, body = self.request("POST", "/api/peers/ping", {"peer": "pc2"})
        self.assertEqual(status, 502)
        self.assertEqual(body["error"], "ping failed: timed out")
        self.assertIn("Windows Firewall on 100.101.1.2", body["message"])
        self.assertIn("'Public' network", body["message"])
        self.ping_error = ValueError("unknown peer: x")
        status, body = self.request("POST", "/api/peers/ping", {"peer": "x"})
        self.assertEqual((status, body), (400, {"error": "unknown peer: x"}))

    def test_pair_request_is_listed_and_added_only_on_accept(self):
        self.requests.note("cccc3333", "pc3", "100.101.1.3", 45781, self.state.config["peers"])
        status, body = self.request("GET", "/api/peers")
        self.assertEqual(status, 200, body)
        self.assertEqual([r["key"] for r in body["pair_requests"]], ["device:cccc3333"])
        self.assertEqual(len(self.state.config["peers"]), 1)
        status, body = self.request("POST", "/api/peers/requests/accept", {"key": "device:cccc3333"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["peer"], {"name": "pc3", "host": "100.101.1.3", "port": 45781,
                                        "device_id": "cccc3333"})
        stored = json.loads(self.config_path.read_text("utf-8"))["peers"]
        self.assertEqual([p["host"] for p in stored], ["100.101.1.2", "100.101.1.3"])
        status, body = self.request("GET", "/api/peers")
        self.assertEqual(body["pair_requests"], [])
        status, body = self.request("POST", "/api/peers/requests/accept", {"key": "device:cccc3333"})
        self.assertEqual(status, 404)

    def test_dismissed_request_adds_nothing(self):
        self.requests.note("dddd4444", "pc4", "100.101.1.4", 45781, [])
        status, body = self.request("POST", "/api/peers/requests/dismiss", {"key": "device:dddd4444"})
        self.assertEqual((status, body["dismissed"]), (200, True))
        self.assertEqual(len(json.loads(self.config_path.read_text("utf-8"))["peers"]), 1)


if __name__ == "__main__":
    unittest.main()
