"""Offline HTTP tests for the Command Wheel action routes."""
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
import overlay_actions as oa
import web_api
from runtime_model import MOD_ALT, MOD_CTRL, MOD_SHIFT


class FakeState:
    def __init__(self, config):
        self.lock = threading.RLock()
        self.config = config
        self.active_peer = None


class ActionApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temporary.name) / "config.json"
        config_schema.save_config(self.config_path, {"device_name": "kept", "peers": []})
        self.state = FakeState(config_schema.load_config(self.config_path))
        self.executed = []
        self.execute_result = {"ok": True, "reason": None}
        self.reloads = 0

        def save_config(config):
            return config_schema.save_config(self.config_path, config)

        def execute(action, context):
            self.executed.append((action["id"], context))
            return self.execute_result

        def reload_hotkeys():
            self.reloads += 1

        web_api._refs = {}
        web_api.init(istate=self.state, save_config=save_config,
                     execute_action=execute, reload_hotkeys=reload_hotkeys)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        web_api._refs = {}
        self.temporary.cleanup()

    def request(self, method, path, body=None, raw=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
        payload = raw if raw is not None else (json.dumps(body) if body is not None else None)
        connection.request(method, path, body=payload,
                           headers={"Content-Type": "application/json"} if payload else {})
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def stored_wheel(self):
        return json.loads(self.config_path.read_text("utf-8")).get("command_wheel")

    def test_get_actions_returns_registry_and_default_wheel(self):
        status, body = self.request("GET", "/api/actions")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual([a["id"] for a in body["actions"]], list(oa.action_ids()))
        self.assertEqual(body["wheel"]["pages"], [list(p) for p in oa.DEFAULT_PAGES])
        self.assertEqual(body["wheel"]["hotkey"]["display"], "Ctrl+Alt+Space")
        self.assertEqual(body["limits"], {"max_slots_per_page": 8, "max_pages": 16})
        for action in body["actions"]:
            self.assertNotIn("sequence", action)

    def test_save_wheel_persists_and_reloads_hotkeys(self):
        status, body = self.request("POST", "/api/actions/wheel", {
            "pages": [["cut", "", "paste"], [], ["delete"]],
            "hotkey": {"mods": MOD_CTRL | MOD_SHIFT, "vk": 0x57},
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["wheel"]["pages"], [["cut", "paste"], ["delete"]])
        self.assertEqual(body["wheel"]["hotkey"]["display"], "Ctrl+Shift+W")
        self.assertEqual(self.stored_wheel(),
                         {"pages": [["cut", "paste"], ["delete"]],
                          "hotkey": {"mods": MOD_CTRL | MOD_SHIFT, "vk": 0x57}})
        self.assertEqual(self.state.config["command_wheel"]["pages"], [["cut", "paste"], ["delete"]])
        self.assertEqual(self.reloads, 1)
        status, body = self.request("GET", "/api/actions")
        self.assertEqual(body["wheel"]["pages"], [["cut", "paste"], ["delete"]])

    def test_save_pages_only_keeps_existing_hotkey(self):
        self.request("POST", "/api/actions/wheel", {"hotkey": {"mods": MOD_ALT, "vk": 0x51}})
        status, body = self.request("POST", "/api/actions/wheel", {"pages": [["copy"]]})
        self.assertEqual(status, 200)
        self.assertEqual(body["wheel"]["hotkey"]["display"], "Alt+Q")
        self.assertEqual(self.stored_wheel()["hotkey"], {"mods": MOD_ALT, "vk": 0x51})

    def test_invalid_wheel_is_rejected_and_not_stored(self):
        cases = (
            ({"pages": [["copy", "bogus"]]}, "unknown_action"),
            ({"pages": [["copy"] * 9]}, "too_many_slots"),
            ({"pages": [[]]}, "empty_wheel"),
            ({"hotkey": {"mods": 0, "vk": 0x57}}, "invalid_hotkey"),
            ({"other": 1}, "invalid_request"),
            ({}, "invalid_request"),
        )
        for body, code in cases:
            with self.subTest(body=body):
                status, response = self.request("POST", "/api/actions/wheel", body)
                self.assertEqual(status, 400)
                self.assertEqual(response["error"], code)
        self.assertIsNone(self.stored_wheel())
        self.assertEqual(self.reloads, 0)
        status, response = self.request("POST", "/api/actions/wheel", raw="not json")
        self.assertEqual(status, 400)

    def test_execute_dispatches_validated_action(self):
        status, body = self.request("POST", "/api/actions/execute",
                                    {"action_id": "copy", "context": {"source": "command_wheel"}})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "action_id": "copy", "reason": None})
        self.assertEqual(self.executed, [("copy", {"source": "command_wheel"})])

    def test_execute_reports_runtime_refusal(self):
        self.execute_result = {"ok": False, "reason": "overlay_busy"}
        status, body = self.request("POST", "/api/actions/execute", {"action_id": "paste"})
        self.assertEqual(status, 409)
        self.assertEqual(body, {"ok": False, "action_id": "paste", "reason": "overlay_busy"})

    def test_execute_rejects_unknown_action_without_dispatch(self):
        status, body = self.request("POST", "/api/actions/execute", {"action_id": "format_disk"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "unknown_action")
        status, body = self.request("POST", "/api/actions/execute",
                                    {"action_id": "copy", "context": {"k": {"nested": True}}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_context")
        self.assertEqual(self.executed, [])

    def test_execute_without_runtime_returns_503(self):
        web_api._refs.pop("execute_action")
        status, body = self.request("POST", "/api/actions/execute", {"action_id": "copy"})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "not_available")

    def test_peers_route_lists_configured_peers_with_identities(self):
        # Regression: _normalize_runtime_peers used an unimported peer_identity.
        with self.state.lock:
            self.state.config["peers"] = [
                {"name": "Alpha", "host": "10.0.0.2", "port": 45781, "device_id": "aaaa0001"}]
        self.state.peers = {}
        status, body = self.request("GET", "/api/peers")
        self.assertEqual(status, 200, body)
        self.assertEqual([p["identity"] for p in body["peers"]], ["device:aaaa0001"])
        self.assertFalse(body["peers"][0]["connected"])


if __name__ == "__main__":
    unittest.main()
