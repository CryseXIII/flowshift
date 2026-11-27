"""Finding a paired PC again under any of its addresses (Viktor 27.11.).

Pure part: ``peer_reach`` (remembered addresses, order of tries, Tailscale
match by name, summarized failure lines, the Peers page sentence). Productive
part: ``tray.connect_one`` with a fake socket: the LAN address times out, the
connector then tries the Tailscale address of the PC with the same name,
connects, hands the link to ``peer_handler`` with the entry's address and
stores the working address in the peer entry.
"""
from __future__ import annotations

import ctypes
import os
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import peer_reach as pr

_MISSING = object()

BETTINA_LAN = "192.168.8.18"
BETTINA_TS = "100.101.7.9"
PEER = {"name": "Laptop-Bettina", "host": BETTINA_LAN, "port": 45781, "device_id": "98194f67"}


def tailscale_status(online=True, host_name="Laptop-Bettina"):
    return {"BackendState": "Running",
            "Self": {"TailscaleIPs": ["100.90.1.1"], "DNSName": "book-viktor.tail1.ts.net."},
            "Peer": {"node1": {"HostName": host_name, "DNSName": "laptop-bettina.tail1.ts.net.",
                               "TailscaleIPs": [BETTINA_TS, "fd7a::1"], "Online": online},
                     "node2": {"HostName": "phone", "TailscaleIPs": ["100.70.0.2"], "Online": True}}}


class AddressTests(unittest.TestCase):
    def test_classify_tailscale_addresses_and_names(self):
        self.assertEqual(pr.classify_via("100.101.7.9"), pr.VIA_TAILSCALE)
        self.assertEqual(pr.classify_via("laptop-bettina.tail1.ts.net"), pr.VIA_TAILSCALE)
        self.assertEqual(pr.classify_via("192.168.8.18"), pr.VIA_LAN)
        self.assertEqual(pr.classify_via("100.200.0.1"), pr.VIA_LAN)  # outside 100.64/10

    def test_remember_address_keeps_newest_first_without_duplicates(self):
        peer = dict(PEER)
        self.assertTrue(pr.remember_address(peer, BETTINA_LAN, 45781, now=100))
        self.assertFalse(pr.remember_address(peer, BETTINA_LAN, 45781, now=200))
        self.assertTrue(pr.remember_address(peer, BETTINA_TS, 45781, now=300))
        rows = pr.known_addresses(peer)
        self.assertEqual([(r["host"], r["via"], r["last_ok"]) for r in rows],
                         [(BETTINA_TS, "tailscale", 300), (BETTINA_LAN, "lan", 200)])

    def test_remember_address_is_bounded(self):
        peer = dict(PEER)
        for n in range(pr.MAX_ADDRESSES + 4):
            pr.remember_address(peer, f"10.0.0.{n + 1}", 45781, now=n)
        self.assertEqual(len(peer["addresses"]), pr.MAX_ADDRESSES)
        self.assertEqual(peer["addresses"][0]["host"], f"10.0.0.{pr.MAX_ADDRESSES + 4}")

    def test_invalid_remembered_rows_are_ignored(self):
        peer = dict(PEER, addresses=[{"host": "bad host"}, "x", {"host": BETTINA_TS, "port": "nan"}])
        self.assertEqual([(r["host"], r["port"]) for r in pr.known_addresses(peer)],
                         [(BETTINA_TS, 45781)])

    def test_candidates_last_working_first_then_entry_then_tailscale(self):
        peer = dict(PEER)
        pr.remember_address(peer, "10.108.223.40", 45781, via=pr.VIA_VPN, now=50)
        rows = pr.candidate_addresses(peer, tailscale_status())
        self.assertEqual([(r["host"], r["source"]) for r in rows],
                         [("10.108.223.40", pr.SOURCE_LAST_OK), (BETTINA_LAN, pr.SOURCE_CONFIGURED),
                          (BETTINA_TS, pr.SOURCE_TAILSCALE)])
        self.assertEqual(rows[0]["via"], pr.VIA_VPN)

    def test_tailscale_match_by_name_any_case_and_offline_skipped(self):
        self.assertEqual(pr.tailscale_address_for(dict(PEER, name="LAPTOP-BETTINA"),
                                                  tailscale_status())["host"], BETTINA_TS)
        self.assertIsNone(pr.tailscale_address_for(PEER, tailscale_status(online=False)))
        self.assertIsNone(pr.tailscale_address_for(dict(PEER, name="Other"), tailscale_status(
            host_name="nobody")))
        self.assertIsNone(pr.tailscale_address_for(PEER, None))


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.tracker = pr.ReachTracker(clock=lambda: self.now[0])
        self.lan = {"host": BETTINA_LAN, "port": 45781, "via": "lan"}
        self.ts = {"host": BETTINA_TS, "port": 45781, "via": "tailscale"}

    def test_a_night_of_timeouts_gives_a_few_lines_not_7597(self):
        lines = []
        for _ in range(7597):  # BOOK-Viktor 26.11. 18:31 to 27.11. 11:35, every 8 s
            lines += self.tracker.attempt_failed("device:98194f67", "Laptop-Bettina", self.lan,
                                                 socket.timeout("timed out"))
            self.now[0] += 8.0
        self.assertIn("connect to 192.168.8.18:45781 (LAN) failed: no answer (timeout)", lines[0][1])
        self.assertLessEqual(len(lines), 1 + int(7597 * 8.0 / pr.SUMMARY_INTERVAL) + 1)
        self.assertGreater(len(lines), 50)
        self.assertIn("still failing", lines[-1][1])

    def test_reason_change_logs_again_and_success_names_the_way(self):
        token = "device:98194f67"
        first = self.tracker.attempt_failed(token, "B", self.lan, socket.timeout())
        self.assertEqual(len(first), 1)
        self.assertEqual(self.tracker.attempt_failed(token, "B", self.lan, socket.timeout()), [])
        refused = self.tracker.attempt_failed(token, "B", self.lan, ConnectionRefusedError())
        self.assertEqual(len(refused), 2)  # summary of the timeouts, then the new reason
        self.assertIn("FlowShift does not listen", refused[1][1])
        self.tracker.round_failed(token, "B", [self.lan])
        self.assertIn("Not connected since", self.tracker.sentence(token))
        done = self.tracker.connected(token, "B", self.ts)
        self.assertIn("connected via 100.101.7.9:45781 (Tailscale) after 3 failed attempt(s)",
                      done[0][1])
        snap = self.tracker.snapshot(token)
        self.assertEqual(snap["last_ok"]["host"], BETTINA_TS)
        self.assertIsNone(snap["failure"])
        self.assertEqual(self.tracker.sentence(token), "")

    def test_round_sentence_names_every_address_and_reason(self):
        token = "device:x"
        self.tracker.attempt_failed(token, "B", self.lan, socket.timeout())
        self.tracker.attempt_failed(token, "B", self.ts, OSError(10065, "unreachable"))
        self.tracker.round_failed(token, "B", [self.lan, self.ts])
        text = self.tracker.snapshot(token)["failure"]["text"]
        self.assertIn("192.168.8.18 (LAN): no answer (timeout)", text)
        self.assertIn("100.101.7.9 (Tailscale): no way to this address", text)


def _import_tray():
    if "tray" in sys.modules or sys.platform == "win32":
        import tray
        return tray
    saved = {name: getattr(ctypes, name, _MISSING) for name in ("windll", "WINFUNCTYPE")}
    ctypes.windll = mock.MagicMock(name="windll")
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE
    try:
        import tray
    finally:
        for name, value in saved.items():
            if value is _MISSING:
                delattr(ctypes, name)
            else:
                setattr(ctypes, name, value)
    return tray


class _FakeSocket:
    """``socket.socket`` stand-in: the LAN address times out, the others connect."""
    attempts = []

    def __init__(self, *_args):
        self.closed = False

    def settimeout(self, _value):
        pass

    def setsockopt(self, *_args):
        pass

    def connect(self, address):
        _FakeSocket.attempts.append(address)
        if address[0] == BETTINA_LAN:
            raise socket.timeout("timed out")

    def close(self):
        self.closed = True


class TrayConnectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="fs-reach-", ignore_cleanup_errors=True)
        cls.old_env = {k: os.environ.get(k) for k in ("FLOWSHIFT_LOG_DIR", "FLOWSHIFT_CONFIG")}
        os.environ["FLOWSHIFT_LOG_DIR"] = cls.tmp.name
        os.environ["FLOWSHIFT_CONFIG"] = os.path.join(cls.tmp.name, "config.json")
        cls.tray = _import_tray()

    @classmethod
    def tearDownClass(cls):
        for key, value in cls.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.tmp.cleanup()

    def setUp(self):
        tray = self.tray
        saved = tray.istate.config
        self.addCleanup(setattr, tray.istate, "config", saved)
        self.logs = []
        self.handled = []
        self.stop = threading.Event()

        def fake_handler(conn, addr, is_server, dial_host=None, dial_port=None,
                         stop_event=None, entry_address=None):
            self.handled.append({"addr": addr, "dial_host": dial_host, "entry": entry_address})
            self.stop.set()

        _FakeSocket.attempts = []
        fake_socket_module = mock.MagicMock(wraps=socket)
        fake_socket_module.socket = _FakeSocket
        for target, value in (("CONFIG_FILE", os.path.join(self.tmp.name, "config.json")),
                              ("log", lambda level, msg: self.logs.append((level, msg))),
                              ("peer_handler", fake_handler),
                              ("_set_tcp_nodelay", lambda s: None),
                              ("_tailscale_status_cached", lambda: tailscale_status()),
                              ("_peer_reach", pr.ReachTracker()),
                              ("socket", fake_socket_module)):
            patcher = mock.patch.object(tray, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        tray.istate.config = {"device_id": "aaaa1111", "device_name": "BOOK-Viktor",
                              "port": 45781, "peers": [dict(PEER)], "hotkeys": []}

    def test_lan_timeout_then_tailscale_address_connects_and_is_remembered(self):
        tray = self.tray
        token = "device:98194f67"
        tray.connect_one(token, self.stop)
        self.assertEqual(_FakeSocket.attempts, [(BETTINA_LAN, 45781), (BETTINA_TS, 45781)])
        self.assertEqual(self.handled, [{"addr": (BETTINA_TS, 45781), "dial_host": BETTINA_TS,
                                         "entry": (BETTINA_LAN, 45781)}])
        peer = tray.istate.config["peers"][0]
        self.assertEqual(peer["host"], BETTINA_LAN)  # the typed-in address stays
        self.assertEqual([(a["host"], a["via"]) for a in peer["addresses"]],
                         [(BETTINA_TS, "tailscale")])
        texts = [msg for level, msg in self.logs if level == "INFO"]
        self.assertTrue(any("connect to 192.168.8.18:45781 (LAN) failed: no answer" in t for t in texts))
        self.assertTrue(any("connected via 100.101.7.9:45781 (Tailscale)" in t for t in texts))
        self.assertFalse(any("outbound connect failed" in msg for _l, msg in self.logs))
        snap = tray.peer_reach_snapshot(token)
        self.assertEqual(snap["last_ok"]["host"], BETTINA_TS)
        self.assertEqual(snap["text"], "")

    def test_next_round_starts_with_the_address_that_worked(self):
        tray = self.tray
        peer = tray.istate.config["peers"][0]
        pr.remember_address(peer, BETTINA_TS, 45781, now=10)
        tray.connect_one("device:98194f67", self.stop)
        self.assertEqual(_FakeSocket.attempts, [(BETTINA_TS, 45781)])


if __name__ == "__main__":
    unittest.main()
