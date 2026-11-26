"""Offline HTTP integration tests for the update Web API."""
from __future__ import annotations

from dataclasses import dataclass
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
import web_api


STATUS_KEYS = {
    "state", "current_version", "latest_version", "channel", "policy", "enabled",
    "check_on_start", "last_check_at", "last_successful_check_at", "release_notes",
    "release_url", "downloaded_asset", "progress", "can_install", "blocked_reason",
    "operation_active", "development_mode", "last_error", "last_update_result",
    "recovery_notices", "source", "checked_channel", "development_access",
    "automatic_install_held", "last_install",
}


@dataclass
class FakeResult:
    status: str
    operation: str
    message: str = ""
    error_code: str | None = None
    blocker: dict | None = None

    def to_dict(self):
        return {
            "status": self.status,
            "operation": self.operation,
            "message": self.message,
            "error_code": self.error_code,
            "blocker": self.blocker,
        }


class FakeManager:
    def __init__(self):
        self.calls = []
        self.results = {
            "check": FakeResult("queued", "check", "check queued"),
            "download": FakeResult("not_available", "download", "no update"),
            "install": FakeResult(
                "blocked", "install", "runtime busy", "install_blocked",
                {"safe": False, "reason": "forwarding_active", "blockers": ["forwarding_active"]},
            ),
        }
        self.status = {
            "state": "downloaded",
            "current_version": "0.4.0",
            "latest_version": "0.5.0",
            "last_check_at": "2026-07-21T10:00:00Z",
            "last_successful_check_at": "2026-07-21T10:00:00Z",
            "release_notes": "Plain release notes",
            "release_url": "https://github.com/CryseXIII/flowshift/releases/tag/v0.5.0",
            "downloaded_asset": {"path": "managed", "version": "0.5.0"},
            "download_progress": {
                "bytes_downloaded": 1048576,
                "bytes_total": 2097152,
                "percentage": 50.0,
                "bytes_per_second": 524288,
                "eta_seconds": 2,
            },
            "can_install": True,
            "blocked_reason": None,
            "development_mode": False,
            "last_error": None,
            "last_update_result": {"status": "downloaded"},
            "recovery_notices": [],
            "active_operation": None,
            "install_pending": False,
            "shutting_down": False,
        }

    def snapshot(self):
        return dict(self.status)

    def check_for_updates(self):
        self.calls.append("check")
        return self.results["check"]

    def download_update(self):
        self.calls.append("download")
        return self.results["download"]

    def install_update(self):
        self.calls.append("install")
        return self.results["install"]


class FakeState:
    def __init__(self, config):
        self.lock = threading.RLock()
        self.config = config


class UpdateApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temporary.name) / "config.json"
        initial = {
            "config_schema_version": 1,
            "device_name": "kept",
            "future_root": {"value": 7},
            "updates": {
                "enabled": False,
                "check_on_start": False,
                "channel": "stable",
                "policy": "notify",
                "future_update_key": "kept",
            },
        }
        config_schema.save_config(self.config_path, initial)
        self.state = FakeState(config_schema.load_config(self.config_path))
        self.manager = FakeManager()
        self.safety = {
            "safe": False,
            "reason": "forwarding_active",
            "blockers": ["forwarding_active"],
        }

        def save_config(config):
            return config_schema.save_config(self.config_path, config)

        web_api._refs = {}
        web_api.init(
            update_manager=self.manager,
            update_install_safety=lambda: self.safety,
            istate=self.state,
            save_config=save_config,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        web_api._refs = {}
        self.temporary.cleanup()

    def request(self, method, path, body=None, raw=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
        headers = dict(headers or {})
        headers.setdefault("Origin", f"http://127.0.0.1:{self.server.server_address[1]}")
        payload = raw
        if raw is None and body is not None:
            payload = json.dumps(body)
        if payload is not None and "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_status_shape_uses_settings_and_runtime_blocker(self):
        status, body = self.request("GET", "/api/update/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        update = body["update"]
        self.assertEqual(set(update), STATUS_KEYS)
        self.assertFalse(update["enabled"])
        self.assertEqual(update["channel"], "stable")
        self.assertFalse(update["can_install"])
        self.assertEqual(update["blocked_reason"], "forwarding_active")
        self.assertEqual(update["progress"]["percentage"], 50.0)

        self.safety = {"safe": True, "reason": "safe", "blockers": []}
        self.manager.status["active_operation"] = "download"
        status, body = self.request("GET", "/api/update/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["update"]["operation_active"])
        self.assertFalse(body["update"]["can_install"])
        self.assertEqual(body["update"]["blocked_reason"], "update_operation_active")

        self.manager.status["active_operation"] = None
        status, body = self.request("GET", "/api/update/status")
        self.assertEqual(status, 200)
        self.assertFalse(body["update"]["operation_active"])
        self.assertTrue(body["update"]["can_install"])
        self.assertIsNone(body["update"]["blocked_reason"])

    def test_all_operation_routes_return_manager_admission_results(self):
        expected = {
            "check": (202, "queued"),
            "download": (409, "not_available"),
            "install": (409, "blocked"),
        }
        for operation, (http_status, result_status) in expected.items():
            with self.subTest(operation=operation):
                status, body = self.request("POST", f"/api/update/{operation}", {})
                self.assertEqual(status, http_status)
                self.assertEqual(body["status"], result_status)
                self.assertEqual(body["operation"], operation)
        self.assertEqual(self.manager.calls, ["check", "download", "install"])
        self.assertEqual(body["blocker"]["reason"], "forwarding_active")

    def test_operation_outcomes_include_already_running_and_shutting_down(self):
        for result_status, http_status in (("already_running", 409), ("shutting_down", 503)):
            with self.subTest(result_status=result_status):
                self.manager.results["check"] = FakeResult(result_status, "check", result_status)
                status, body = self.request("POST", "/api/update/check", {})
                self.assertEqual(status, http_status)
                self.assertEqual(body["status"], result_status)
                self.assertFalse(body["ok"])

    def test_operations_reject_arbitrary_update_inputs_without_calling_manager(self):
        forbidden = {
            "url": "https://example.invalid/setup.exe",
            "release": {"version": "99.0.0"},
            "installer_path": "C:\\Temp\\evil.exe",
            "hash": "00" * 32,
            "command": "calc.exe",
            "target_version": "99.0.0",
        }
        for operation in ("check", "download", "install"):
            with self.subTest(operation=operation):
                status, body = self.request("POST", f"/api/update/{operation}", forbidden)
                self.assertEqual(status, 400)
                self.assertEqual(body["status"], "blocked")
                self.assertEqual(body["error_code"], "request_body_not_allowed")
        self.assertEqual(self.manager.calls, [])
        status, body = self.request("POST", "/api/update/check", raw="[")
        self.assertEqual(status, 400)
        self.assertIn("valid JSON", body["error"])

    def test_settings_validation_atomic_persistence_and_preservation(self):
        settings = {
            "enabled": False,
            "check_on_start": True,
            "channel": "stable",
            "policy": "install",
        }
        status, body = self.request("POST", "/api/update/settings", settings)
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"], settings)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["device_name"], "kept")
        self.assertEqual(saved["future_root"], {"value": 7})
        self.assertEqual(saved["updates"]["future_update_key"], "kept")
        self.assertEqual(saved["updates"]["policy"], "install")

        status, body = self.request("GET", "/api/update/status")
        self.assertEqual(body["update"]["policy"], "install")
        self.assertTrue(body["update"]["check_on_start"])

        status, body = self.request("POST", "/api/update/check", {})
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "queued")

    def test_settings_reject_unknown_missing_and_malformed_values(self):
        valid = {
            "enabled": True,
            "check_on_start": True,
            "channel": "stable",
            "policy": "notify",
        }
        cases = [
            ({**valid, "url": "https://example.invalid"}, "exactly"),
            ({key: value for key, value in valid.items() if key != "policy"}, "exactly"),
            ({**valid, "enabled": 1}, "booleans"),
            ({**valid, "check_on_start": "true"}, "booleans"),
            ({**valid, "channel": "beta"}, "stable"),
            ({**valid, "policy": "force"}, "notify"),
        ]
        before = self.config_path.read_bytes()
        for payload, message in cases:
            with self.subTest(payload=payload):
                status, body = self.request("POST", "/api/update/settings", payload)
                self.assertEqual(status, 400)
                self.assertIn(message, body["error"])
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_manager_absent_is_503_for_status_and_operations(self):
        web_api._refs["update_manager"] = None
        status, body = self.request("GET", "/api/update/status")
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "not_available")
        status, body = self.request("POST", "/api/update/download", {})
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "not_available")

    def test_mutating_update_routes_reject_untrusted_request_shapes(self):
        cases = [
            ({"Origin": "https://example.invalid"}, 403, "origin"),
            ({"Host": "example.invalid", "Content-Type": "application/json"}, 403, "local"),
            ({"Content-Type": "text/plain"}, 415, "application/json"),
            ({"Content-Type": "application/json", "Content-Length": "65537"}, 413, "too large"),
        ]
        for headers, expected_status, message in cases:
            with self.subTest(headers=headers):
                status, body = self.request(
                    "POST", "/api/update/check", raw="{}", headers=headers)
                self.assertEqual(status, expected_status)
                self.assertIn(message, body["error"])
        status, body = self.request("POST", "/api/update/check")
        self.assertEqual(status, 415)
        self.assertIn("application/json", body["error"])
        self.assertEqual(self.manager.calls, [])

    def test_development_mode_always_blocks_install_capability(self):
        self.manager.status["development_mode"] = True
        self.manager.status["blocked_reason"] = "development_checkout"
        self.safety = {"safe": True, "reason": "safe", "blockers": []}
        status, body = self.request("GET", "/api/update/status")
        self.assertEqual(status, 200)
        self.assertFalse(body["update"]["can_install"])
        self.assertEqual(body["update"]["blocked_reason"], "development_checkout")


class FakeUsbJob:
    def __init__(self):
        self.starts = 0
        self.sources = []
        self.state = {"state": "idle"}

    def status(self):
        return dict(self.state)

    def start(self, python_source="download"):
        if self.state["state"] == "running":
            return False, self.status()
        self.starts += 1
        self.sources.append(python_source)
        self.state = {"state": "running", "step": "Starting"}
        return True, self.status()


class UsbPackageApiTests(UpdateApiTests):
    """Settings > Software Update: USB package button (development channel only)."""

    def setUp(self):
        super().setUp()
        self.job = FakeUsbJob()
        web_api.init(usb_package_job=self.job)

    def set_channel(self, channel):
        with self.state.lock:
            self.state.config = dict(self.state.config, updates=dict(
                self.state.config["updates"], channel=channel))

    def test_usb_package_is_offered_only_on_the_development_channel(self):
        status, body = self.request("GET", "/api/update/usb-package")
        self.assertEqual(status, 200)
        self.assertFalse(body["available"])
        self.assertEqual(body["blocked_reason"], "channel_not_development")
        status, body = self.request("POST", "/api/update/usb-package", {})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "channel_not_development")
        self.assertEqual(self.job.starts, 0)

        self.set_channel("development")
        status, body = self.request("GET", "/api/update/usb-package")
        self.assertTrue(body["available"])
        self.assertEqual(body["package"], {"state": "idle"})
        status, body = self.request("POST", "/api/update/usb-package", {})
        self.assertEqual(status, 202)
        self.assertTrue(body["ok"])
        self.assertEqual(body["package"]["state"], "running")
        status, body = self.request("POST", "/api/update/usb-package", {})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "already_running")
        self.assertEqual(self.job.starts, 1)

    def test_usb_package_refuses_body_checkout_and_missing_job(self):
        self.set_channel("development")
        status, body = self.request("POST", "/api/update/usb-package", {"path": "C:/x"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error_code"], "request_body_not_allowed")
        status, _ = self.request("POST", "/api/update/usb-package", {},
                                 headers={"Origin": "http://attrappe.example"})
        self.assertEqual(status, 403)
        self.manager.status["development_mode"] = True
        status, body = self.request("POST", "/api/update/usb-package", {})
        self.assertEqual(status, 409)
        self.assertEqual(body["blocked_reason"], "development_checkout")
        web_api._refs.pop("usb_package_job")
        status, body = self.request("GET", "/api/update/usb-package")
        self.assertEqual(body["blocked_reason"], "not_available")
        status, _ = self.request("POST", "/api/update/usb-package", {})
        self.assertEqual(status, 503)
        self.assertEqual(self.job.starts, 0)

    def test_usb_package_can_use_the_python_installer_from_downloads(self):
        """After a failed python.org download: the browser-saved file, never a path."""
        self.set_channel("development")
        for body in ({"python_installer": "C:/Users/x/evil.exe"},
                     {"python_installer": "downloads", "path": "C:/x"},
                     {"python_installer": "skip-checksum"}):
            status, answer = self.request("POST", "/api/update/usb-package", body)
            self.assertEqual(status, 400, body)
            self.assertEqual(answer["error_code"], "request_body_not_allowed")
        self.assertEqual(self.job.starts, 0)
        status, body = self.request("POST", "/api/update/usb-package", {"python_installer": "downloads"})
        self.assertEqual(status, 202)
        self.assertEqual(self.job.sources, ["downloads"])


class FakePeerUpdate:
    def __init__(self):
        self.calls = []
        self.state = "available"

    def snapshot(self):
        return {"state": self.state, "offer": {"version": "0.7.0-dev.90", "peer_name": "Viktor-PC"}}

    def settings_changed(self):
        self.calls.append("settings_changed")

    def download(self):
        self.calls.append("download")
        return "started"

    def install(self):
        self.calls.append("install")
        return "no_offer"

    def cancel(self):
        self.calls.append("cancel")
        return False


class PeerUpdateApiTests(UpdateApiTests):
    """Settings > Software Update > Updates from connected devices."""

    def setUp(self):
        super().setUp()
        self.peer = FakePeerUpdate()
        web_api.init(peer_update=self.peer)

    def test_status_and_actions(self):
        status, body = self.request("GET", "/api/update/peer")
        self.assertEqual(status, 200)
        self.assertEqual(body["peer_update"]["offer"]["version"], "0.7.0-dev.90")
        status, body = self.request("POST", "/api/update/peer/download", {})
        self.assertEqual((status, body["status"]), (202, "started"))
        status, body = self.request("POST", "/api/update/peer/install", {})
        self.assertEqual((status, body["status"]), (409, "no_offer"))
        status, body = self.request("POST", "/api/update/peer/cancel", {})
        self.assertEqual((status, body["status"]), (409, "nothing_to_cancel"))
        status, body = self.request("POST", "/api/update/peer/download", {"force": True})
        self.assertEqual(status, 400)
        self.assertEqual(self.peer.calls, ["download", "install", "cancel"])

    def test_settings_are_saved_strictly_and_keep_other_update_keys(self):
        status, body = self.request("POST", "/api/update/peer/settings",
                                    {"policy": "install", "allow_development": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"], {"policy": "install", "allow_development": True})
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["updates"]["from_peers"], {"policy": "install", "allow_development": True})
        self.assertEqual(saved["updates"]["future_update_key"], "kept")
        self.assertEqual(saved["updates"]["channel"], "stable")
        self.assertEqual(self.peer.calls, ["settings_changed"])
        for bad in ({"policy": "always", "allow_development": False}, {"policy": "notify"},
                    {"policy": "notify", "allow_development": "yes"},
                    {"policy": "notify", "allow_development": False, "extra": 1}):
            status, _ = self.request("POST", "/api/update/peer/settings", bad)
            self.assertEqual(status, 400, bad)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["updates"]["from_peers"]["policy"], "install")

    def test_normal_update_settings_keep_the_peer_setting(self):
        self.request("POST", "/api/update/peer/settings", {"policy": "download", "allow_development": False})
        status, _ = self.request("POST", "/api/update/settings", {
            "enabled": True, "check_on_start": True, "channel": "stable", "policy": "notify"})
        self.assertEqual(status, 200)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["updates"]["from_peers"]["policy"], "download")

    def test_foreign_page_cannot_trigger_a_peer_install(self):
        status, _ = self.request("POST", "/api/update/peer/install", {},
                                 headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.peer.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
