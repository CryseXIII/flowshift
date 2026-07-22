"""Phase 2 clipboard schema, migration, origin, and current-item tests."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import clipboard_model as cm
import clipboard_protocol as cp
import clipboard_store as cs
from clipboard_runtime import ClipboardManager


def settings():
    return cm.clipboard_settings({"clipboard": {"enabled": True}})


class ClipboardItemSchemaTests(unittest.TestCase):
    def test_new_item_has_additive_schema_origin_and_payload(self):
        item = cm.make_text_item("hello", seq=1, created_at=123.0)

        self.assertEqual(item["schema_version"], cm.ITEM_SCHEMA_VERSION)
        self.assertEqual(item["origin"]["event_id"], item["item_id"])
        self.assertEqual(item["origin"]["captured_at"], 123.0)
        self.assertEqual(item["payload"]["content_sha256"], item["sha256"])
        self.assertEqual(item["payload"]["sha256"], item["sha256"])
        self.assertEqual(item["payload_state"], "source_available")

    def test_manifest_is_versioned_and_excludes_local_paths(self):
        item = cm.make_text_item("hello", seq=1)
        item["files"] = [{"abspath": "C:\\private\\secret.txt"}]
        item["providers"] = [{
            "device_id": "dev-a",
            "state": "available",
            "last_seen_at": 1.0,
            "cache_path": "C:\\private\\cache",
        }]
        item["payload"]["cache_path"] = "C:\\private\\payload"
        item["metadata"] = {"local_path": "C:\\private\\metadata"}

        manifest = cm.build_manifest("profile", "dev-a", 2, [item], item["item_id"])
        encoded = json.dumps(manifest)

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["current_item_id"], item["item_id"])
        self.assertNotIn("abspath", encoded)
        self.assertNotIn("cache_path", encoded)
        self.assertNotIn("local_path", encoded)
        self.assertIsNotNone(cm.parse_manifest(manifest))

    def test_future_item_and_manifest_schemas_are_rejected(self):
        item = cm.make_text_item("hello", seq=1)
        item["schema_version"] = cm.ITEM_SCHEMA_VERSION + 1
        with self.assertRaises(ValueError):
            cm.version_item(item)
        manifest = cm.build_manifest("profile", "dev-a", 1, [], None)
        manifest["schema_version"] = cm.ITEM_SCHEMA_VERSION + 1
        self.assertIsNone(cm.parse_manifest(manifest))

    def test_malformed_ids_hashes_and_structures_are_rejected(self):
        item = cm.make_text_item("hello", seq=1)
        manifest = cm.build_manifest("profile", "dev-a", 1, [item], item["item_id"])
        manifest["items"][0]["item_id"] = "../escape"
        self.assertIsNone(cm.parse_manifest(manifest))

        manifest = cm.build_manifest("profile", "dev-a", 1, [item], item["item_id"])
        manifest["items"][0]["sha256"] = "../escape"
        self.assertIsNone(cm.parse_manifest(manifest))

        manifest = cm.build_manifest("profile", "dev-a", 1, [item], item["item_id"])
        manifest["items"][0]["size"] = True
        self.assertIsNone(cm.parse_manifest(manifest))

        manifest = cm.build_manifest("profile", "dev-a", 1, [item], item["item_id"])
        manifest["items"][0]["payload"]["sha256"] = "b" * 64
        self.assertIsNone(cm.parse_manifest(manifest))


class ClipboardStoreMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flowshift-clipboard-schema-")
        self.root = Path(self.temp.name)
        self.profile_dir = self.root / "profiles" / "profile"
        self.profile_dir.mkdir(parents=True)
        (self.profile_dir / "objects").mkdir()
        (self.profile_dir / "previews").mkdir()
        (self.profile_dir / "temp").mkdir()
        self.index = self.profile_dir / "index.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_store_migrates_once_with_backup_and_current_item(self):
        first = cm.make_text_item("first", seq=1, created_at=1.0)
        second = cm.make_text_item("second", seq=2, created_at=2.0)
        for item in (first, second):
            item.pop("schema_version", None)
            item.pop("origin", None)
            item.pop("payload", None)
            item.pop("providers", None)
            item.pop("payload_state", None)
        legacy = {"revision": 7, "items": [first, second], "future_key": {"keep": True}}
        raw = json.dumps(legacy).encode("utf-8")
        self.index.write_bytes(raw)
        (self.profile_dir / "objects" / second["sha256"]).write_bytes(b"second")

        store = cs.ClipboardStore(str(self.root), "profile")
        document = json.loads(self.index.read_text(encoding="utf-8"))
        backup = Path(cs.schema_backup_path(str(self.index), 0, 1))

        self.assertEqual(backup.read_bytes(), raw)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["future_key"], {"keep": True})
        self.assertEqual(store.current_item_id, second["item_id"])
        self.assertEqual(store.current_item()["payload_state"], "cached")
        self.assertTrue(all(item["schema_version"] == 1 for item in store.list_items()))

        before = backup.read_bytes()
        reopened = cs.ClipboardStore(str(self.root), "profile")
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(reopened.current_item_id, second["item_id"])

    def test_corrupt_store_is_preserved_and_recovered(self):
        self.index.write_bytes(b'{"broken":')

        store = cs.ClipboardStore(str(self.root), "profile")
        backups = list(self.profile_dir.glob("index.backup-corrupt-*.json"))
        document = json.loads(self.index.read_text(encoding="utf-8"))

        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'{"broken":')
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(store.list_items(), [])
        self.assertIn("corrupt_index", store.load_error)

    def test_structurally_invalid_store_is_preserved_as_corrupt(self):
        raw = json.dumps({"schema_version": 1, "revision": 0, "items": None,
                          "received_cache": {}}).encode("utf-8")
        self.index.write_bytes(raw)

        store = cs.ClipboardStore(str(self.root), "profile")
        backups = list(self.profile_dir.glob("index.backup-corrupt-*.json"))

        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), raw)
        self.assertEqual(store.list_items(), [])

    def test_future_store_is_read_only_and_not_rewritten(self):
        future = {"schema_version": 99, "revision": 3, "current_item_id": None,
                  "items": [], "received_cache": {}, "new_data": True}
        raw = json.dumps(future).encode("utf-8")
        self.index.write_bytes(raw)

        store = cs.ClipboardStore(str(self.root), "profile")

        self.assertTrue(store.read_only)
        self.assertEqual(self.index.read_bytes(), raw)
        with self.assertRaises(RuntimeError):
            store.add_item(cm.make_text_item("blocked", seq=0), data=b"blocked")

    def test_future_store_with_unknown_shapes_still_opens_read_only(self):
        future = {"schema_version": 99, "revision": "future", "current_item_id": {},
                  "items": {"future": True}, "received_cache": []}
        raw = json.dumps(future).encode("utf-8")
        self.index.write_bytes(raw)

        store = cs.ClipboardStore(str(self.root), "profile")

        self.assertTrue(store.read_only)
        self.assertEqual(store.list_items(), [])
        self.assertEqual(self.index.read_bytes(), raw)

    def test_failed_atomic_save_restores_memory_and_disk_state(self):
        store = cs.ClipboardStore(str(self.root), "profile")
        item, _ = store.add_item(cm.make_text_item("hello", seq=0), data=b"hello",
                                 make_current=True)
        before = self.index.read_bytes()
        real_replace = os.replace

        def fail_index_replace(source, destination):
            if os.path.abspath(destination) == os.path.abspath(self.index):
                raise OSError("simulated replace failure")
            return real_replace(source, destination)

        with mock.patch.object(cs.os, "replace", side_effect=fail_index_replace):
            with self.assertRaises(OSError):
                store.set_current(None)

        self.assertEqual(store.current_item_id, item["item_id"])
        self.assertEqual(self.index.read_bytes(), before)

    def test_replace_existing_is_atomic_and_preserves_order(self):
        store = cs.ClipboardStore(str(self.root), "profile")
        placeholder = cm.make_text_item("hello", seq=0)
        placeholder = cm.version_item(placeholder, payload_state="metadata_only")
        stored, _ = store.add_item(placeholder, make_current=True)
        before = self.index.read_bytes()
        before_seq = stored["seq"]
        real_replace = os.replace

        def fail_index_replace(source, destination):
            if os.path.abspath(destination) == os.path.abspath(self.index):
                raise OSError("simulated replace failure")
            return real_replace(source, destination)

        replacement = cm.version_item(placeholder, payload_state="cached")
        with mock.patch.object(cs.os, "replace", side_effect=fail_index_replace):
            with self.assertRaises(OSError):
                store.add_item(replacement, data=b"hello", replace_existing=True)

        self.assertEqual(store.get_item(stored["item_id"])["payload_state"], "metadata_only")
        self.assertEqual(store.get_item(stored["item_id"])["seq"], before_seq)
        self.assertEqual(store.current_item_id, stored["item_id"])
        self.assertEqual(self.index.read_bytes(), before)

    def test_accessors_do_not_expose_mutable_nested_state(self):
        store = cs.ClipboardStore(str(self.root), "profile")
        item, _ = store.add_item(cm.make_text_item("hello", seq=0), data=b"hello")

        fetched = store.get_item(item["item_id"])
        fetched["origin"]["device_id"] = "mutated"
        listed = store.list_items()
        listed[0]["payload"]["encoding"] = "mutated"

        persisted = store.get_item(item["item_id"])
        self.assertNotEqual(persisted["origin"]["device_id"], "mutated")
        self.assertNotEqual(persisted["payload"]["encoding"], "mutated")

    def test_missing_schema_one_payload_is_reconciled_on_startup(self):
        item = cm.version_item(cm.make_text_item("gone", seq=1), payload_state="cached")
        document = {"schema_version": 1, "revision": 1,
                    "current_item_id": item["item_id"], "items": [item],
                    "received_cache": {}}
        self.index.write_text(json.dumps(document), encoding="utf-8")

        store = cs.ClipboardStore(str(self.root), "profile")

        self.assertEqual(store.current_item()["payload_state"], "missing")
        self.assertFalse(store.current_item()["available"])

    def test_current_item_persists_and_clears_on_delete_or_eviction(self):
        store = cs.ClipboardStore(str(self.root), "profile")
        first, _ = store.add_item(cm.make_text_item("first", seq=0), data=b"first",
                                  make_current=True)
        second, _ = store.add_item(cm.make_text_item("second", seq=0), data=b"second")

        self.assertEqual(store.current_item_id, first["item_id"])
        self.assertEqual(cs.ClipboardStore(str(self.root), "profile").current_item_id,
                         first["item_id"])
        store.delete_item(first["item_id"])
        self.assertIsNone(store.current_item_id)
        store.set_current(second["item_id"])
        store.enforce_limits(0, 10**9)
        self.assertIsNone(store.current_item_id)


class ClipboardOriginTests(unittest.TestCase):
    def test_capture_all_uses_one_origin_event_across_profiles(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-origin-") as root:
            manager = ClipboardManager(root, "local-device", lambda _identity, _msg: None,
                                       settings)
            try:
                manager.capture_text_all(["peer-a", "peer-b"], "same event")
                item_a = manager.list_items("peer-a")[0]
                item_b = manager.list_items("peer-b")[0]
                self.assertNotEqual(item_a["item_id"], item_b["item_id"])
                self.assertEqual(item_a["origin"]["event_id"],
                                 item_b["origin"]["event_id"])
                self.assertEqual(item_a["origin"]["device_id"], "local-device")
                self.assertEqual(manager.store("peer-a").current_item_id,
                                 item_a["item_id"])
            finally:
                manager.shutdown()


class ClipboardAnnouncementTests(unittest.TestCase):
    def test_live_announcement_is_metadata_only_and_acknowledged_idempotently(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-announcement-") as root:
            outgoing = []
            acknowledgements = []
            sender = ClipboardManager(
                os.path.join(root, "sender"), "sender-device",
                lambda identity, msg: outgoing.append(msg), settings)
            receiver = ClipboardManager(
                os.path.join(root, "receiver"), "receiver-device",
                lambda identity, msg: acknowledgements.append(msg), settings)
            try:
                captured = sender.capture_text("receiver", "metadata first")
                self.assertEqual(len(outgoing), 1)
                announcement = outgoing[0]
                encoded = json.dumps(announcement)
                self.assertEqual(announcement["type"], cp.T_ANNOUNCEMENT)
                self.assertNotIn("data", announcement["item"])
                self.assertNotIn("abspath", encoded)
                parsed = cp.parse_announcement(announcement)
                self.assertEqual(parsed["item"]["origin"]["device_id"], "sender-device")
                self.assertEqual(parsed["item"]["providers"][0]["device_id"], "sender-device")

                receiver.handle("sender", announcement)
                stored = receiver.store("sender").get_item(captured["item_id"])
                self.assertIsNotNone(stored)
                self.assertFalse(stored["available"])
                self.assertEqual(receiver.store("sender").current_item_id, captured["item_id"])
                self.assertEqual(acknowledgements[-1]["status"], "accepted")
                accepted_ack = dict(acknowledgements[-1])
                self.assertFalse(any(msg.get("type") == cp.T_REQUEST
                                     for msg in acknowledgements))

                sender.handle("receiver", accepted_ack)
                sender.handle("receiver", accepted_ack)
                self.assertEqual(sender.stats["announcement_acks"], 1)

                receiver.handle("sender", announcement)
                self.assertEqual(acknowledgements[-1]["status"], "duplicate")
                self.assertEqual(len(receiver.list_items("sender")), 1)
            finally:
                sender.shutdown()
                receiver.shutdown()

    def test_announcement_rejects_payload_paths_and_unbound_provider(self):
        item = cm.make_text_item("private", seq=1)
        item["providers"] = [{"device_id": "other-device", "state": "available",
                              "last_seen_at": 1.0,
                              "payload_sha256": item["payload"]["sha256"],
                              "payload_size": item["payload"]["size"]}]
        message = cp.build_announcement(
            "announcement-private", "profile", "sender-device", 1, item["item_id"], item)
        self.assertIsNone(cp.parse_announcement(message))
        message["item"]["providers"][0]["device_id"] = "sender-device"
        message["item"]["files"] = [{"abspath": "C:\\private\\secret.txt"}]
        self.assertIsNone(cp.parse_announcement(message))

    def test_stale_announcement_cannot_clear_current(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-announcement-stale-") as root:
            receiver = ClipboardManager(root, "receiver-device", lambda _identity, _msg: None,
                                        settings)
            try:
                item = cm.version_item(cm.make_text_item("remote", seq=1),
                                       origin_device_id="sender-device")
                item["providers"] = [{"device_id": "sender-device", "state": "available",
                                      "last_seen_at": 1.0,
                                      "payload_sha256": item["payload"]["sha256"],
                                      "payload_size": item["payload"]["size"]}]
                fresh = cp.build_announcement(
                    "announcement-fresh", "sender", "sender-device", 5,
                    item["item_id"], item)
                stale = cp.build_announcement(
                    "announcement-stale", "sender", "sender-device", 4, None, item)
                receiver.handle("sender", fresh)
                receiver.handle("sender", stale)
                self.assertEqual(receiver.store("sender").current_item_id, item["item_id"])
            finally:
                receiver.shutdown()

    def test_failed_blob_capture_does_not_change_current_item(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-capture-failure-") as root:
            manager = ClipboardManager(root, "local-device", lambda _identity, _msg: None,
                                       settings)
            try:
                first = manager.capture_text("peer-a", "first")
                store = manager.store("peer-a")
                with mock.patch.object(store, "write_object", side_effect=OSError("disk full")):
                    failed = manager.capture_text("peer-a", "second")
                self.assertIsNone(failed)
                self.assertEqual(store.current_item_id, first["item_id"])
                self.assertEqual(len(store.list_items()), 1)
            finally:
                manager.shutdown()

    def test_manifest_reconciles_current_metadata_without_retransfer(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-current-reconcile-") as root:
            sent = []
            manager = ClipboardManager(root, "local-device",
                                       lambda identity, msg: sent.append((identity, msg)), settings)
            try:
                existing = manager.capture_text("peer-a", "same content")
                remote = cm.make_text_item("same content", seq=7)
                remote["item_id"] = "remote-current-event"
                remote["origin"]["event_id"] = "remote-current-event"
                manifest = cm.build_manifest("peer-a", "remote-device", 10, [remote],
                                             remote["item_id"])

                manager._on_manifest("peer-a", manifest)

                stored = manager.store("peer-a").get_item(remote["item_id"])
                self.assertIsNotNone(stored)
                self.assertEqual(manager.store("peer-a").current_item_id, remote["item_id"])
                self.assertFalse(any(msg.get("type") == "clipboard_request_items"
                                     for _identity, msg in sent))
                self.assertNotEqual(existing["item_id"], remote["item_id"])

                stale = cm.build_manifest("peer-a", "remote-device", 9, [remote], None)
                manager._on_manifest("peer-a", stale)
                self.assertEqual(manager.store("peer-a").current_item_id, remote["item_id"])

                fresh = cm.build_manifest("peer-a", "remote-device", 11, [remote], None)
                manager._on_manifest("peer-a", fresh)
                self.assertIsNone(manager.store("peer-a").current_item_id)
            finally:
                manager.shutdown()

    def test_metadata_only_manifest_item_is_requested_again_after_restart(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-metadata-retry-") as root:
            remote = cm.make_text_item("needs payload", seq=1)
            remote["item_id"] = "remote-missing-payload"
            remote["origin"]["event_id"] = remote["item_id"]
            manifest = cm.build_manifest("peer-a", "remote-device", 1, [remote],
                                         remote["item_id"])
            sent = []
            manager = ClipboardManager(root, "local-device",
                                       lambda identity, msg: sent.append(msg), settings)
            manager._on_manifest("peer-a", manifest)
            self.assertTrue(any(msg.get("type") == "clipboard_request_items" for msg in sent))
            sent.clear()
            manager._on_manifest("peer-a", manifest)
            self.assertTrue(any(msg.get("type") == "clipboard_request_items" for msg in sent))
            manager.shutdown()

            sent_after_restart = []
            reopened = ClipboardManager(
                root, "local-device", lambda identity, msg: sent_after_restart.append(msg), settings)
            try:
                reopened._on_manifest("peer-a", manifest)
                self.assertTrue(any(msg.get("type") == "clipboard_request_items"
                                    for msg in sent_after_restart))
            finally:
                reopened.shutdown()

    def test_item_origin_is_immutable_across_conflicting_manifest_replays(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-origin-replay-") as root:
            manager = ClipboardManager(root, "local-device", lambda _identity, _msg: None,
                                       settings)
            try:
                item = cm.make_text_item("payload", seq=1)
                item["item_id"] = "immutable-event"
                item["origin"]["event_id"] = item["item_id"]
                item["origin"]["device_id"] = "origin-a"
                first = cm.build_manifest("peer-a", "remote-device", 5, [item], item["item_id"])
                manager._on_manifest("peer-a", first)

                changed = cm.version_item(item)
                changed["origin"]["device_id"] = "origin-b"
                replay = cm.build_manifest("peer-a", "remote-device", 5, [changed], None)
                manager._on_manifest("peer-a", replay)

                self.assertEqual(manager._remote_meta["peer-a"][item["item_id"]]
                                 ["origin"]["device_id"], "origin-a")
                self.assertEqual(manager.store("peer-a").get_item(item["item_id"])
                                 ["origin"]["device_id"], "origin-a")
                self.assertEqual(manager.store("peer-a").current_item_id, item["item_id"])
            finally:
                manager.shutdown()

    def test_receive_finalization_failure_is_reported_without_masking(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-finalize-failure-") as root:
            sent = []
            manager = ClipboardManager(root, "local-device",
                                       lambda identity, msg: sent.append(msg), settings)
            payload = b"payload"
            payload_sha = cm.sha256_bytes(payload)
            try:
                manager._on_start("peer-a", {
                    "type": "clipboard_transfer_start", "transfer_id": "transfer-one",
                    "item_id": "received-item", "sha256": payload_sha,
                    "total_size": len(payload), "chunk_size": len(payload), "chunk_count": 1,
                    "kind": cm.KIND_BINARY, "mime": "application/octet-stream",
                    "file_count": 0, "display_name": "payload.bin",
                })
                manager._on_chunk("peer-a", {
                    "type": "clipboard_transfer_chunk", "transfer_id": "transfer-one",
                    "item_id": "received-item", "chunk_index": 0, "offset": 0,
                    "size": len(payload), "sha256": payload_sha,
                    "data": base64.b64encode(payload).decode("ascii"),
                })
                store = manager.store("peer-a")
                with mock.patch.object(store, "add_item", side_effect=OSError("index failed")):
                    manager._on_complete("peer-a", {
                        "type": "clipboard_transfer_complete", "transfer_id": "transfer-one",
                        "item_id": "received-item", "sha256": payload_sha, "status": "ok",
                    })
                self.assertTrue(any(msg.get("type") == "clipboard_transfer_error" for msg in sent))
                self.assertGreaterEqual(manager.stats["failed"], 1)
            finally:
                manager.shutdown()

    def test_explicit_successful_write_can_mark_an_older_item_current(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-mark-current-") as root:
            manager = ClipboardManager(root, "local-device", lambda _identity, _msg: None,
                                       settings)
            try:
                first = manager.capture_text("peer-a", "first")
                manager.capture_text("peer-a", "second")
                self.assertTrue(manager.mark_current("peer-a", first["item_id"]))
                self.assertEqual(manager.store("peer-a").current_item_id, first["item_id"])
            finally:
                manager.shutdown()

    def test_transfer_paths_and_raw_payload_identity_are_validated(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-transfer-validation-") as root:
            manager = ClipboardManager(root, "local-device", lambda _identity, _msg: None,
                                       settings)
            try:
                manager._on_start("peer-a", {
                    "type": "clipboard_transfer_start", "transfer_id": "../escape",
                    "item_id": "safe-item", "sha256": "a" * 64,
                    "total_size": 1, "chunk_count": 1,
                })
                self.assertEqual(manager._assemblers, {})
                with self.assertRaises(ValueError):
                    manager.store("peer-a").object_path("../escape")
                item = cm.make_text_item("hello", seq=0)
                with self.assertRaises(ValueError):
                    manager._bind_received_payload("peer-a", item, "b" * 64, 5)
            finally:
                manager.shutdown()


class ReceivedCacheModelTests(unittest.TestCase):
    def test_make_cache_entry_validates_content_sha256(self):
        cm.make_cache_entry("a" * 64)
        with self.assertRaises(ValueError):
            cm.make_cache_entry("invalid")
        with self.assertRaises(ValueError):
            cm.make_cache_entry("")

    def test_make_cache_entry_validates_payload_sha256(self):
        cm.make_cache_entry("a" * 64, payload_sha256="b" * 64)
        with self.assertRaises(ValueError):
            cm.make_cache_entry("a" * 64, payload_sha256="invalid")

    def test_make_cache_entry_validates_payload_size(self):
        cm.make_cache_entry("a" * 64, payload_size=100)
        with self.assertRaises(ValueError):
            cm.make_cache_entry("a" * 64, payload_size=-1)

    def test_make_cache_entry_sets_timestamps(self):
        entry = cm.make_cache_entry("a" * 64, payload_size=100)
        self.assertEqual(entry["content_sha256"], "a" * 64)
        self.assertIsInstance(entry["received_at"], float)
        self.assertIsInstance(entry["last_access"], float)
        self.assertEqual(entry["received_at"], entry["last_access"])

    def test_validate_cache_entry_rejects_malformed(self):
        self.assertIsNone(cm.validate_cache_entry(None))
        self.assertIsNone(cm.validate_cache_entry({"content_sha256": "bad"}))
        self.assertEqual(
            cm.validate_cache_entry({"content_sha256": "a" * 64, "payload_size": 50}),
            {"content_sha256": "a" * 64, "payload_size": 50})

    def test_evictable_cache_entries_returns_lru_sorted(self):
        entries = {
            "aaa": {"last_access": 1.0},
            "bbb": {"last_access": 3.0},
            "ccc": {"last_access": 2.0},
        }
        evictable = cm.evictable_cache_entries(entries, {"bbb"})
        self.assertEqual(evictable, [("aaa", {"last_access": 1.0}),
                                      ("ccc", {"last_access": 2.0})])

    def test_evictable_cache_entries_excludes_protected(self):
        entries = {"aaa": {"last_access": 1.0}, "bbb": {"last_access": 2.0}}
        evictable = cm.evictable_cache_entries(entries, {"aaa", "bbb"})
        self.assertEqual(evictable, [])


class ReceivedCacheStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = cs.ClipboardStore(self.tmp, "cache-test")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_and_get_cache_entry(self):
        self.store.record_cache_entry("a" * 64, payload_size=100)
        entry = self.store.get_cache_entry("a" * 64)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["payload_size"], 100)

    def test_access_cache_entry_updates_last_access(self):
        self.store.record_cache_entry("a" * 64, payload_size=100)
        before = self.store.get_cache_entry("a" * 64)["last_access"]
        import time
        time.sleep(0.01)
        self.store.access_cache_entry("a" * 64)
        after = self.store.get_cache_entry("a" * 64)["last_access"]
        self.assertGreater(after, before)

    def test_remove_cache_entry(self):
        self.store.record_cache_entry("a" * 64, payload_size=100)
        self.assertIsNotNone(self.store.get_cache_entry("a" * 64))
        self.store.remove_cache_entry("a" * 64)
        self.assertIsNone(self.store.get_cache_entry("a" * 64))

    def test_cache_protected_hashes_includes_pinned_and_current(self):
        item = cm.make_text_item("hello", seq=1)
        item["pinned"] = True
        self.store.add_item(item, data=b"hello")
        other = cm.make_text_item("world", seq=2)
        self.store.add_item(other, data=b"world", make_current=True)
        protected = self.store.cache_protected_hashes()
        self.assertIn(item["sha256"], protected)
        self.assertIn(other["sha256"], protected)

    def test_cache_protected_hashes_includes_extra(self):
        protected = self.store.cache_protected_hashes(extra_protected={"b" * 64})
        self.assertIn("b" * 64, protected)

    def test_evict_cache_protects_pinned_item_hash(self):
        item = cm.make_text_item("pinned-one", seq=1)
        self.store.add_item(item, data=b"pinned-one")
        self.store.set_pinned(item["item_id"], True)
        self.store.record_cache_entry(item["sha256"], payload_size=10)
        self.store.record_cache_entry("b" * 64, payload_size=10)
        evicted = self.store.evict_cache()
        self.assertNotIn(item["sha256"], evicted)
        self.assertIn("b" * 64, evicted)

    def test_evict_cache_protects_current_item_hash(self):
        item = cm.make_text_item("current", seq=1)
        self.store.add_item(item, data=b"current", make_current=True)
        self.store.record_cache_entry(item["sha256"], payload_size=10)
        self.store.record_cache_entry("b" * 64, payload_size=10)
        evicted = self.store.evict_cache()
        self.assertNotIn(item["sha256"], evicted)
        self.assertIn("b" * 64, evicted)

    def test_evict_cache_respects_extra_protected(self):
        self.store.record_cache_entry("a" * 64, payload_size=10)
        self.store.record_cache_entry("b" * 64, payload_size=10)
        evicted = self.store.evict_cache(protected_hashes={"a" * 64})
        self.assertNotIn("a" * 64, evicted)
        self.assertIn("b" * 64, evicted)

    def test_evict_cache_removes_lru_first(self):
        self.store.record_cache_entry("c" * 64, payload_size=10)
        self.store.record_cache_entry("b" * 64, payload_size=10)
        self.store.record_cache_entry("a" * 64, payload_size=10)
        evicted = self.store.evict_cache()
        self.assertEqual(len(evicted), 3)
        keys = list(evicted.keys())
        self.assertEqual(keys, ["c" * 64, "b" * 64, "a" * 64])

    def test_evict_cache_target_unique_bytes(self):
        self.store.record_cache_entry("c" * 64, payload_size=100)
        self.store.record_cache_entry("b" * 64, payload_size=100)
        self.store.record_cache_entry("a" * 64, payload_size=100)
        evicted = self.store.evict_cache(target_unique_bytes=150)
        self.assertEqual(len(evicted), 2)

    def test_cache_snapshot_reports_counts(self):
        self.store.record_cache_entry("a" * 64, payload_size=100)
        self.store.record_cache_entry("b" * 64, payload_size=200)
        snap = self.store.cache_snapshot()
        self.assertEqual(snap["entry_count"], 2)
        self.assertEqual(snap["unique_bytes"], 300)
        self.assertEqual(snap["protected_count"], 0)

    def test_clear_removes_cache_entries(self):
        self.store.record_cache_entry("a" * 64, payload_size=100)
        self.store.clear()
        self.assertIsNone(self.store.get_cache_entry("a" * 64))
        snap = self.store.cache_snapshot()
        self.assertEqual(snap["entry_count"], 0)

    def test_cache_entry_survives_restart(self):
        self.store.record_cache_entry("a" * 64, payload_size=100)
        self.store2 = cs.ClipboardStore(self.tmp, "cache-test")
        entry = self.store2.get_cache_entry("a" * 64)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["payload_size"], 100)

    def test_record_cache_entry_merges_providers(self):
        providers = [{"device_id": "dev-a", "state": "available", "last_seen_at": 1.0}]
        self.store.record_cache_entry("a" * 64, payload_size=100, providers=providers)
        entry = self.store.get_cache_entry("a" * 64)
        self.assertEqual(len(entry["providers"]), 1)
        providers2 = [{"device_id": "dev-b", "state": "available", "last_seen_at": 2.0}]
        self.store.record_cache_entry("a" * 64, payload_size=100, providers=providers2)
        entry = self.store.get_cache_entry("a" * 64)
        self.assertEqual(len(entry["providers"]), 2)


class ReceivedCacheRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sent = []
        self.manager = ClipboardManager(
            self.tmp, "dev-self",
            send_fn=lambda identity, msg: self.sent.append(msg),
            settings_fn=lambda: cm.clipboard_settings(
                {"clipboard": {"enabled": True, "cache_received_payloads": True}}))

    def tearDown(self):
        self.manager.shutdown()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cache_disabled_does_not_record_entries(self):
        disabled_mgr = ClipboardManager(
            self.tmp, "dev-self",
            send_fn=lambda identity, msg: None,
            settings_fn=lambda: cm.clipboard_settings(
                {"clipboard": {"enabled": True, "cache_received_payloads": False}}))
        try:
            st = disabled_mgr.store("peer-a")
            item = cm.make_text_item("test", seq=1)
            item = disabled_mgr._bind_received_payload("peer-a", item, item["sha256"], 4)
            entry = st.get_cache_entry(item["sha256"])
            self.assertIsNone(entry)
        finally:
            disabled_mgr.shutdown()

    def test_cache_records_after_bind_received_payload(self):
        st = self.manager.store("peer-a")
        item = cm.make_text_item("hello", seq=0)
        item = self.manager._bind_received_payload("peer-a", item, item["sha256"], 5)
        entry = st.get_cache_entry(item["sha256"])
        self.assertIsNotNone(entry)
        self.assertEqual(entry["payload_size"], 5)

    def test_evict_cache_runs_after_receiving_item(self):
        st = self.manager.store("peer-a")
        sha_a = "a" * 64
        sha_b = "b" * 64
        st.record_cache_entry(sha_a, payload_size=10)
        st.record_cache_entry(sha_b, payload_size=10)
        item = cm.make_text_item("hello", seq=1)
        pinned = cm.make_text_item("pinned-one", seq=2)
        st.add_item(pinned, data=b"pinned-one")
        st.set_pinned(pinned["item_id"], True)
        st.record_cache_entry(pinned["sha256"], payload_size=10)
        evicted = self.manager._evict_cache_if_needed("peer-a")
        self.assertIn(sha_b, evicted)
        self.assertNotIn(pinned["sha256"], evicted)


class MaterializationLeaseModelTests(unittest.TestCase):
    def test_make_lease_requires_valid_params(self):
        lease = cm.make_lease("profile-a", "item-123", "/tmp/dest")
        self.assertEqual(lease["profile_id"], "profile-a")
        self.assertEqual(lease["item_id"], "item-123")
        self.assertEqual(lease["state"], cm.LEASE_ACTIVE)
        self.assertIsNone(lease["owner_sequence"])
        with self.assertRaises(ValueError):
            cm.make_lease("", "item-123", "/tmp/dest")
        with self.assertRaises(ValueError):
            cm.make_lease("profile-a", "../escape", "/tmp/dest")
        with self.assertRaises(ValueError):
            cm.make_lease("profile-a", "item-123", "")

    def test_validate_lease_rejects_malformed(self):
        self.assertIsNone(cm.validate_lease(None))
        self.assertIsNone(cm.validate_lease({"item_id": "../escape"}))
        self.assertEqual(
            cm.validate_lease({"profile_id": "p", "item_id": "i-1", "dest_path": "/d",
                               "state": "active"}),
            {"profile_id": "p", "item_id": "i-1", "dest_path": "/d", "state": "active"})

    def test_lease_stale_cutoff_is_reasonable(self):
        cutoff = cm.lease_stale_cutoff(24)
        self.assertIsInstance(cutoff, float)
        self.assertGreater(cutoff, 0)


class MaterializationLeaseStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = cs.ClipboardStore(self.tmp, "lease-test")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_set_and_get_lease(self):
        self.store.set_lease("item-1", "/tmp/dest-1")
        lease = self.store.get_lease("item-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease["item_id"], "item-1")
        self.assertEqual(lease["state"], cm.LEASE_ACTIVE)

    def test_bind_lease_sequence(self):
        self.store.set_lease("item-1", "/tmp/dest-1")
        self.assertTrue(self.store.bind_lease_sequence("item-1", 42))
        lease = self.store.get_lease("item-1")
        self.assertEqual(lease["owner_sequence"], 42)

    def test_bind_lease_unknown_item_returns_false(self):
        self.assertFalse(self.store.bind_lease_sequence("no-such-item", 42))

    def test_release_lease(self):
        self.store.set_lease("item-1", "/tmp/dest-1")
        self.assertTrue(self.store.release_lease("item-1"))
        self.assertIsNone(self.store.get_lease("item-1"))

    def test_release_leases_for_item(self):
        self.store.set_lease("item-1", "/tmp/d1")
        self.store.set_lease("item-2", "/tmp/d2")
        released = self.store.release_leases_for_item("item-1")
        self.assertIn("item-1", released)
        self.assertNotIn("item-2", released)
        self.assertIsNotNone(self.store.get_lease("item-2"))

    def test_active_lease_hashes_includes_item_content(self):
        item = cm.make_text_item("hello", seq=1)
        self.store.add_item(item, data=b"hello")
        self.store.set_lease(item["item_id"], "/tmp/dest")
        hashes = self.store.active_lease_hashes()
        self.assertIn(item["sha256"], hashes)

    def test_release_stale_leases_removes_non_matching_sequence(self):
        self.store.set_lease("item-1", "/tmp/d1")
        self.store.bind_lease_sequence("item-1", 10)
        self.store.set_lease("item-2", "/tmp/d2")
        self.store.bind_lease_sequence("item-2", 20)
        released = self.store.release_stale_leases(current_sequence=20)
        self.assertIn("item-1", released)
        self.assertNotIn("item-2", released)

    def test_cleanup_leases_removes_stale_old_leases(self):
        self.store.set_lease("item-1", "/tmp/d1")
        lease = self.store.get_lease("item-1")
        import time as _time
        lease["last_access"] = 1.0
        lease["state"] = cm.LEASE_RELEASED
        self.store._materialization_leases["item-1"] = lease
        removed = self.store.cleanup_leases(max_age_hours=0)
        self.assertIn("item-1", removed)

    def test_lease_snapshot_counts(self):
        self.store.set_lease("item-1", "/tmp/d1")
        snap = self.store.lease_snapshot()
        self.assertEqual(snap["active"], 1)
        self.assertEqual(snap["total"], 1)

    def test_lease_survives_restart(self):
        self.store.set_lease("item-1", "/tmp/dest")
        self.store2 = cs.ClipboardStore(self.tmp, "lease-test")
        lease = self.store2.get_lease("item-1")
        self.assertIsNotNone(lease)

    def test_clear_removes_leases(self):
        self.store.set_lease("item-1", "/tmp/dest")
        self.store.clear()
        self.assertIsNone(self.store.get_lease("item-1"))
        snap = self.store.lease_snapshot()
        self.assertEqual(snap["total"], 0)


class MaterializationLeaseRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sent = []
        self.manager = ClipboardManager(
            self.tmp, "dev-self",
            send_fn=lambda identity, msg: self.sent.append(msg),
            settings_fn=lambda: cm.clipboard_settings(
                {"clipboard": {"enabled": True, "cache_received_payloads": True}}))

    def tearDown(self):
        self.manager.shutdown()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_perform_windows_write_binds_lease(self):
        st = self.manager.store("peer-a")
        item = cm.make_text_item("hello", seq=1)
        st.add_item(item, data=b"hello")
        st.set_lease(item["item_id"], "/tmp/dest")
        write_called = []
        def write_fn():
            write_called.append(True)
            return (True, 42)
        def seq_fn():
            return 42
        self.manager.perform_windows_write(
            "peer-a", item["item_id"], {"text"}, "text",
            self.manager.text_digest("hello"), write_fn, seq_fn)
        lease = st.get_lease(item["item_id"])
        self.assertEqual(lease["owner_sequence"], 42)

    def test_delete_item_releases_lease(self):
        st = self.manager.store("peer-a")
        item = cm.make_text_item("hello", seq=1)
        st.add_item(item, data=b"hello")
        st.set_lease(item["item_id"], "/tmp/dest")
        self.assertIsNotNone(st.get_lease(item["item_id"]))
        self.manager.delete_item("peer-a", item["item_id"])
        self.assertIsNone(st.get_lease(item["item_id"]))

    def test_active_lease_hashes_protects_cache_from_eviction(self):
        st = self.manager.store("peer-a")
        item = cm.make_text_item("protected-by-lease", seq=1)
        st.add_item(item, data=b"protected-by-lease")
        st.set_lease(item["item_id"], "/tmp/dest")
        st.record_cache_entry(item["sha256"], payload_size=10)
        st.record_cache_entry("b" * 64, payload_size=10)
        protected = st.cache_protected_hashes()
        protected |= st.active_lease_hashes()
        evicted = st.evict_cache(protected_hashes=protected)
        self.assertNotIn(item["sha256"], evicted)
        self.assertIn("b" * 64, evicted)


if __name__ == "__main__":
    unittest.main()
