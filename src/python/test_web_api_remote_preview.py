"""Offline HTTP tests: remote preview settings through ``/api/settings``."""
from __future__ import annotations

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config_schema
import remote_preview as rp
import web_api


class FakeState:
    def __init__(self, config):
        self.lock = threading.RLock()
        self.config = config
        self.active_peer = None


class RemotePreviewSettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temporary.name) / "config.json"
        config_schema.save_config(self.config_path, {"device_name": "kept", "peers": []})
        self.state = FakeState(config_schema.load_config(self.config_path))
        self.announced = 0

        def save_config(config):
            return config_schema.save_config(self.config_path, config)

        def announced():
            self.announced += 1

        web_api._refs = {}
        web_api.init(istate=self.state, save_config=save_config,
                     remote_preview_changed=announced)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        web_api._refs = {}
        self.temporary.cleanup()

    def request(self, method, path, body=None, origin=True):
        port = self.server.server_address[1]
        connection = HTTPConnection("127.0.0.1", port, timeout=2)
        payload = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{port}"
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def stored(self):
        return json.loads(self.config_path.read_text("utf-8")).get("remote_preview")

    def test_get_returns_defaults_before_first_save(self):
        status, body = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(body["remote_preview"], rp.DEFAULT_SETTINGS)
        self.assertIsNone(self.stored())

    def test_save_persists_merges_and_survives_reload(self):
        status, body = self.request("POST", "/api/settings", {"remote_preview": {
            "enabled": True, "fps": 24, "monitor": 1, "window": {"corner": "top_left"}}})
        self.assertEqual(status, 200, body)
        expected = {"enabled": True, "fps": 24, "max_width": 960, "monitor": 1,
                    "cursor_crop": False, "window": {"corner": "top_left", "width_px": 480}}
        self.assertEqual(self.stored(), expected)
        self.assertEqual(self.state.config["remote_preview"], expected)
        self.assertEqual(self.announced, 1)
        # A partial second save keeps the earlier values; same enabled -> no announce.
        status, body = self.request("POST", "/api/settings", {"remote_preview": {
            "cursor_crop": True, "window": {"width_px": 640}}})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.announced, 1)
        expected.update(cursor_crop=True, window={"corner": "top_left", "width_px": 640})
        # "Reload": a fresh state from the file and a fresh GET.
        self.state.config = config_schema.load_config(self.config_path)
        status, body = self.request("GET", "/api/settings")
        self.assertEqual(body["remote_preview"], expected)

    def test_invalid_values_are_refused_and_nothing_is_stored(self):
        for bad, field in (({"fps": 31}, "fps"), ({"fps": "15"}, "fps"),
                           ({"max_width": 100}, "max_width"), ({"monitor": -1}, "monitor"),
                           ({"enabled": "yes"}, "enabled"), ({"stream_debug": True}, "stream_debug"),
                           ({"window": {"corner": "center"}}, "window.corner"),
                           ({"window": {"width_px": 5000}}, "window.width_px")):
            status, body = self.request("POST", "/api/settings",
                                        {"remote_preview": bad, "device_name": "changed"})
            self.assertEqual(status, 400, bad)
            self.assertEqual(body["error"], "invalid_remote_preview")
            self.assertEqual(body["fields"], [field])
        status, body = self.request("POST", "/api/settings", {"remote_preview": "on"})
        self.assertEqual((status, body["fields"]), (400, ["remote_preview"]))
        self.assertIsNone(self.stored())
        self.assertEqual(self.state.config["device_name"], "kept")
        self.assertEqual(self.announced, 0)

    def test_write_needs_own_origin(self):
        status, body = self.request("POST", "/api/settings",
                                    {"remote_preview": {"enabled": True}}, origin=False)
        self.assertEqual(status, 403, body)
        self.assertIsNone(self.stored())

    def test_body_limit_of_route(self):
        status, _body = self.request("POST", "/api/settings", {
            "remote_preview": {"enabled": True}, "pad": "x" * (web_api._LOCAL_REQUEST_MAX_BYTES + 1)})
        self.assertEqual(status, 413)
        self.assertIsNone(self.stored())


if __name__ == "__main__":
    unittest.main()
