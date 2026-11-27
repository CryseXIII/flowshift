"""Scan lines in the log, Tailscale reasons, and the remote preview self-test route.

Viktor 27.11.: three ``POST /api/peers/scan`` with 200 and no line which
networks were asked; the preview of the other PC without a single log line.
Covered: ``peer_discovery.scan_log_lines`` and the reason of a missing
Tailscale status, the scan route writing those INFO lines through the
productive handler, ``POST /api/remote-preview/self-test`` handing the
runtime's report through, saving a forward hotkey in the old WebGUI form, and
remote preview lines landing under "Connection" in the Event Log.
"""
from __future__ import annotations

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config_schema
import event_log
import peer_discovery as pdisc
import web_api

SEARCHED = [
    {"label": "WLAN (192.168.8.201/24)", "address": "192.168.8.201", "prefix": 24, "status": "sent"},
    {"label": "VirtualBox Host-Only (192.168.56.1/24)", "address": "192.168.56.1", "prefix": 24,
     "status": "sent"},
    {"label": "Tailscale (100.90.1.1/32)", "address": "100.90.1.1", "prefix": 32,
     "status": "point_to_point"},
]


class ScanLogLineTests(unittest.TestCase):
    def test_lines_name_adapters_tailscale_and_result(self):
        summary = {"tailscale": {"available": True, "asked": 2, "reason": "Running", "offline": 1,
                                 "self": {"address": "100.90.1.1"}},
                   "vpn": {"interfaces": ["wg0"], "asked": 3}}
        found = [{"name": "Laptop-Bettina", "host": "100.101.7.9", "port": 45781, "via": "tailscale"}]
        lines = pdisc.scan_log_lines(found, summary, SEARCHED)
        self.assertTrue(all(level == "INFO" for level, _ in lines))
        text = "\n".join(t for _, t in lines)
        self.assertIn("WLAN (192.168.8.201/24): broadcast sent", text)
        self.assertIn("Tailscale (100.90.1.1/32): no broadcast (point-to-point)", text)
        self.assertIn("Tailscale Running, asked 2 online device(s) directly, 1 offline device(s) "
                      "skipped, this PC is 100.90.1.1", text)
        self.assertIn("VPN: asked 3 address(es) over wg0", text)
        self.assertIn("peer scan found 1: Laptop-Bettina 100.101.7.9:45781 via tailscale", text)

    def test_empty_scan_says_why_tailscale_was_not_used(self):
        summary = {"tailscale": {"available": False, "asked": 0,
                                 "reason": pdisc.TAILSCALE_NOT_INSTALLED},
                   "vpn": {"interfaces": [], "asked": 0}}
        text = "\n".join(t for _, t in pdisc.scan_log_lines([], summary, SEARCHED[:1]))
        self.assertIn("Tailscale not used: not installed (no tailscale command)", text)
        self.assertIn("peer scan found no FlowShift PC", text)


class TailscaleReasonTests(unittest.TestCase):
    def read(self, run, which=lambda name: "C:/Tailscale/tailscale.exe"):
        diag = {}
        status = pdisc.read_tailscale_status(run=run, which=which, exists=lambda p: False,
                                             environ={}, diag=diag)
        return status, diag.get("reason")

    def test_not_installed(self):
        self.assertEqual(self.read(run=None, which=lambda name: None),
                         (None, pdisc.TAILSCALE_NOT_INSTALLED))

    def test_logged_out_exit_code_and_message(self):
        def run(*_a, **_k):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="Tailscale is stopped.\n")
        status, reason = self.read(run)
        self.assertIsNone(status)
        self.assertEqual(reason, "tailscale status failed (exit 1: Tailscale is stopped.)")

    def test_timeout(self):
        def run(*_a, **_k):
            raise subprocess.TimeoutExpired("tailscale", 4)
        self.assertIn("no answer within 4 s", self.read(run)[1])

    def test_running_backend_state(self):
        def run(*_a, **_k):
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({"BackendState": "Running",
                                                                          "Peer": {}}), stderr="")
        status, reason = self.read(run)
        self.assertEqual((status["BackendState"], reason), ("Running", "Running"))

    def test_collect_passes_the_reason_into_the_summary(self):
        def reader(diag=None):
            diag["reason"] = "tailscale status failed (exit 1)"
            return None
        _targets, summary = pdisc.collect_unicast_targets(read_tailscale=reader,
                                                          read_ifaces=lambda: None)
        self.assertEqual(summary["tailscale"]["reason"], "tailscale status failed (exit 1)")


class FakeState:
    def __init__(self, config):
        self.lock = threading.RLock()
        self.config = config
        self.active_peer = None
        self.hotkeys = []


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmp.name) / "config.json"
        config_schema.save_config(self.config_path, {
            "device_name": "BOOK-Viktor", "device_id": "aaaa1111", "port": 45781,
            "peers": [{"name": "Laptop-Bettina", "host": "192.168.8.18", "port": 45781}]})
        self.state = FakeState(config_schema.load_config(self.config_path))
        self.logs = []
        self.self_tests = []

        def self_test(frames=3):
            self.self_tests.append(frames)
            return {"ok": True, "steps": [{"step": "capture", "ok": True, "detail": "1920x1080"}],
                    "image": "data:image/jpeg;base64,AAAA", "width": 960, "height": 540}

        web_api._refs = {}
        web_api.init(istate=self.state,
                     save_config=lambda cfg: config_schema.save_config(self.config_path, cfg),
                     log=lambda level, msg: self.logs.append((level, msg)),
                     remote_preview_self_test=self_test)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        web_api._refs = {}
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        port = self.server.server_address[1]
        connection = HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request(method, path, body=json.dumps(body) if body is not None else None,
                           headers={"Content-Type": "application/json",
                                    "Origin": f"http://127.0.0.1:{port}"})
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_self_test_route_returns_the_runtime_report(self):
        status, body = self.request("POST", "/api/remote-preview/self-test", {"frames": 50})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["image"], "data:image/jpeg;base64,AAAA")
        self.assertEqual(self.self_tests, [10])  # bounded

    def test_self_test_route_without_runtime(self):
        web_api._refs.pop("remote_preview_self_test")
        status, _body = self.request("POST", "/api/remote-preview/self-test", {})
        self.assertEqual(status, 503)

    def test_scan_route_logs_how_it_searched(self):
        summary = {"tailscale": {"available": False, "asked": 0,
                                 "reason": pdisc.TAILSCALE_NOT_INSTALLED},
                   "vpn": {"interfaces": [], "asked": 0}}

        def fake_scan(port, timeout=0, local_device_id="", unicast_targets=(), searched=None):
            searched.extend(SEARCHED[:1])
            return []

        with mock.patch.object(web_api, "_unicast_targets", lambda: ([], summary)), \
                mock.patch.object(web_api, "_scan_broadcast", fake_scan):
            status, body = self.request("POST", "/api/peers/scan", {"timeout": 0.1})
        self.assertEqual(status, 200, body)
        infos = [msg for level, msg in self.logs if level == "INFO"]
        self.assertIn("peer scan on port 45781: WLAN (192.168.8.201/24): broadcast sent", infos)
        self.assertTrue(any("Tailscale not used: not installed" in m for m in infos))
        self.assertIn("peer scan found no FlowShift PC", infos)

    def test_saving_the_old_webgui_forward_form_stores_forward_peer(self):
        status, body = self.request("POST", "/api/settings", {"hotkeys": [
            {"action": "forward_endpoint:192.168.8.18:45781", "mods": 3, "key": 0x31,
             "label": "Forward to Laptop-Bettina"}]})
        self.assertEqual(status, 200, body)
        stored = json.loads(self.config_path.read_text("utf-8"))["hotkeys"]
        self.assertEqual(stored[0]["action"], "forward_peer:endpoint:192.168.8.18:45781")


class EventLogKindTests(unittest.TestCase):
    def test_remote_preview_lines_are_connection_lines(self):
        for text in ("remote_preview not requested on switch to device:x: the setting is off",
                     "remote_preview self-test capture ok 1920x1080 -> 960x540",
                     "remote_preview rejected reason=preview_unavailable peer=Laptop-Bettina"):
            self.assertEqual(event_log.classify_kind(text), "connection", text)


if __name__ == "__main__":
    unittest.main()
