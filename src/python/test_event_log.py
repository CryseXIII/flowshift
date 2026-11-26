"""Event Log entries carry level, kind and device for the WebGUI filters.

Viktor 26.11.: "der event log sollte auch einen gescheiten filter haben"
(level, kind, device, text, time range). The filters run in the WebGUI over
the fields set here, so these tests check the fields of real runtime lines.
"""
import json
import os
import sys
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import event_log


class Clock:
    def __init__(self, now=1_764_150_000.0):
        self.now = now

    def __call__(self):
        return self.now


DEVICES = [("Viktor-Laptop", ["192.0.2.20", "abcd1234ef"]), ("Buero", ["192.0.2.30", ""])]


class FieldsTest(unittest.TestCase):
    def setUp(self):
        self.log = event_log.EventLog(clock=Clock())
        self.log.set_device_source(lambda: DEVICES)

    def test_levels(self):
        self.assertEqual(self.log.record("INFO", "peer Buero connected")["level"], "info")
        self.assertEqual(self.log.record("WARN", "peer Buero unreachable")["level"], "warning")
        self.assertEqual(self.log.record("ERROR", "web_api GET /api/status: boom")["level"], "error")
        self.assertEqual(self.log.record("OK", "scheduled task registered")["level"], "info")
        self.assertIsNone(self.log.record("DEBUG", "mouse batch"))
        self.assertEqual(len(self.log.snapshot()["entries"]), 4)

    def test_kinds_of_runtime_lines(self):
        cases = {
            "peer Viktor-Laptop connected from 192.0.2.20": "connection",
            "discover_reply from 192.0.2.30 ignored": "connection",
            "edge switch to Buero refused: foreign target": "edge",
            "hotkey forward to Buero failed": "edge",
            "clipboard transfer from Buero failed: checksum": "clipboard",
            "peer update offer from Buero: 0.7.1-dev.9": "update",
            "update check failed: timeout": "update",
            "web_api rejected POST /api/settings reason=origin": "web_api",
            "tray notification failed": "other",
        }
        for text, kind in cases.items():
            self.assertEqual(self.log.record("INFO", text)["kind"], kind, text)

    def test_device_by_name_address_or_id(self):
        self.assertEqual(self.log.record("INFO", "peer viktor-laptop connected")["device"], "Viktor-Laptop")
        self.assertEqual(self.log.record("WARN", "ping 192.0.2.30:45781 timed out")["device"], "Buero")
        self.assertEqual(self.log.record("INFO", "hello device_id=abcd1234ef")["device"], "Viktor-Laptop")
        self.assertEqual(self.log.record("INFO", "ping 192.0.2.3 timed out")["device"], "", "no prefix match")
        self.assertEqual(self.log.record("INFO", "Bueroklammer")["device"], "", "whole names only")

    def test_traceback_goes_to_detail(self):
        entry = self.log.record("ERROR", "web_api POST /api/peers/add: boom\nTraceback (most recent call last):\n  x")
        self.assertEqual(entry["text"], "web_api POST /api/peers/add: boom")
        self.assertTrue(entry["detail"].startswith("Traceback"))

    def test_user_folder_is_not_exposed(self):
        entry = self.log.record("ERROR", "clipboard write failed: C:\\Users\\viktor\\Desktop\\a.txt\n"
                                         "  File \"C:/Users/viktor/x.py\"")
        self.assertEqual(entry["text"], "clipboard write failed: %USERPROFILE%\\Desktop\\a.txt")
        self.assertNotIn("viktor", entry["detail"])

    def test_time_fields(self):
        entry = self.log.record("INFO", "peer Buero connected")
        self.assertEqual(entry["ts"], 1_764_150_000.0)
        self.assertEqual(entry["at"], "2025-11-26T09:40:00Z")

    def test_failing_device_source_leaves_device_empty(self):
        log = event_log.EventLog(clock=Clock())
        log.set_device_source(lambda: 1 / 0)
        self.assertEqual(log.record("INFO", "peer Buero connected")["device"], "")


class BufferTest(unittest.TestCase):
    def test_after_and_ring_limit(self):
        log = event_log.EventLog(max_entries=3, clock=Clock())
        for n in range(5):
            log.record("INFO", f"line {n}")
        snap = log.snapshot()
        self.assertEqual([e["text"] for e in snap["entries"]], ["line 2", "line 3", "line 4"])
        self.assertEqual(snap["last_id"], 5)
        self.assertEqual([e["id"] for e in log.snapshot(after=4)["entries"]], [5])
        self.assertEqual(log.snapshot(after="x")["last_id"], 5)
        self.assertEqual(len(log.snapshot(limit=1)["entries"]), 1)
        self.assertEqual(snap["levels"], ["info", "warning", "error"])
        self.assertIn("web_api", snap["kinds"])

    def test_module_record_never_raises(self):
        self.assertIsNone(event_log.record("INFO", object.__new__(BrokenStr)))


class BrokenStr:
    def __str__(self):
        raise RuntimeError("no text")


class TrayLogFeedsEventLogTest(unittest.TestCase):
    def test_tray_log_lines_reach_the_api(self):
        from test_autostart import _import_tray
        tray = _import_tray()
        import web_api
        before = event_log.snapshot()["last_id"]
        tray.log("WARN", "peer Buero unreachable (attrappe)")
        tray.log("DEBUG", "not kept")
        saved_refs = web_api._refs
        web_api._refs = {}
        web_api.init()
        server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            connection.putrequest("GET", f"/api/event-log?after={before}", skip_host=True)
            connection.putheader("Host", f"127.0.0.1:{port}")
            connection.endheaders()
            response = connection.getresponse()
            data = json.loads(response.read().decode("utf-8"))
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
            web_api._refs = saved_refs
        self.assertEqual(response.status, 200)
        texts = [(e["level"], e["kind"], e["text"]) for e in data["entries"]]
        self.assertEqual(texts, [("warning", "connection", "peer Buero unreachable (attrappe)")])


if __name__ == "__main__":
    unittest.main()
