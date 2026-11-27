"""The two separate clear buttons: "Clear pinned" and "Clear clipboard".

``ClipboardManager.clear_area`` empties only the pinned or only the not pinned
items of one profile; ``POST /api/clipboard/clear?scope=pinned|unpinned``
calls it. Without ``scope`` the route clears the whole history as before
(older WebGUIs). The persisted history is checked after a restart.
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_model as cm
from clipboard_runtime import ClipboardManager
import web_api


def settings():
    return cm.clipboard_settings({"clipboard": {"enabled": True}})


def _ids(items):
    return sorted(item["item_id"] for item in items)


def _manager(root):
    return ClipboardManager(root, "local-device", lambda _identity, _msg: None, settings)


class ManagerClearAreaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="flowshift-clear-area-")
        self.root = self._tmp.name
        self.manager = _manager(self.root)
        for index in range(5):
            self.manager.capture_text_all(["local"], f"text {index}")
        ids = [item["item_id"] for item in self.manager.list_items("local")]
        self.assertEqual(len(ids), 5)
        self.pinned = sorted(ids[:2])
        self.unpinned = sorted(ids[2:])
        for item_id in self.pinned:
            self.manager.set_pinned("local", item_id, True)
        # A second profile must stay untouched by every clear of "local".
        self.manager.capture_text_all(["device:peer-b"], "other profile")
        self.other = _ids(self.manager.list_items("device:peer-b"))
        self.assertEqual(len(self.other), 1)

    def tearDown(self):
        self.manager.shutdown()
        self._tmp.cleanup()

    def _restarted_ids(self, identity="local"):
        self.manager.shutdown()
        self.manager = _manager(self.root)
        return _ids(self.manager.list_items(identity))

    def test_clear_pinned_keeps_not_pinned_items_after_restart(self):
        self.assertEqual(sorted(self.manager.clear_area("local", True)), self.pinned)
        self.assertEqual(_ids(self.manager.list_items("local")), self.unpinned)
        self.assertEqual(self._restarted_ids(), self.unpinned)
        self.assertEqual(_ids(self.manager.list_items("device:peer-b")), self.other)

    def test_clear_unpinned_keeps_pinned_items_after_restart(self):
        self.assertEqual(sorted(self.manager.clear_area("local", False)), self.unpinned)
        remaining = self.manager.list_items("local")
        self.assertEqual(_ids(remaining), self.pinned)
        self.assertTrue(all(item.get("pinned") for item in remaining))
        self.assertEqual(self._restarted_ids(), self.pinned)
        self.assertEqual(_ids(self.manager.list_items("device:peer-b")), self.other)

    def test_both_buttons_one_after_the_other_empty_the_history(self):
        self.manager.clear_area("local", False)
        self.manager.clear_area("local", True)
        self.assertEqual(self.manager.list_items("local"), [])
        self.assertEqual(self._restarted_ids(), [])

    def test_empty_area_deletes_nothing(self):
        self.manager.clear_area("local", True)
        self.assertEqual(self.manager.clear_area("local", True), [])
        self.assertEqual(_ids(self.manager.list_items("local")), self.unpinned)


class FakeManager:
    def __init__(self):
        self.calls = []

    def clear_area(self, identity, pinned):
        self.calls.append(("clear_area", identity, pinned))
        return ["a", "b"] if pinned else ["c"]

    def clear(self, identity):
        self.calls.append(("clear", identity))
        return True


class ClearEndpointTests(unittest.TestCase):
    def setUp(self):
        self.manager = FakeManager()
        web_api._refs = {}
        web_api.init(clip_mgr=self.manager)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        web_api._refs = {}

    def request(self, path, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
        headers = dict(headers or {})
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Origin", f"http://127.0.0.1:{self.server.server_address[1]}")
        connection.request("POST", path, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_scope_pinned_clears_only_pinned_of_the_profile(self):
        status, body = self.request("/api/clipboard/clear?profile=device%3Apeer-a&scope=pinned")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "scope": "pinned", "deleted": 2})
        self.assertEqual(self.manager.calls, [("clear_area", "device:peer-a", True)])

    def test_scope_unpinned_clears_only_not_pinned_of_the_profile(self):
        status, body = self.request("/api/clipboard/clear?profile=local&scope=unpinned")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "scope": "unpinned", "deleted": 1})
        self.assertEqual(self.manager.calls, [("clear_area", "local", False)])

    def test_without_scope_old_callers_still_clear_everything(self):
        status, body = self.request("/api/clipboard/clear?profile=local")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(self.manager.calls, [("clear", "local")])

    def test_unknown_scope_is_rejected_and_nothing_is_deleted(self):
        for scope in ("all", "", "PINNED"):
            with self.subTest(scope=scope):
                status, _ = self.request(f"/api/clipboard/clear?profile=local&scope={scope}")
                self.assertEqual(status, 400)
        self.assertEqual(self.manager.calls, [])

    def test_foreign_origin_is_rejected(self):
        status, _ = self.request("/api/clipboard/clear?profile=local&scope=pinned",
                                 headers={"Origin": "https://example.invalid"})
        self.assertEqual(status, 403)
        self.assertEqual(self.manager.calls, [])


if __name__ == "__main__":
    unittest.main()
