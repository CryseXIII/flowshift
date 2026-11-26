"""GET /api/hotkeys with Viktor's config (26.11.) and the quiet access log.

The WebGUI polls /api/hotkeys every 3 s. A loop variable named ``it`` in the
clipboard branch of ``do_GET`` hid the module ``input_trigger as it`` for the
whole handler, so every call ended in UnboundLocalError and HTTP 500.
"""
from __future__ import annotations

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime_model as rm
import web_api

ENDPOINT = "endpoint:192.168.8.18:45781"


class FakeState:
    def __init__(self, config):
        self.lock = threading.RLock()
        self.config = config
        self.hotkeys = rm.load_hotkeys(config)
        self.active_peer = None


def viktor_config():
    return {
        "device_id": "f2176177", "device_name": "PC-Viktor", "port": 45781,
        "peers": [{"name": "Laptop-Bettina", "host": "192.168.8.18", "port": 45781,
                   "listen_port": 45781, "display_name": "Laptop-Bettina",
                   "identity": ENDPOINT}],
        "hotkeys": [
            {"action": rm.FORWARD_PREFIX + ENDPOINT, "mods": rm.MOD_CTRL | rm.MOD_ALT,
             "key": 0x31, "label": "Forward to Laptop-Bettina"},
            {"action": rm.RETURN_ACTION, "mods": rm.MOD_CTRL | rm.MOD_ALT,
             "key": 0x30, "label": "Return to local"},
            {"action": rm.RETURN_ACTION, "mods": rm.MOD_CTRL, "key": 0,
             "label": "Unassigned"},
        ],
    }


class HotkeysApiTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        web_api._refs = {}
        web_api.init(istate=FakeState(viktor_config()),
                     log=lambda level, msg: self.logs.append((level, msg)))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        web_api._refs = {}

    def get(self, path):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
        connection.request("GET", path)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_peer_hotkeys_ctrl_alt_1_and_0_are_listed(self):
        for _poll in range(3):                      # the WebGUI asks every 3 s
            status, body = self.get("/api/hotkeys")
            self.assertEqual(status, 200, body)
        self.assertEqual(body["hotkeys"], [
            {"label": "Forward to Laptop-Bettina", "display": "Ctrl+Alt+1",
             "action": rm.FORWARD_PREFIX + ENDPOINT},
            {"label": "Return to local", "display": "Ctrl+Alt+0", "action": rm.RETURN_ACTION},
            {"label": "Unassigned", "display": "not set", "action": rm.RETURN_ACTION},
        ])
        self.assertFalse([m for level, m in self.logs if level == "ERROR"], self.logs)

    def test_successful_polls_are_not_logged_but_errors_are(self):
        self.get("/api/hotkeys")
        self.get("/api/hotkeys")
        self.assertEqual([m for _level, m in self.logs if m.startswith("web_api: ")], [])
        status, _body = self.get("/api/clipboard/preview")      # missing params: 400
        self.assertGreaterEqual(status, 400)
        self.assertTrue(any(m.startswith("web_api: ") and '"GET /api/clipboard/preview' in m
                            for _level, m in self.logs), self.logs)

    def test_quiet_rule(self):
        self.assertTrue(web_api._quiet_request("GET", 200))
        self.assertTrue(web_api._quiet_request("OPTIONS", 204))
        self.assertFalse(web_api._quiet_request("GET", 404))
        self.assertFalse(web_api._quiet_request("POST", 200))
        self.assertFalse(web_api._quiet_request("GET", "-"))


if __name__ == "__main__":
    unittest.main()
