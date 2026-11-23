"""Central request guard of the local Web API (Host, Origin, JSON type).

A web page open in the browser must not change settings, inject keys or run
Command Wheel actions through the loopback API. Every POST and DELETE passes
``web_api.request_rejection`` before any route runs; these tests send each
changing route the requests a foreign page could produce and prove that
nothing reaches the runtime, and that the WebGUI's own requests still work.
"""
import inspect
import json
import os
import queue
import re
import sys
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import web_api

# Every route literal in the POST/DELETE dispatcher, with one request path.
# A new changing route makes test_route_inventory_is_complete fail until it is
# listed here, and then the rejection tests below cover it automatically.
CHANGING_ROUTES = {
    "/api/update/check": "/api/update/check",
    "/api/update/download": "/api/update/download",
    "/api/update/install": "/api/update/install",
    "/api/update/settings": "/api/update/settings",
    "/api/update/development-access": "/api/update/development-access",
    "/api/actions/wheel": "/api/actions/wheel",
    "/api/actions/execute": "/api/actions/execute",
    "/api/forwarding/activate": "/api/forwarding/activate",
    "/api/forwarding/deactivate": "/api/forwarding/deactivate",
    "/api/forwarding/toggle": "/api/forwarding/toggle",
    "/api/settings": "/api/settings",
    "/api/display/layout": "/api/display/layout",
    "/api/peers/add": "/api/peers/add",
    "/api/peers/remove": "/api/peers/remove",
    "/api/peers/scan": "/api/peers/scan",
    "/api/peers/edit": "/api/peers/edit",
    "/api/peers/ping": "/api/peers/ping",
    "/api/auto-start": "/api/auto-start",
    "/api/webgui/config": "/api/webgui/config",
    "/api/restart": "/api/restart",
    "/api/runtime/restart": "/api/runtime/restart",
    "/api/shutdown": "/api/shutdown",
    "/api/runtime/stop": "/api/runtime/stop",
    "/api/diagnostics": "/api/diagnostics",
    "/api/overlay/show": "/api/overlay/show",
    "/api/overlay/probe": "/api/overlay/probe",
    "/api/overlay/hide": "/api/overlay/hide",
    "/api/overlay/ping": "/api/overlay/ping",
    "/api/inject/type": "/api/inject/type",
    "/api/inject/key": "/api/inject/key",
    "/api/clipboard/sync": "/api/clipboard/sync?profile=local",
    "/api/clipboard/clear": "/api/clipboard/clear?profile=local",
    "/api/clipboard/delete": "/api/clipboard/delete?profile=local",
    "/api/clipboard/item/": "/api/clipboard/item/a1?profile=local",
    "/paste": "/api/clipboard/item/a1/paste?profile=local",
    "/pin": "/api/clipboard/item/a1/pin?profile=local",
    "/copy-text": "/api/clipboard/item/a1/copy-text?profile=local",
    "/request": "/api/clipboard/item/a1/request?profile=local",
    "/api/": "/api/not-a-route",
}

# GET routes only read. A new GET route fails test_get_routes_only_read until
# it is checked and listed here (or moved to POST if it changes something).
READ_ONLY_GET_ROUTES = {
    "/api/", "/api/actions", "/api/auto-start", "/api/clipboard/item/",
    "/api/clipboard/items", "/api/clipboard/preview", "/api/clipboard/profiles",
    "/api/clipboard/progress", "/api/clipboard/status", "/api/clipboard/thumbnail/",
    "/api/display/layout", "/api/events", "/api/hotkeys", "/api/peers",
    "/api/settings", "/api/status", "/api/update/status", "/api/webgui/config",
}

# A body each route would act on if the guard let it through.
BODY = {"text": "attrappe-geheimtext", "vk": 65, "action_id": "copy",
        "pages": [["copy"]], "profile": "local", "item_ids": ["a1"], "enabled": True}


class _Recorder:
    """Stands in for every runtime reference and records any use."""

    def __init__(self):
        self.calls = []
        self.lock = threading.RLock()
        self.config = {}
        self.active_peer = None
        self.peers = {}
        self.inject_queue = queue.Queue()

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
            return {"ok": True}
        return record


class _Refs(dict):
    def __init__(self, recorder, logs):
        super().__init__()
        self.recorder = recorder
        self.logs = logs
        self.used = []

    def get(self, key, default=None):
        if key == "log":
            return lambda level, msg: self.logs.append((level, msg))
        self.used.append(key)
        if key in ("istate", "clip_mgr"):
            return self.recorder
        return getattr(self.recorder, key)


def _literals(function):
    return set(re.findall(r'"(/api/[^"]*|/[a-z-]+)"', inspect.getsource(function)))


class RequestGuardTests(unittest.TestCase):
    def setUp(self):
        self.recorder = _Recorder()
        self.logs = []
        self.saved_refs = web_api._refs
        web_api._refs = _Refs(self.recorder, self.logs)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        web_api._refs = self.saved_refs

    def send(self, method, path, headers, body=BODY):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        connection.putrequest(method, path, skip_host="Host" in headers)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(len(payload or b"")))
        connection.endheaders(payload)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8") or "{}")
        connection.close()
        return response.status, data

    def own(self, **extra):
        headers = {"Origin": f"http://127.0.0.1:{self.port}",
                   "Content-Type": "application/json"}
        headers.update(extra)
        return headers

    def assert_nothing_reached_runtime(self):
        self.assertEqual(web_api._refs.used, [])
        self.assertEqual(self.recorder.calls, [])
        self.assertTrue(self.recorder.inject_queue.empty())

    def test_route_inventory_is_complete(self):
        handler = web_api.make_api_handler()
        self.assertEqual(_literals(handler._route_post), set(CHANGING_ROUTES))

    def test_get_routes_only_read(self):
        handler = web_api.make_api_handler()
        self.assertEqual({r for r in _literals(handler.do_GET) if r.startswith("/api/")},
                         READ_ONLY_GET_ROUTES)

    def test_only_guarded_methods_exist(self):
        handler = web_api.make_api_handler()
        methods = {name for name in dir(handler) if name.startswith("do_")}
        self.assertEqual(methods, {"do_GET", "do_POST", "do_DELETE", "do_OPTIONS"})
        for name in ("do_POST", "do_DELETE", "do_GET"):
            self.assertIn("_guard_request", inspect.getsource(getattr(handler, name)))

    def test_every_changing_route_rejects_foreign_requests(self):
        cases = [
            ("foreign origin", {"Origin": "https://example.invalid",
                                "Content-Type": "application/json"}, 403, "origin_not_allowed"),
            ("foreign origin on own port", {"Origin": f"http://attacker.invalid:{self.port}",
                                            "Content-Type": "application/json"},
             403, "origin_not_allowed"),
            ("null origin", {"Origin": "null", "Content-Type": "application/json"},
             403, "origin_not_allowed"),
            ("no origin", {"Content-Type": "application/json"}, 403, "origin_required"),
            ("foreign host (DNS rebinding)", self.own(Host=f"attacker.invalid:{self.port}"),
             403, "host_not_allowed"),
            ("foreign port in host", self.own(Host="127.0.0.1:1"), 403, "host_not_allowed"),
            ("simple form post", self.own(**{"Content-Type": "text/plain"}),
             415, "json_required"),
            ("form urlencoded", self.own(**{"Content-Type":
                                           "application/x-www-form-urlencoded"}),
             415, "json_required"),
            ("no content type", {"Origin": f"http://127.0.0.1:{self.port}"},
             415, "json_required"),
        ]
        for route, path in CHANGING_ROUTES.items():
            for method in ("POST", "DELETE"):
                for label, headers, status, code in cases:
                    with self.subTest(route=route, method=method, case=label):
                        got, body = self.send(method, path, headers)
                        self.assertEqual(got, status, body)
                        self.assertEqual(body["error_code"], code)
                        self.assertIs(body["ok"], False)
        self.assert_nothing_reached_runtime()

    def test_rejection_log_names_reason_but_no_content(self):
        status, _ = self.send("POST", "/api/inject/type?profile=attrappe-query",
                              {"Origin": "https://example.invalid",
                               "Content-Type": "application/json"})
        self.assertEqual(status, 403)
        warnings = [msg for level, msg in self.logs if level == "WARN"]
        self.assertEqual(warnings, ["web_api rejected POST /api/inject/type "
                                    "reason=origin_not_allowed"])
        self.assertNotIn("attrappe", " ".join(warnings))

    def test_get_api_requires_local_host_but_no_origin(self):
        status, body = self.send("GET", "/api/status", {"Host": f"attacker.invalid:{self.port}"},
                                 body=None)
        self.assertEqual((status, body["error_code"]), (403, "host_not_allowed"))
        self.assertEqual(web_api._refs.used, [])
        status, _ = self.send("GET", "/api/status", {}, body=None)
        self.assertEqual(status, 200)

    def test_webgui_requests_pass(self):
        allowed = [
            self.own(),
            self.own(Host=f"localhost:{self.port}"),
            self.own(Origin=f"http://localhost:{self.port}"),
            self.own(Origin="http://localhost:5173"),  # Vite dev server
            self.own(**{"Content-Type": "application/json; charset=utf-8"}),
        ]
        for headers in allowed:
            with self.subTest(headers=headers):
                self.recorder.inject_queue = queue.Queue()
                status, body = self.send("POST", "/api/inject/key", headers)
                self.assertEqual(status, 200, body)
                self.assertEqual(self.recorder.inject_queue.get_nowait(),
                                 {"type": "key", "code": 65})
        status, body = self.send("DELETE", "/api/clipboard/item/a1?profile=local", self.own(),
                                 body=None)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.recorder.calls, ["delete_item"])

    def test_request_rejection_unit(self):
        class Headers(dict):
            def get_content_type(self):
                return self.get("Content-Type", "text/plain").split(";")[0].strip()

        port = 5000
        ok = Headers({"Host": "127.0.0.1:5000", "Origin": "http://127.0.0.1:5000",
                      "Content-Type": "application/json"})
        self.assertIsNone(web_api.request_rejection("POST", "/api/settings", ok, port))
        self.assertIsNone(web_api.request_rejection(
            "GET", "/index.html", Headers({"Host": "x.invalid"}), port))
        self.assertEqual(web_api.request_rejection(
            "POST", "/api/settings", Headers(ok, **{"Transfer-Encoding": "chunked"}), port)[1],
            "chunked_not_supported")
        self.assertEqual(web_api.request_rejection(
            "POST", "/api/settings", Headers(ok, **{"Content-Length": "-1"}), port)[1],
            "invalid_content_length")
        self.assertEqual(web_api.request_rejection(
            "POST", "/api/settings", Headers(ok, Host="user@127.0.0.1:5000"), port)[1],
            "host_not_allowed")
        self.assertEqual(web_api.request_rejection(
            "POST", "/api/settings", Headers(ok, Origin="https://127.0.0.1:5000"), port)[1],
            "origin_not_allowed")


class NativeWheelThroughGuardTests(unittest.TestCase):
    """The native Command Wheel's own request passes the real guard."""

    def setUp(self):
        import test_overlay_native_wheel as wheel_tests
        self.executed = []
        self.saved_refs = web_api._refs
        web_api._refs = {"execute_action": lambda action, context: (
            self.executed.append((action["id"], context)) or {"ok": True})}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = wheel_tests._native_host()
        self.host.args.url = f"http://127.0.0.1:{self.server.server_address[1]}/overlay.html"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        web_api._refs = self.saved_refs

    def test_wheel_click_runs_action(self):
        import io
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()):
            result = self.host._native_execute("copy")
        self.assertIs(result["ok"], True, result)
        self.assertEqual(self.executed, [("copy", {"source": "command_wheel"})])


if __name__ == "__main__":
    unittest.main()
