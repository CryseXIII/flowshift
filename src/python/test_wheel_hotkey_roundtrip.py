"""Command wheel hotkey round trip: WebGUI requests -> Web-API -> config.json -> reload -> GET.

Viktor 24.11. (0.7.0-dev.50): a changed or cleared wheel hotkey was
Ctrl+right click again after Save and F5. These tests run the real HTTP
handler of ``web_api`` with the real config file writer (``config_schema``)
and send exactly what the Settings page sends (routes and bodies of
``CommandWheelSection.save`` / ``SettingsPanel.handleSave``). After each save
the runtime is "restarted": config.json is loaded again the way ``tray.load_config``
does it for the wheel (``config_schema.load_config`` + ``migrate_wheel_config``)
and ``GET /api/actions`` must return the saved value.

Why the earlier tests were green: they posted straight to ``/api/actions/wheel``.
Viktor pressed "Save Settings" at the bottom of the page, which only posted
``/api/settings``; since dev.50 that route ignores ``command_wheel`` on
purpose, answered "Settings saved." and the wheel stayed as it was.
``test_save_settings_route_alone_never_stores_the_wheel`` pins that route
behaviour; the WebGUI now saves the wheel first whenever "Save Settings" is
clicked (see ``SettingsPanel.test.jsx``).
"""
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
from runtime_model import MOD_CTRL

CTRL_SIDE_1 = {"mods": MOD_CTRL, "mouse": "x1"}
CTRL_RIGHT = {"mods": MOD_CTRL, "mouse": "right"}
FOREIGN_KEYS = ("command_wheel", "peers", "display_layout", "updates", "remote_preview")


class FakeState:
    def __init__(self, config):
        self.lock = threading.RLock()
        self.config = config
        self.active_peer = None


class WheelHotkeyRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temporary.name) / "config.json"
        config_schema.save_config(self.config_path, {"device_name": "attrappe", "peers": []})
        self.server = None
        self.start_runtime()

    def tearDown(self):
        self.stop_runtime()
        web_api._refs = {}
        self.temporary.cleanup()

    # ── runtime ──────────────────────────────────────────────────────
    def start_runtime(self):
        """Load config.json like ``tray.load_config`` (wheel part) and serve the API."""
        cfg = config_schema.load_config(self.config_path)
        if oa.migrate_wheel_config(cfg):
            cfg = config_schema.save_config(self.config_path, cfg)
        self.state = FakeState(cfg)

        def save_config(config):
            return config_schema.save_config(self.config_path, config)

        web_api._refs = {}
        web_api.init(istate=self.state, save_config=save_config, reload_hotkeys=lambda: None)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop_runtime(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def restart(self):
        self.stop_runtime()
        self.start_runtime()

    def request(self, method, path, body=None):
        port = self.server.server_address[1]
        connection = HTTPConnection("127.0.0.1", port, timeout=3)
        headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}
        connection.request(method, path, body=None if body is None else json.dumps(body),
                           headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def stored_wheel(self):
        return json.loads(self.config_path.read_text("utf-8")).get("command_wheel")

    # ── what the Settings page sends ─────────────────────────────────
    def open_page(self):
        """F5: the Settings page loads both blocks."""
        status, settings = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        status, actions = self.request("GET", "/api/actions")
        self.assertEqual(status, 200)
        return settings, actions["wheel"]

    def press_save_settings(self, settings, wheel, hotkey):
        """``SettingsPanel.handleSave`` with a changed wheel hotkey (dev.51)."""
        wheel_body = {"pages": wheel["pages"], "hotkey": hotkey, "trigger": wheel["trigger"]}
        status, wheel_answer = self.request("POST", "/api/actions/wheel", wheel_body)
        self.assertEqual(status, 200, wheel_answer)
        payload = {k: v for k, v in settings.items() if k not in FOREIGN_KEYS}
        status, settings_answer = self.request("POST", "/api/settings", payload)
        self.assertEqual(status, 200, settings_answer)
        return wheel_answer

    # ── tests ────────────────────────────────────────────────────────
    def test_changed_hotkey_survives_save_reload_and_restart(self):
        settings, wheel = self.open_page()
        self.assertEqual(wheel["hotkey"]["display"], "Ctrl+Right click")
        answer = self.press_save_settings(settings, wheel, CTRL_SIDE_1)
        self.assertEqual(answer["wheel"]["hotkey"]["display"], "Ctrl+Side button 1")
        self.assertEqual(self.stored_wheel()["hotkey"], CTRL_SIDE_1)
        # F5 without restart, then F5 after FlowShift restarted.
        self.assertEqual(self.open_page()[1]["hotkey"]["display"], "Ctrl+Side button 1")
        self.restart()
        _settings, wheel = self.open_page()
        self.assertEqual(wheel["hotkey"]["display"], "Ctrl+Side button 1")
        self.assertNotIn("extra_hotkeys", wheel)

    def test_cleared_hotkey_stays_empty_after_reload_and_restart(self):
        settings, wheel = self.open_page()
        answer = self.press_save_settings(settings, wheel, None)
        self.assertIsNone(answer["wheel"]["hotkey"])
        self.assertIn("hotkey", self.stored_wheel())
        self.assertIsNone(self.stored_wheel()["hotkey"])
        self.assertIsNone(self.open_page()[1]["hotkey"])
        self.restart()
        self.assertIsNone(self.open_page()[1]["hotkey"])
        self.assertIsNone(self.stored_wheel()["hotkey"])
        # And set again from empty.
        settings, wheel = self.open_page()
        self.press_save_settings(settings, wheel, CTRL_SIDE_1)
        self.restart()
        self.assertEqual(self.open_page()[1]["hotkey"]["display"], "Ctrl+Side button 1")

    def test_save_settings_route_alone_never_stores_the_wheel(self):
        # What the dev.50 page did on "Save Settings": only /api/settings, the
        # wheel block left out. Answer 200, config.json wheel unchanged.
        settings, _wheel = self.open_page()
        payload = {k: v for k, v in settings.items() if k not in FOREIGN_KEYS}
        status, body = self.request("POST", "/api/settings", payload)
        self.assertEqual(status, 200, body)
        # Even a posted command_wheel is ignored by that route.
        status, body = self.request("POST", "/api/settings",
                                    dict(payload, command_wheel={"version": 3, "hotkey": None}))
        self.assertEqual(status, 200, body)
        self.restart()
        self.assertEqual(self.open_page()[1]["hotkey"]["display"], "Ctrl+Right click")

    def test_dev50_hotkey_list_on_disk_becomes_one_hotkey(self):
        # Config written by dev.49/50 (version 2 with a list): first entry wins.
        cfg = json.loads(self.config_path.read_text("utf-8"))
        cfg["command_wheel"] = {"version": 2, "pages": [["copy"]], "hotkey": None,
                                "extra_hotkeys": [CTRL_SIDE_1, {"mods": 0, "mouse": "x2"}],
                                "trigger": dict(oa.DEFAULT_TRIGGER)}
        self.config_path.write_text(json.dumps(cfg), "utf-8")
        self.restart()
        self.assertEqual(self.stored_wheel(), {"version": 3, "pages": [["copy"]],
                                               "hotkey": CTRL_SIDE_1,
                                               "trigger": dict(oa.DEFAULT_TRIGGER)})
        self.assertEqual(self.open_page()[1]["hotkey"]["display"], "Ctrl+Side button 1")

    def test_cleared_hotkey_on_disk_is_not_replaced_by_the_default(self):
        cfg = json.loads(self.config_path.read_text("utf-8"))
        for stored in ({"version": 2, "hotkey": None, "extra_hotkeys": []},
                       {"version": 3, "hotkey": None},
                       {"version": 3, "hotkey": "Ctrl+Right"}):
            with self.subTest(stored=stored):
                cfg["command_wheel"] = stored
                self.config_path.write_text(json.dumps(cfg), "utf-8")
                self.restart()
                self.assertIsNone(self.open_page()[1]["hotkey"])


if __name__ == "__main__":
    unittest.main()
