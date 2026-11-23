"""Deleting several selected clipboard items at once (no confirmation).

Covers the store, the manager and ``POST /api/clipboard/delete`` and checks
that the persisted history survives a restart (a new store/manager instance
reading the same ``index.json``).
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
import clipboard_store as cs
from clipboard_runtime import ClipboardManager
import web_api


def settings():
    return cm.clipboard_settings({"clipboard": {"enabled": True}})


def _ids(items):
    return [item["item_id"] for item in items]


class StoreDeleteItemsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="flowshift-delete-")
        self.root = self._tmp.name
        self.store = cs.ClipboardStore(self.root, "local")
        self.items = []
        for index in range(4):
            item, _ = self.store.add_item(cm.make_text_item(f"entry {index}", seq=index),
                                          data=f"entry {index}".encode())
            self.items.append(item)

    def tearDown(self):
        self._tmp.cleanup()

    def test_deletes_selected_items_and_keeps_the_rest_after_restart(self):
        doomed = [self.items[0]["item_id"], self.items[2]["item_id"]]
        deleted = self.store.delete_items(doomed + ["unknown", doomed[0]])
        self.assertEqual(deleted, doomed)
        expected = {self.items[1]["item_id"], self.items[3]["item_id"]}
        self.assertEqual(set(_ids(self.store.list_items())), expected)
        reopened = cs.ClipboardStore(self.root, "local")
        self.assertEqual(set(_ids(reopened.list_items())), expected)

    def test_deleting_every_item_leaves_an_empty_history_after_restart(self):
        self.store.set_pinned(self.items[1]["item_id"], True)
        deleted = self.store.delete_items(_ids(self.items))
        self.assertEqual(len(deleted), 4)
        self.assertEqual(self.store.list_items(), [])
        self.assertEqual(cs.ClipboardStore(self.root, "local").list_items(), [])

    def test_pinned_item_is_deleted_when_selected(self):
        pinned_id = self.items[3]["item_id"]
        self.store.set_pinned(pinned_id, True)
        self.assertEqual(self.store.delete_items([pinned_id]), [pinned_id])
        self.assertNotIn(pinned_id, _ids(cs.ClipboardStore(self.root, "local").list_items()))

    def test_unknown_ids_change_nothing(self):
        revision = self.store.revision
        self.assertEqual(self.store.delete_items(["nope", "", None]), [])
        self.assertEqual(self.store.revision, revision)
        self.assertEqual(len(self.store.list_items()), 4)

    def test_current_item_is_cleared_but_nothing_else_changes(self):
        current, _ = self.store.add_item(cm.make_text_item("current", seq=9),
                                         data=b"current", make_current=True)
        self.store.delete_items([current["item_id"]])
        self.assertIsNone(self.store.current_item_id)
        self.assertIsNone(cs.ClipboardStore(self.root, "local").current_item_id)

    def test_single_delete_still_works(self):
        self.assertTrue(self.store.delete_item(self.items[0]["item_id"]))
        self.assertFalse(self.store.delete_item(self.items[0]["item_id"]))


class ManagerDeleteItemsTests(unittest.TestCase):
    def test_manager_deletes_selected_items_and_history_survives_restart(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-delete-mgr-") as root:
            manager = ClipboardManager(root, "local-device", lambda _identity, _msg: None,
                                       settings)
            try:
                for index in range(3):
                    manager.capture_text_all(["local"], f"text {index}")
                ids = _ids(manager.list_items("local"))
                self.assertEqual(len(ids), 3)
                deleted = manager.delete_items("local", ids[:2])
                self.assertEqual(deleted, ids[:2])
                self.assertEqual(_ids(manager.list_items("local")), ids[2:])
            finally:
                manager.shutdown()
            restarted = ClipboardManager(root, "local-device", lambda _identity, _msg: None,
                                         settings)
            try:
                self.assertEqual(_ids(restarted.list_items("local")), ids[2:])
                self.assertEqual(restarted.delete_items("local", ids[2:]), ids[2:])
            finally:
                restarted.shutdown()
            again = ClipboardManager(root, "local-device", lambda _identity, _msg: None,
                                     settings)
            try:
                self.assertEqual(again.list_items("local"), [])
            finally:
                again.shutdown()

    def test_select_all_with_pinned_items_empties_history_after_restart(self):
        # The WebGUI "All" passes every id, Recent and Pinned; Delete removes them all.
        with tempfile.TemporaryDirectory(prefix="flowshift-delete-all-") as root:
            manager = ClipboardManager(root, "local-device", lambda _identity, _msg: None,
                                       settings)
            try:
                for index in range(4):
                    manager.capture_text_all(["local"], f"text {index}")
                ids = _ids(manager.list_items("local"))
                manager.set_pinned("local", ids[0], True)
                manager.set_pinned("local", ids[2], True)
                pinned = [item["item_id"] for item in manager.list_items("local")
                          if item.get("pinned")]
                self.assertEqual(sorted(pinned), sorted([ids[0], ids[2]]))
                self.assertEqual(sorted(manager.delete_items("local", ids)), sorted(ids))
                self.assertEqual(manager.list_items("local"), [])
            finally:
                manager.shutdown()
            restarted = ClipboardManager(root, "local-device", lambda _identity, _msg: None,
                                         settings)
            try:
                self.assertEqual(restarted.list_items("local"), [])
            finally:
                restarted.shutdown()


class FakeManager:
    def __init__(self):
        self.calls = []

    def delete_items(self, identity, item_ids):
        self.calls.append((identity, list(item_ids)))
        return [item_id for item_id in item_ids if item_id != "missing"]


class DeleteEndpointTests(unittest.TestCase):
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

    def request(self, body, headers=None, path="/api/clipboard/delete?profile=local"):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
        payload = json.dumps(body).encode("utf-8")
        headers = dict(headers or {})
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Origin", f"http://127.0.0.1:{self.server.server_address[1]}")
        connection.request("POST", path, body=payload, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_deletes_selected_ids_in_one_request(self):
        status, body = self.request({"item_ids": ["a", "b", "missing"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"deleted": 2, "item_ids": ["a", "b"]})
        self.assertEqual(self.manager.calls, [("local", ["a", "b", "missing"])])

    def test_rejects_invalid_bodies(self):
        for body in ({}, {"item_ids": "a"}, {"item_ids": [1]}, {"item_ids": [""]}):
            with self.subTest(body=body):
                status, _ = self.request(body)
                self.assertEqual(status, 400)
        self.assertEqual(self.manager.calls, [])

    def test_rejects_foreign_origin(self):
        status, _ = self.request({"item_ids": ["a"]},
                                 headers={"Origin": "https://example.invalid"})
        self.assertEqual(status, 403)
        status, _ = self.request({"item_ids": ["a"]}, headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertEqual(self.manager.calls, [])

    def test_missing_profile_falls_back_to_local(self):
        status, body = self.request({"item_ids": ["a"]}, path="/api/clipboard/delete")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.manager.calls, [("local", ["a"])])


if __name__ == "__main__":
    unittest.main()
