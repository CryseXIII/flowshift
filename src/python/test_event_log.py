"""Event Log page: recent log lines, ``GET /api/logs`` and the log folder.

Viktor 26.11.: an edge switch was refused with ``invalid_target`` and that
only stood in ``flowshift.log``; the Event Log page showed nothing. Lines from
INFO upwards are now kept (last 500) and published as SSE ``log`` events.
"""
from __future__ import annotations

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import event_log
import web_api
from test_tray_remote_preview import _import_tray


class LogBufferTests(unittest.TestCase):
    def test_keeps_info_warn_error_and_drops_debug_and_web_api_access_lines(self):
        buf = event_log.LogBuffer()
        self.assertIsNone(buf.record("DEBUG", "outbound connect failed to PC2"))
        self.assertIsNone(buf.record("INFO", 'web_api: "POST /api/peers/add HTTP/1.1" 200 -'))
        info = buf.record("INFO", "edge-watch trigger north -> Laptop-Bettina", at=100.0)
        warn = buf.record("WARN", "edge_enter rejected: invalid_target", at=101.0)
        error = buf.record("ERROR", "web_api POST /api/settings: boom", at=102.0)
        self.assertEqual([e["level"] for e in buf.entries()], ["INFO", "WARN", "ERROR"])
        self.assertEqual(warn, {"seq": 2, "time": 101.0, "level": "WARN",
                                "text": "edge_enter rejected: invalid_target"})
        self.assertEqual(info["seq"], 1)
        self.assertEqual(error["seq"], 3)

    def test_levels_are_normalized(self):
        buf = event_log.LogBuffer()
        buf.record("warning", "a")
        buf.record("CRITICAL", "b")
        buf.record("OK", "c")
        self.assertEqual([e["level"] for e in buf.entries()], ["WARN", "ERROR", "INFO"])

    def test_holds_only_the_last_500_in_order(self):
        buf = event_log.LogBuffer()
        self.assertEqual(buf.capacity, 500)
        for n in range(620):
            buf.record("INFO", f"line {n}")
        entries = buf.entries()
        self.assertEqual(len(entries), 500)
        self.assertEqual(entries[0]["text"], "line 120")
        self.assertEqual(entries[-1]["text"], "line 619")
        self.assertEqual(entries[-1]["seq"], 620)

    def test_after_returns_only_newer_entries(self):
        buf = event_log.LogBuffer()
        for n in range(5):
            buf.record("WARN", f"w{n}")
        self.assertEqual([e["text"] for e in buf.entries(after=3)], ["w3", "w4"])
        self.assertEqual(buf.entries(after=5), [])

    def test_long_text_is_cut(self):
        buf = event_log.LogBuffer()
        entry = buf.record("ERROR", "x" * (event_log.MAX_TEXT_CHARS + 50))
        self.assertTrue(entry["text"].endswith(" [...]"))
        self.assertEqual(len(entry["text"]), event_log.MAX_TEXT_CHARS + len(" [...]"))

    def test_returned_entries_are_copies(self):
        buf = event_log.LogBuffer()
        buf.record("INFO", "a")
        buf.entries()[0]["text"] = "changed"
        self.assertEqual(buf.entries()[0]["text"], "a")


class EventBusLogTests(unittest.TestCase):
    def setUp(self):
        self.old_bus = web_api._event_bus
        web_api._event_bus = web_api.EventBus()

    def tearDown(self):
        web_api._event_bus = self.old_bus

    def test_log_entry_reaches_subscribers_but_not_the_replay_history(self):
        sub = web_api._event_bus.subscribe()
        web_api.publish_log_entry({"seq": 7, "time": 1.0, "level": "WARN",
                                   "text": "edge_enter rejected: invalid_target"})
        self.assertEqual(sub.get_nowait(), {"type": "log", "seq": 7, "time": 1.0,
                                            "level": "WARN",
                                            "text": "edge_enter rejected: invalid_target"})
        # A page that opens later gets the line from /api/logs, not twice.
        late = web_api._event_bus.subscribe()
        self.assertTrue(late.empty())
        web_api.publish_event({"type": "status_update"})
        self.assertEqual(late.get_nowait(), {"type": "status_update"})

    def test_none_entry_is_not_published(self):
        sub = web_api._event_bus.subscribe()
        web_api.publish_log_entry(None)
        self.assertTrue(sub.empty())


class LogsApiTests(unittest.TestCase):
    def setUp(self):
        self.buffer = event_log.LogBuffer()
        self.buffer.record("INFO", "edge_enter sent device:a -> device:b north->south", at=10.0)
        self.buffer.record("WARN", "edge_enter rejected: invalid_target", at=11.0)
        self.buffer.record("DEBUG", "not shown")
        self.opened = []
        web_api._refs = {}
        web_api.init(log=lambda level, msg: None, log_buffer=self.buffer,
                     log_dir=r"C:\ProgramData\FlowShift\logs",
                     open_log_folder=lambda: self.opened.append(True) or True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        web_api._refs = {}

    def request(self, method, path, body=None, origin=True):
        port = self.server.server_address[1]
        connection = HTTPConnection("127.0.0.1", port, timeout=2)
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{port}"
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_get_logs_returns_buffer_and_folder(self):
        status, body = self.request("GET", "/api/logs")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["capacity"], 500)
        self.assertEqual(body["log_folder"], r"C:\ProgramData\FlowShift\logs")
        self.assertEqual(body["log_file"], "flowshift.log")
        self.assertEqual(body["entries"], [
            {"seq": 1, "time": 10.0, "level": "INFO",
             "text": "edge_enter sent device:a -> device:b north->south"},
            {"seq": 2, "time": 11.0, "level": "WARN",
             "text": "edge_enter rejected: invalid_target"},
        ])

    def test_get_logs_after(self):
        status, body = self.request("GET", "/api/logs?after=1")
        self.assertEqual(status, 200)
        self.assertEqual([e["seq"] for e in body["entries"]], [2])
        status, body = self.request("GET", "/api/logs?after=oops")
        self.assertEqual([e["seq"] for e in body["entries"]], [1, 2])

    def test_open_folder_runs_the_opener_and_returns_the_path(self):
        status, body = self.request("POST", "/api/logs/open-folder", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["opened"], True)
        self.assertEqual(body["log_folder"], r"C:\ProgramData\FlowShift\logs")
        self.assertEqual(self.opened, [True])

    def test_open_folder_needs_the_webgui_origin(self):
        status, body = self.request("POST", "/api/logs/open-folder", {}, origin=False)
        self.assertEqual(status, 403, body)
        self.assertEqual(self.opened, [])

    def test_open_folder_failure_still_shows_the_path(self):
        def fail():
            raise OSError("no explorer")
        web_api._refs["open_log_folder"] = fail
        status, body = self.request("POST", "/api/logs/open-folder", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["opened"], False)
        self.assertEqual(body["log_folder"], r"C:\ProgramData\FlowShift\logs")


class TrayLogPublishesTests(unittest.TestCase):
    """The productive ``tray.log`` feeds the buffer and the open pages."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="fs-event-log-", ignore_cleanup_errors=True)
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

    def test_warn_line_goes_to_file_buffer_and_sse(self):
        buffer = event_log.LogBuffer()
        bus = web_api.EventBus()
        sub = bus.subscribe()
        path = os.path.join(self.tmp.name, "flowshift.log")
        with mock.patch.object(event_log, "BUFFER", buffer), \
                mock.patch.object(web_api, "_event_bus", bus), \
                mock.patch.object(self.tray, "LOG_FILE", path), \
                mock.patch("builtins.print"):
            self.tray.log("DEBUG", "outbound connect failed to PC2")
            self.tray.log("WARN", "edge_enter rejected: invalid_target")
        self.assertEqual([(e["level"], e["text"]) for e in buffer.entries()],
                         [("WARN", "edge_enter rejected: invalid_target")])
        event = sub.get_nowait()
        self.assertEqual((event["type"], event["level"], event["text"]),
                         ("log", "WARN", "edge_enter rejected: invalid_target"))
        self.assertTrue(sub.empty())
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("[DEBUG] outbound connect failed to PC2", text)
        self.assertIn("[WARN] edge_enter rejected: invalid_target", text)


if __name__ == "__main__":
    unittest.main()
