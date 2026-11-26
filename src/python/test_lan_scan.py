"""Broadcast scan, one socket per adapter (lan_scan): masks, sockets, replies."""

import json
import socket
import unittest

import lan_scan


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeSocket:
    """UDP socket stand-in: records bind/sendto, answers from a reply queue."""

    def __init__(self, net, *_args):
        self.net = net
        self.bound = None
        self.sent = []
        self.replies = []
        self.options = {}
        self.closed = False
        net.sockets.append(self)

    def setsockopt(self, level, name, value):
        self.options[(level, name)] = value

    def settimeout(self, value):
        self.timeout = value

    def bind(self, addr):
        if addr[0] in self.net.refuse_bind:
            raise OSError("cannot bind")
        self.bound = addr

    def sendto(self, data, addr):
        self.sent.append((json.loads(data.decode("utf-8")), addr))
        for reply, source in self.net.answers.get((self.bound[0], addr[0]), []):
            self.replies.append((json.dumps(reply).encode("utf-8"), source))

    def recvfrom(self, _size):
        if self.replies:
            return self.replies.pop(0)
        raise BlockingIOError()

    def close(self):
        self.closed = True


class FakeNet:
    def __init__(self):
        self.sockets = []
        self.answers = {}
        self.refuse_bind = set()

    def factory(self, *args):
        return FakeSocket(self, *args)


def reply(device_id, name="PC2", port=45781):
    return {"type": "discover_reply", "device_id": device_id, "display_name": name, "port": port}


class NetworkEntryTest(unittest.TestCase):
    def test_slash_23_gives_the_real_broadcast_address(self):
        entry = lan_scan.network_entry("192.168.0.23", 23, "WLAN")
        self.assertEqual(entry["broadcast"], "192.168.1.255")
        self.assertTrue(entry["mask_known"])
        self.assertEqual(entry["prefix"], 23)

    def test_unknown_mask_assumes_24_and_says_so(self):
        entry = lan_scan.network_entry("10.1.2.3")
        self.assertEqual(entry["broadcast"], "10.1.2.255")
        self.assertFalse(entry["mask_known"])

    def test_point_to_point_and_local_addresses(self):
        self.assertIsNone(lan_scan.network_entry("100.64.1.2", 32, "Tailscale")["broadcast"])
        self.assertIsNone(lan_scan.network_entry("127.0.0.1", 8))
        self.assertIsNone(lan_scan.network_entry("169.254.3.4", 16))
        self.assertIsNone(lan_scan.network_entry("nonsense", 24))

    def test_parse_get_netipaddress_output(self):
        text = json.dumps([
            {"IPAddress": "192.168.0.23", "PrefixLength": 23, "InterfaceAlias": "WLAN"},
            {"IPAddress": "10.8.0.2", "PrefixLength": 24, "InterfaceAlias": "WireGuard"},
            {"IPAddress": "127.0.0.1", "PrefixLength": 8, "InterfaceAlias": "Loopback"},
        ])
        rows = lan_scan.parse_netipaddress(text)
        self.assertEqual([(r["name"], r["broadcast"]) for r in rows],
                         [("WLAN", "192.168.1.255"), ("WireGuard", "10.8.0.255")])
        single = lan_scan.parse_netipaddress(json.dumps(
            {"IPAddress": "192.168.5.9", "PrefixLength": 24, "InterfaceAlias": "Ethernet"}))
        self.assertEqual(single[0]["broadcast"], "192.168.5.255")
        self.assertEqual(lan_scan.parse_netipaddress("not json"), [])


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.net = FakeNet()
        self.clock = FakeClock()
        self.inbox = lan_scan.ReplyInbox()
        self.adapters = lan_scan.adapter_networks([
            {"address": "192.168.0.23", "prefix": 23, "name": "WLAN"},
            {"address": "10.8.0.2", "prefix": 24, "name": "WireGuard"},
            {"address": "100.101.1.2", "prefix": 32, "name": "Tailscale"},
        ])

    def scan(self, timeout=3.0, local_device_id="laptop"):
        return lan_scan.scan(45781, self.adapters, timeout, local_device_id,
                             socket_factory=self.net.factory, inbox=self.inbox,
                             clock=self.clock, sleep=self.clock.sleep)

    def test_one_socket_per_adapter_bound_to_its_address_with_its_broadcast(self):
        found, searched = self.scan()
        self.assertEqual(found, [])
        bound = [s.bound for s in self.net.sockets]
        self.assertEqual(bound, [("192.168.0.23", 0), ("10.8.0.2", 0)])
        wlan, wireguard = self.net.sockets
        self.assertEqual([a for _, a in wlan.sent],
                         [("192.168.1.255", 45781), ("255.255.255.255", 45781)])
        self.assertEqual([a for _, a in wireguard.sent],
                         [("10.8.0.255", 45781), ("255.255.255.255", 45781)])
        for sock in self.net.sockets:
            self.assertEqual(sock.options[(socket.SOL_SOCKET, socket.SO_BROADCAST)], 1)
            self.assertTrue(sock.closed)
        # The request asks for the reply on the FlowShift port too.
        self.assertEqual(wlan.sent[0][0], {"type": "discover", "reply_port": 45781})
        self.assertEqual([(r["name"], r["status"]) for r in searched],
                         [("WLAN", "sent"), ("WireGuard", "sent"), ("Tailscale", "point_to_point")])
        self.assertEqual(lan_scan.summary(searched),
                         "WLAN (192.168.0.23/23), WireGuard (10.8.0.2/24)")

    def test_pc_in_the_upper_half_of_a_slash_23_answers(self):
        # PC2 at 192.168.1.40: a /24 guess (192.168.0.255) never reached it.
        self.net.answers[("192.168.0.23", "192.168.1.255")] = [
            (reply("pc2"), ("192.168.1.40", 45781))]
        found, _ = self.scan()
        self.assertEqual(found, [{"name": "PC2", "host": "192.168.1.40", "port": 45781,
                                  "device_id": "pc2", "adapter": "WLAN (192.168.0.23/23)"}])

    def test_reply_on_the_flowshift_port_counts_and_duplicates_and_self_are_dropped(self):
        self.net.answers[("192.168.0.23", "192.168.1.255")] = [
            (reply("pc2"), ("192.168.1.40", 45781)),
            (reply("laptop", "Laptop"), ("192.168.0.23", 45781))]

        def deliver_during_scan(seconds):
            # The discovery listener hands over what came in on port 45781.
            self.inbox.deliver(reply("pc2"), ("192.168.1.40", 45781))
            self.inbox.deliver(reply("pc3", "PC3"), ("192.168.1.41", 45781))
            self.clock.now += seconds

        found, _ = lan_scan.scan(45781, self.adapters, 3.0, "laptop",
                                 socket_factory=self.net.factory, inbox=self.inbox,
                                 clock=self.clock, sleep=deliver_during_scan)
        self.assertEqual([(f["device_id"], f["adapter"]) for f in found],
                         [("pc2", "WLAN (192.168.0.23/23)"), ("pc3", "WLAN (192.168.0.23/23)")])
        # Inbox closed after the scan: later replies go nowhere.
        self.assertFalse(self.inbox.deliver(reply("pc4"), ("192.168.1.42", 45781)))

    def test_waits_the_full_timeout(self):
        self.scan(timeout=3.0)
        self.assertGreaterEqual(self.clock.now, 3.0)
        self.assertLess(self.clock.now, 3.1)

    def test_adapter_that_cannot_bind_is_reported_and_others_still_scan(self):
        self.net.refuse_bind.add("10.8.0.2")
        _, searched = self.scan()
        self.assertEqual([r["status"] for r in searched], ["sent", "failed", "point_to_point"])
        self.assertIn("cannot bind", searched[1]["error"])

    def test_no_adapter_known_falls_back_to_the_wildcard_socket(self):
        self.adapters = []
        _, searched = self.scan()
        self.assertEqual(self.net.sockets[0].bound, ("0.0.0.0", 0))
        self.assertEqual([a for _, a in self.net.sockets[0].sent], [("255.255.255.255", 45781)])
        self.assertEqual(searched[0]["status"], "sent")


class ReplyTargetsTest(unittest.TestCase):
    def test_source_port_and_flowshift_port_on_the_same_host(self):
        addr = ("192.168.0.23", 51234)
        self.assertEqual(lan_scan.reply_targets(addr, {"type": "discover", "reply_port": 45781}),
                         [addr, ("192.168.0.23", 45781)])

    def test_old_request_or_bad_port_only_source(self):
        addr = ("192.168.0.23", 51234)
        for request in ({"type": "discover"}, {"reply_port": 80}, {"reply_port": "45781"},
                        {"reply_port": True}, {"reply_port": 70000}):
            self.assertEqual(lan_scan.reply_targets(addr, request), [addr])
        self.assertEqual(lan_scan.reply_targets(("192.168.0.23", 45781), {"reply_port": 45781}),
                         [("192.168.0.23", 45781)])


if __name__ == "__main__":
    unittest.main()
