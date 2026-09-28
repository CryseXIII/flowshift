"""Offline HTTP tests for the public clipboard card contract."""
from __future__ import annotations

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_model as cbm
import web_api


def _text_item(item_id="txt-1"):
    return {
        "item_id": item_id,
        "kind": "text",
        "mime": "text/plain",
        "size": 11,
        "created_at": 1700000000.0,
        "seq": 1,
        "display_name": "hello.txt",
        "preview_text": "hello world",
        "file_count": 0,
        "directory_count": 0,
        "total_file_size": 0,
        "pinned": False,
        "available": True,
        "payload_state": "cached",
        "origin": {"device_id": "dev", "event_id": item_id, "captured_at": 1700000000.0},
        "payload": {"content_sha256": "a" * 64, "encoding": "raw",
                    "sha256": "a" * 64, "size": 11},
        "providers": [],
        "metadata": {},
    }


def _batch_item(item_id="batch-1"):
    return {
        "item_id": item_id,
        "kind": "file_batch",
        "mime": "application/zip",
        "size": 300,
        "created_at": 1700000001.0,
        "seq": 2,
        "display_name": "2 Dateien",
        "preview_text": "Folder/a.txt\nFolder/b.txt",
        "file_count": 2,
        "directory_count": 1,
        "total_file_size": 300,
        "pinned": False,
        "available": True,
        "payload_state": "source_available",
        "origin": {"device_id": "dev", "event_id": item_id, "captured_at": 1700000001.0},
        "payload": {"content_sha256": None, "encoding": "deterministic_zip",
                    "sha256": None, "size": None},
        "providers": [],
        "metadata": {},
        # Private local fields that must never reach HTTP clients.
        "files": [{"abspath": "C:\\secret\\Folder\\a.txt", "rel": "Folder/a.txt",
                   "type": "file", "size": 100}],
        "source_paths": ["C:\\secret\\Folder"],
        "base": "C:\\secret",
        "cache_path": "C:\\secret\\cache\\x",
        "batch_manifest": {
            "schema_version": 2,
            "protocol_major": 2,
            "item_id": item_id,
            "item_revision": 0,
            "manifest_digest": "b" * 64,
            "total_size": 300,
            "file_count": 2,
            "directory_count": 1,
            "entries": [
                {"index": 0, "path": "Folder", "type": "directory", "size": 0,
                 "mtime_ns": 0,
                 "source_fingerprint": {"version": 1, "size": 0, "mtime_ns": 0,
                                        "strength": "weak", "device": 123, "inode": 456},
                 "hash_state": "unhashed", "sha256": None},
                {"index": 1, "path": "Folder/a.txt", "type": "file", "size": 100,
                 "mtime_ns": 0,
                 "source_fingerprint": {"version": 1, "size": 100, "mtime_ns": 0,
                                        "strength": "weak", "device": 123, "inode": 457},
                 "hash_state": "unhashed", "sha256": None},
                {"index": 2, "path": "Folder/b.txt", "type": "file", "size": 200,
                 "mtime_ns": 0,
                 "source_fingerprint": {"version": 1, "size": 200, "mtime_ns": 0,
                                        "strength": "weak"},
                 "hash_state": "unhashed", "sha256": None},
            ],
        },
    }


class FakeStore:
    def __init__(self, items, current_item_id=None):
        self._items = [dict(it) for it in items]
        self.current_item_id = current_item_id

    def total_size(self):
        return sum(int(it.get("size", 0) or 0) for it in self._items)

    def get_item(self, item_id):
        for it in self._items:
            if it.get("item_id") == item_id:
                return dict(it)
        return None

    def get_data(self, item_id):
        return None


class FakeManager:
    def __init__(self, items, current_item_id=None):
        self._store = FakeStore(items, current_item_id)

    def list_items(self, ident):
        return [dict(it) for it in self._store._items]

    def store(self, ident):
        return self._store

    def item_kind(self, ident, item_id):
        item = self._store.get_item(item_id)
        return item.get("kind") if item else None

    def get_text(self, ident, item_id):
        return None

    def get_html(self, ident, item_id):
        return None


class ClipboardApiTests(unittest.TestCase):
    def setUp(self):
        self.items = [_text_item(), _batch_item()]
        web_api._refs = {}
        web_api.init(clip_mgr=FakeManager(self.items, current_item_id="batch-1"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        web_api._refs = {}

    def request(self, method, path):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
        connection.request(method, path)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_list_strips_private_paths_and_marks_current(self):
        status, body = self.request("GET", "/api/clipboard/items?profile=device%3Apeer-a")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["current_item_id"], "batch-1")
        self.assertEqual(len(body["items"]), 2)
        dumped = json.dumps(body)
        for banned in ("abspath", "source_paths", "C:\\\\secret", "cache_path",
                       "source_fingerprint", "inode", "\"files\""):
            self.assertNotIn(banned, dumped)
        by_id = {item["item_id"]: item for item in body["items"]}
        self.assertTrue(by_id["batch-1"]["is_current"])
        self.assertFalse(by_id["txt-1"]["is_current"])
        card = by_id["batch-1"]["file_card"]
        self.assertEqual(card["root_names"], ["Folder"])
        self.assertEqual(card["file_names"], ["a.txt", "b.txt"])
        self.assertEqual(card["relative_paths"], ["Folder/a.txt", "Folder/b.txt"])
        self.assertEqual(card["total_files"], 2)
        self.assertFalse(card["truncated"])
        self.assertIsNone(by_id["txt-1"]["file_card"])

    def test_detail_returns_bounded_relative_manifest(self):
        status, body = self.request(
            "GET", "/api/clipboard/item/batch-1?profile=device%3Apeer-a&manifest_limit=1")
        self.assertEqual(status, 200, body)
        manifest = body["file_manifest"]
        self.assertEqual(len(manifest["entries"]), 1)
        self.assertTrue(manifest["truncated"])
        self.assertEqual(manifest["total_files"], 2)
        self.assertEqual(set(manifest["entries"][0].keys()), {"path", "type", "size"})
        dumped = json.dumps(body)
        self.assertNotIn("abspath", dumped)
        self.assertNotIn("source_fingerprint", dumped)
        status, body = self.request(
            "GET", "/api/clipboard/item/txt-1?profile=device%3Apeer-a")
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["file_manifest"])
        self.assertEqual(body["item"]["item_id"], "txt-1")

    def test_profiles_endpoint_lists_local_first(self):
        status, body = self.request("GET", "/api/clipboard/profiles")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["profiles"][0]["identity"], "local")
        self.assertEqual(body["profiles"][0]["label"], "This PC")

    def test_items_without_profile_falls_back_to_local(self):
        status, body = self.request("GET", "/api/clipboard/items")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual(body["current_item_id"], "batch-1")

    def test_public_card_rejects_invalid_items(self):
        with self.assertRaises(ValueError):
            cbm.public_card_item({"item_id": "bad id!", "kind": "text"})
        with self.assertRaises(ValueError):
            cbm.public_card_item({"item_id": "ok-1", "kind": "bogus"})


if __name__ == "__main__":
    unittest.main()
