"""Phase 2 clipboard schema, migration, origin, and current-item tests."""
from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import clipboard_model as cm
import clipboard_protocol as cp
import clipboard_store as cs
import clipboard_transfer as ctt
from clipboard_runtime import ClipboardManager


def settings():
    return cm.clipboard_settings({"clipboard": {"enabled": True}})


class ClipboardItemSchemaTests(unittest.TestCase):
    def test_new_item_has_additive_schema_origin_and_payload(self):
        item = cm.make_text_item("hello", seq=1, created_at=123.0)

        self.assertEqual(item["schema_version"], cm.PERSISTED_ITEM_SCHEMA_VERSION)
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
        item["schema_version"] = cm.PERSISTED_ITEM_SCHEMA_VERSION + 1
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
        backup = Path(cs.schema_backup_path(str(self.index), 0, 2))

        self.assertEqual(backup.read_bytes(), raw)
        self.assertEqual(document["schema_version"], 2)
        self.assertEqual(document["integrity_tombstones"], {})
        self.assertTrue(all(item["schema_version"] == 2 for item in document["items"]))
        self.assertEqual(document["future_key"], {"keep": True})
        self.assertEqual(store.current_item_id, second["item_id"])
        self.assertEqual(store.current_item()["payload_state"], "cached")
        self.assertTrue(all(item["schema_version"] == 2 for item in store.list_items()))

        before = backup.read_bytes()
        reopened = cs.ClipboardStore(str(self.root), "profile")
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(len(list(self.profile_dir.glob("index.backup-schema-*.json"))), 1)
        self.assertEqual(reopened.current_item_id, second["item_id"])

    def test_corrupt_store_is_preserved_and_recovered(self):
        self.index.write_bytes(b'{"broken":')

        store = cs.ClipboardStore(str(self.root), "profile")
        backups = list(self.profile_dir.glob("index.backup-corrupt-*.json"))
        document = json.loads(self.index.read_text(encoding="utf-8"))

        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'{"broken":')
        self.assertEqual(document["schema_version"], 2)
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

    def test_received_commit_index_failure_rolls_back_item_session_and_new_object(self):
        store = cs.ClipboardStore(str(self.root), "profile")
        payload = b"atomic receipt"
        item = cm.make_binary_item(cm.sha256_bytes(payload), len(payload), seq=0)
        item["item_id"] = "atomic-received-item"
        item["origin"]["event_id"] = item["item_id"]
        item = cm.version_item(item, payload_state="cached")
        session = ctt.TransferSession(
            transfer_id="atomic-received-transfer", direction="receive",
            item_id=item["item_id"], item_revision=cm.item_revision(item),
            profile="profile", peer_identity="peer-a", provider="peer-a",
            strategy=ctt.LEGACY_ZIP_V1_STRATEGY,
            manifest_digest=item["sha256"], logical_bytes=len(payload), file_count=0,
            remaining_bytes=0, state=ctt.TransferSessionState.completed,
            progress={"payload_sha256": item["sha256"]})
        staged = Path(store.temp_dir) / "atomic-received.part"
        staged.write_bytes(payload)
        before = self.index.read_bytes() if self.index.exists() else None
        real_replace = os.replace

        def fail_index_replace(source, destination):
            if os.path.abspath(destination) == os.path.abspath(self.index):
                raise OSError("simulated receipt index failure")
            return real_replace(source, destination)

        with mock.patch.object(cs.os, "replace", side_effect=fail_index_replace):
            with self.assertRaises(OSError):
                store.commit_received_item(
                    item, session.snapshot(), object_source_path=str(staged))

        self.assertEqual(self.index.read_bytes() if self.index.exists() else None, before)
        self.assertIsNone(store.get_item(item["item_id"]))
        self.assertNotIn(session.transfer_id, store.transfer_sessions_snapshot())
        self.assertFalse(store.has_object(item["sha256"]))
        self.assertFalse(staged.exists())

    def test_received_commit_failure_preserves_existing_object(self):
        store = cs.ClipboardStore(str(self.root), "profile")
        payload = b"deduplicated receipt"
        digest = cm.sha256_bytes(payload)
        store.write_object(digest, payload)
        item = cm.make_binary_item(digest, len(payload), seq=0)
        item["item_id"] = "deduplicated-received-item"
        item["origin"]["event_id"] = item["item_id"]
        item = cm.version_item(item, payload_state="cached")
        session = ctt.TransferSession(
            transfer_id="deduplicated-received-transfer", direction="receive",
            item_id=item["item_id"], item_revision=cm.item_revision(item),
            profile="profile", peer_identity="peer-a", provider="peer-a",
            strategy=ctt.LEGACY_ZIP_V1_STRATEGY, manifest_digest=digest,
            logical_bytes=len(payload), file_count=0, remaining_bytes=0,
            state=ctt.TransferSessionState.completed,
            progress={"payload_sha256": digest})

        with mock.patch.object(store, "_save", side_effect=OSError("index failed")):
            with self.assertRaises(OSError):
                store.commit_received_item(item, session.snapshot(), data=payload)

        self.assertTrue(store.has_object(digest))
        self.assertEqual(Path(store.object_path(digest)).read_bytes(), payload)
        self.assertIsNone(store.get_item(item["item_id"]))
        self.assertNotIn(session.transfer_id, store.transfer_sessions_snapshot())

    def test_received_commit_dedupes_only_after_full_object_verification(self):
        store = cs.ClipboardStore(str(self.root), "profile")
        payload = b"verified dedup object"
        digest = cm.sha256_bytes(payload)
        store.write_object(digest, payload)
        object_path = Path(store.object_path(digest))
        before = object_path.stat().st_mtime_ns
        item = cm.make_binary_item(digest, len(payload), seq=0)
        item["item_id"] = "verified-dedup-item"
        item["origin"]["event_id"] = item["item_id"]
        session = ctt.TransferSession(
            transfer_id="verified-dedup-transfer", direction="receive",
            item_id=item["item_id"], item_revision=cm.item_revision(item),
            profile="profile", peer_identity="peer-a", provider="peer-a",
            strategy=ctt.LEGACY_ZIP_V1_STRATEGY, manifest_digest=digest,
            logical_bytes=len(payload), file_count=0, remaining_bytes=0,
            state=ctt.TransferSessionState.completed,
            progress={"payload_sha256": digest})

        with mock.patch.object(store, "_install_verified_object_locked",
                               wraps=store._install_verified_object_locked) as install:
            stored, _ = store.commit_received_item(
                item, session.snapshot(), data=payload)

        self.assertEqual(install.call_count, 0)
        self.assertEqual(object_path.stat().st_mtime_ns, before)
        self.assertTrue(stored["available"])
        self.assertEqual(object_path.read_bytes(), payload)

    def test_crash_after_object_publication_does_not_commit_placeholder(self):
        store = cs.ClipboardStore(str(self.root), "profile")
        payload = b"publication boundary"
        digest = cm.sha256_bytes(payload)
        item = cm.make_binary_item(digest, len(payload), seq=0)
        item["item_id"] = "publication-boundary-item"
        item["origin"]["event_id"] = item["item_id"]
        item = cm.version_item(item, payload_state="receiving")
        store.add_item(item)
        session = ctt.TransferSession(
            transfer_id="publication-boundary-transfer", direction="receive",
            item_id=item["item_id"], item_revision=cm.item_revision(item),
            profile="profile", peer_identity="peer-a", provider="peer-a",
            strategy=ctt.LEGACY_ZIP_V1_STRATEGY, manifest_digest=digest,
            logical_bytes=len(payload), file_count=0, remaining_bytes=0,
            state=ctt.TransferSessionState.completed,
            progress={"payload_sha256": digest})

        class InjectedCrash(BaseException):
            pass

        with mock.patch.object(store, "_save", side_effect=InjectedCrash()):
            with self.assertRaises(InjectedCrash):
                store.commit_received_item(item, session.snapshot(), data=payload,
                                           replace_existing=True)

        self.assertTrue(Path(store.object_path(digest)).exists())
        reopened = cs.ClipboardStore(str(self.root), "profile")
        restored = reopened.get_item(item["item_id"])
        self.assertEqual(restored["payload_state"], "receiving")
        self.assertFalse(restored["available"])
        self.assertIsNone(reopened.get_data(item["item_id"]))
        self.assertFalse(reopened.has_committed_object(digest))
        self.assertNotIn(session.transfer_id, reopened.transfer_sessions_snapshot())
        self.assertEqual(reopened.build_manifest("local-device")["items"][0]
                         ["available"], False)
        sent = []
        manager = ClipboardManager(
            str(self.root), "local-device", lambda _identity, message: sent.append(message),
            settings)
        try:
            self.assertTrue(manager.handle("profile", cp.build_transfer_complete(
                session.transfer_id, item["item_id"], digest)))
            self.assertFalse(any(message.get("type") == cp.T_ACK for message in sent))
        finally:
            manager.shutdown()

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
        self.assertTrue(Path(cs.schema_backup_path(str(self.index), 1, 2)).exists())
        self.assertEqual(json.loads(self.index.read_text(encoding="utf-8"))["schema_version"], 2)

    def test_schema_one_hashed_file_migrates_additively(self):
        source = self.profile_dir / "legacy.txt"
        source.write_bytes(b"legacy")
        digest = cm.sha256_bytes(b"legacy")
        legacy = {
            "item_id": "legacy-file", "sha256": digest, "kind": cm.KIND_FILE,
            "mime": "application/zip", "size": 6, "created_at": 1.0, "seq": 1,
            "display_name": "legacy.txt", "preview_text": "legacy.txt",
            "preview_hash": "", "file_count": 1, "total_file_size": 6,
            "pinned": False, "available": True,
            "files": [{"abspath": str(source), "rel": "legacy.txt", "size": 6,
                       "sha256": digest}],
        }
        document = {"schema_version": 1, "revision": 1, "current_item_id": "legacy-file",
                    "items": [legacy], "received_cache": {}, "materialization_leases": {}}
        self.index.write_text(json.dumps(document), encoding="utf-8")

        store = cs.ClipboardStore(str(self.root), "profile")
        migrated = store.current_item()
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["content_sha256"], digest)
        self.assertEqual(migrated["hash_state"], "verified")
        self.assertEqual(migrated["item_revision"], 0)

    def test_schema_one_hashed_file_same_size_mutation_is_unavailable(self):
        source = self.profile_dir / "legacy-mutated.txt"
        source.write_bytes(b"original")
        digest = cm.sha256_bytes(b"original")
        legacy = {
            "item_id": "legacy-mutated", "sha256": digest, "kind": cm.KIND_FILE,
            "mime": "application/zip", "size": 8, "created_at": 1.0, "seq": 1,
            "display_name": "legacy-mutated.txt", "preview_text": "legacy-mutated.txt",
            "preview_hash": "", "file_count": 1, "total_file_size": 8,
            "pinned": False, "available": True,
            "files": [{"abspath": str(source), "rel": "legacy-mutated.txt", "size": 8,
                       "sha256": digest}],
        }
        self.index.write_text(json.dumps({
            "schema_version": 1, "revision": 1, "current_item_id": legacy["item_id"],
            "items": [legacy], "received_cache": {}, "materialization_leases": {},
        }), encoding="utf-8")
        source.write_bytes(b"mutated!")

        migrated = cs.ClipboardStore(str(self.root), "profile").current_item()
        self.assertFalse(migrated["available"])
        self.assertEqual(migrated["payload_state"], "missing")

    def test_store_reload_checks_fingerprint_without_reading_source(self):
        import clipboard_files as cf
        source = self.profile_dir / "metadata-only.bin"
        source.write_bytes(b"source payload")
        item = cf.make_file_item([source])
        store = cs.ClipboardStore(str(self.root), "profile")
        store.add_item(item)
        real_open = open

        def reject_source_open(path, *args, **kwargs):
            if os.path.abspath(os.fspath(path)) == os.path.abspath(source):
                raise AssertionError("store reload read source contents")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=reject_source_open):
            reopened = cs.ClipboardStore(str(self.root), "profile")
        loaded = reopened.list_items()
        self.assertEqual(len(loaded), 1)
        self.assertTrue(loaded[0]["available"])
        self.assertEqual(loaded[0]["payload_state"], "source_available")

    def test_public_history_projection_stays_schema_one_and_private_path_free(self):
        import clipboard_files as cf
        source = self.profile_dir / "private.bin"
        source.write_bytes(b"private")
        item = cf.make_file_item([source])
        projection = cm.manifest_item(item)
        self.assertEqual(projection["schema_version"], 1)
        self.assertEqual(projection["sha256"], item["legacy_provisional_sha256"])
        self.assertEqual(projection["payload"]["content_sha256"],
                         item["legacy_provisional_sha256"])
        self.assertEqual(projection["metadata"]["flowshift_file_identity"],
                         cm.PROVISIONAL_FILE_IDENTITY)
        imported = cm.parse_manifest(cm.build_manifest("profile", "peer", 1, [item]))["items"][0]
        self.assertIsNone(imported["content_sha256"])
        self.assertEqual(imported["hash_state"], "unhashed")
        self.assertNotIn(str(source), json.dumps(projection))

    def test_newer_hash_revision_keeps_item_id_and_replaces_identity(self):
        import clipboard_files as cf
        store = cs.ClipboardStore(str(self.root), "profile")
        source = self.profile_dir / "revision.bin"
        source.write_bytes(b"old")
        stored, _ = store.add_item(cf.make_file_item([source]))
        revised = dict(stored)
        revised["item_revision"] = 1
        revised["hash_state"] = "verified"
        entries = [dict(entry) for entry in revised["batch_manifest"]["entries"]]
        entries[0] = dict(entries[0], hash_state="verified", sha256="f" * 64)
        revised["files"] = [dict(revised["files"][0], hash_state="verified",
                                  sha256="f" * 64)]
        revised["batch_manifest"] = __import__("clipboard_manifest_v2").build_manifest(
            revised["item_id"], 1, [{key: value for key, value in entries[0].items()
                                     if key != "index"}])
        identity = __import__("clipboard_manifest_v2").content_identity(
            revised["batch_manifest"])
        revised["sha256"] = identity
        revised["content_sha256"] = identity
        revised["payload"] = dict(revised["payload"], content_sha256=identity,
                                  sha256="e" * 64, size=3)
        replaced, _ = store.add_item(revised, replace_existing=True)
        self.assertEqual(replaced["item_id"], stored["item_id"])
        self.assertEqual(replaced["item_revision"], 1)
        self.assertEqual(replaced["content_sha256"], identity)
        self.assertEqual(len(store.list_items()), 1)

    def test_schema2_file_cross_field_corruption_is_rejected(self):
        import clipboard_files as cf
        source = self.profile_dir / "corrupt.bin"
        source.write_bytes(b"payload")
        base = cf.make_file_item([source])
        corruptions = []
        changed = copy.deepcopy(base)
        changed["file_count"] = 2
        corruptions.append(changed)
        changed = copy.deepcopy(base)
        changed["hash_state"] = "verified"
        corruptions.append(changed)
        changed = copy.deepcopy(base)
        changed["files"][0]["source_fingerprint"]["size"] = 99
        corruptions.append(changed)
        changed = copy.deepcopy(base)
        changed["batch_manifest"]["item_id"] = "other-item"
        corruptions.append(changed)
        for changed in corruptions:
            with self.subTest(fields=changed), self.assertRaises(ValueError):
                cm.version_item(changed)

    def test_finalized_schema2_cross_fields_reject_valid_redigested_corruption(self):
        import clipboard_files as cf
        import clipboard_manifest_v2 as mv2
        source = self.profile_dir / "verified.bin"
        source.write_bytes(b"payload")
        item = cf.make_file_item([source])
        item["item_revision"] = 1
        item["hash_state"] = "verified"
        item["files"][0]["hash_state"] = "verified"
        item["files"][0]["sha256"] = cm.sha256_bytes(b"payload")
        manifest_entry = dict(item["batch_manifest"]["entries"][0])
        manifest_entry.pop("index")
        manifest_entry.update(hash_state="verified", sha256=cm.sha256_bytes(b"payload"))
        item["batch_manifest"] = mv2.build_manifest(item["item_id"], 1, [manifest_entry])
        identity = mv2.content_identity(item["batch_manifest"])
        item["sha256"] = identity
        item["content_sha256"] = identity
        item["payload"] = dict(item["payload"], content_sha256=identity)
        cm.version_item(item)

        corruptions = []
        changed = copy.deepcopy(item)
        entry = dict(changed["batch_manifest"]["entries"][0])
        entry.pop("index")
        entry["sha256"] = "a" * 64
        changed["batch_manifest"] = mv2.build_manifest(changed["item_id"], 1, [entry])
        corruptions.append(changed)
        changed = copy.deepcopy(item)
        changed["sha256"] = "b" * 64
        changed["content_sha256"] = "b" * 64
        changed["payload"] = dict(changed["payload"], content_sha256="b" * 64)
        corruptions.append(changed)
        changed = copy.deepcopy(item)
        changed["files"][0]["sha256"] = "c" * 64
        corruptions.append(changed)
        for changed in corruptions:
            with self.assertRaises(ValueError):
                cm.version_item(changed)

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

    def test_current_item_protected_from_eviction(self):
        store = cs.ClipboardStore(str(self.root), "profile")
        # second has seq=0 (older), first has seq=1 (newer, current)
        second, _ = store.add_item(cm.make_text_item("second", seq=0), data=b"second")
        first, _ = store.add_item(cm.make_text_item("first", seq=1), data=b"first",
                                  make_current=True)

        # enforce limit=1, oldest non-pinned is second → evicted
        evicted = store.enforce_limits(1, 10**9)
        self.assertIn(second["item_id"], evicted)
        self.assertEqual(store.current_item_id, first["item_id"])

        # enforce limit=0 → would evict first BUT it's current → protected
        evicted = store.enforce_limits(0, 10**9)
        self.assertNotIn(first["item_id"], evicted)
        self.assertEqual(store.current_item_id, first["item_id"])

    def test_current_not_deleted_when_other_item_evicted(self):
        store = cs.ClipboardStore(str(self.root), "profile")
        current_item, _ = store.add_item(
            cm.make_text_item("current", seq=0), data=b"current", make_current=True)
        for i in range(5):
            store.add_item(cm.make_text_item(f"extra-{i}", seq=i + 1), data=b"x")

        store.enforce_limits(max_items=3, max_total_bytes=10**9)
        self.assertEqual(store.current_item_id, current_item["item_id"])

    def test_reset_current(self):
        store = cs.ClipboardStore(str(self.root), "profile")
        item, _ = store.add_item(cm.make_text_item("hello", seq=0), data=b"hello",
                                 make_current=True)
        self.assertIsNotNone(store.current_item_id)
        self.assertTrue(store.reset_current())
        self.assertIsNone(store.current_item_id)

    def test_reset_current_already_none(self):
        store = cs.ClipboardStore(str(self.root), "profile")
        self.assertTrue(store.reset_current())
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
                self.assertIsNone(receiver.store("sender").current_item_id)
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
                local = receiver.capture_text("sender", "local item")
                self.assertEqual(receiver.store("sender").current_item_id, local["item_id"])
                item = cm.version_item(cm.make_text_item("remote", seq=2),
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
                self.assertEqual(receiver.store("sender").current_item_id, local["item_id"])
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
                self.assertEqual(manager.store("peer-a").current_item_id, existing["item_id"])
                self.assertFalse(any(msg.get("type") == "clipboard_request_items"
                                     for _identity, msg in sent))
                self.assertNotEqual(existing["item_id"], remote["item_id"])

                stale = cm.build_manifest("peer-a", "remote-device", 9, [remote], None)
                manager._on_manifest("peer-a", stale)
                self.assertEqual(manager.store("peer-a").current_item_id, existing["item_id"])

                fresh = cm.build_manifest("peer-a", "remote-device", 11, [remote], None)
                manager._on_manifest("peer-a", fresh)
                self.assertEqual(manager.store("peer-a").current_item_id, existing["item_id"])
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
                self.assertIsNone(manager.store("peer-a").current_item_id)
            finally:
                manager.shutdown()

    def test_receive_finalization_failure_is_reported_without_masking(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-finalize-failure-") as root:
            sent = []
            manager = ClipboardManager(root, "local-device",
                                       lambda identity, msg: sent.append(msg), settings)
            payload = b"payload".ljust(cp.MIN_LEGACY_CHUNK_SIZE, b"\0")
            payload_sha = cm.sha256_bytes(payload)
            try:
                manager._on_start("peer-a", {
                    "type": "clipboard_transfer_start", "transfer_id": "transfer-one",
                    "item_id": "received-item", "sha256": payload_sha,
                    "total_size": len(payload),
                    "chunk_size": cp.MIN_LEGACY_CHUNK_SIZE, "chunk_count": 1,
                    "kind": cm.KIND_BINARY, "mime": "application/octet-stream",
                    "file_count": 0, "display_name": "payload.bin",
                })
                manager._on_chunk("peer-a", {
                    "type": "clipboard_transfer_chunk", "transfer_id": "transfer-one",
                    "item_id": "received-item", "chunk_index": 0, "offset": 0,
                    "size": cp.MIN_LEGACY_CHUNK_SIZE, "sha256": payload_sha,
                    "data": base64.b64encode(payload).decode("ascii"),
                })
                store = manager.store("peer-a")
                before = Path(store.index_path).read_bytes()
                with mock.patch.object(
                        store, "commit_received_item", side_effect=OSError("index failed")):
                    manager._on_complete("peer-a", {
                        "type": "clipboard_transfer_complete", "transfer_id": "transfer-one",
                        "item_id": "received-item", "sha256": payload_sha, "status": "ok",
                    })
                self.assertTrue(any(msg.get("type") == "clipboard_transfer_error" for msg in sent))
                self.assertGreaterEqual(manager.stats["failed"], 1)
                self.assertEqual(Path(store.index_path).read_bytes(), before)
                self.assertIsNone(store.get_item("received-item"))
                self.assertNotEqual(store.transfer_sessions_snapshot()["transfer-one"]["state"],
                                    ctt.TransferSessionState.completed)
            finally:
                manager.shutdown()

    def test_concurrent_duplicate_completion_finalizes_once(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-duplicate-complete-") as root:
            sent = []
            manager = ClipboardManager(root, "local-device",
                                       lambda _identity, msg: sent.append(msg), settings)
            payload = b"payload".ljust(cp.MIN_LEGACY_CHUNK_SIZE, b"\0")
            payload_sha = cm.sha256_bytes(payload)
            transfer_id = "duplicate-complete"
            item_id = "duplicate-complete-item"
            entered = threading.Event()
            release = threading.Event()
            try:
                manager._on_start("peer-a", cp.build_transfer_start(
                    transfer_id, item_id, payload_sha, len(payload), len(payload)))
                manager._on_chunk("peer-a", cp.build_transfer_chunk(
                    transfer_id, item_id, 0, 0, payload, payload_sha))
                assembler = manager._assemblers[transfer_id]["asm"]
                real_assemble = assembler.assemble
                assemble_calls = 0

                def blocked_assemble():
                    nonlocal assemble_calls
                    assemble_calls += 1
                    entered.set()
                    release.wait(2.0)
                    return real_assemble()

                assembler.assemble = blocked_assemble
                store = manager.store("peer-a")
                completion = cp.build_transfer_complete(transfer_id, item_id, payload_sha)
                with mock.patch.object(store, "commit_received_item",
                                       wraps=store.commit_received_item) as commit:
                    first = threading.Thread(
                        target=manager._on_complete, args=("peer-a", completion))
                    duplicate = threading.Thread(
                        target=manager._on_complete, args=("peer-a", completion))
                    first.start()
                    self.assertTrue(entered.wait(1.0))
                    duplicate.start()
                    duplicate.join(1.0)
                    self.assertFalse(duplicate.is_alive())
                    release.set()
                    first.join(2.0)
                self.assertFalse(first.is_alive())
                self.assertEqual(assemble_calls, 1)
                self.assertEqual(commit.call_count, 1)
                self.assertEqual(manager.stats["received_items"], 1)
                self.assertEqual(sum(msg.get("type") == cp.T_ACK for msg in sent), 1)
            finally:
                release.set()
                manager.shutdown()

    def test_lost_final_ack_replay_resends_ack_without_duplicate_commit(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-complete-replay-") as root:
            sent = []
            manager = ClipboardManager(root, "local-device",
                                       lambda _identity, msg: sent.append(msg), settings)
            payload = b"payload".ljust(cp.MIN_LEGACY_CHUNK_SIZE, b"\0")
            payload_sha = cm.sha256_bytes(payload)
            transfer_id = "complete-replay"
            item_id = "complete-replay-item"
            completion = cp.build_transfer_complete(transfer_id, item_id, payload_sha)
            store = manager.store("peer-a")
            try:
                manager._on_start("peer-a", cp.build_transfer_start(
                    transfer_id, item_id, payload_sha, len(payload), len(payload)))
                manager._on_chunk("peer-a", cp.build_transfer_chunk(
                    transfer_id, item_id, 0, 0, payload, payload_sha))
                with mock.patch.object(store, "commit_received_item",
                                       wraps=store.commit_received_item) as commit:
                    self.assertTrue(manager.handle("peer-a", completion))
                    self.assertNotIn(transfer_id, manager._assemblers)
                    self.assertTrue(manager.handle("peer-a", completion))

                self.assertEqual(commit.call_count, 1)
                self.assertEqual(len(store.list_items()), 1)
                self.assertEqual(manager.stats["received_items"], 1)
                acks = [msg for msg in sent if msg.get("type") == cp.T_ACK]
                self.assertEqual(acks, [
                    cp.build_transfer_ack(transfer_id, item_id),
                    cp.build_transfer_ack(transfer_id, item_id),
                ])
            finally:
                manager.shutdown()
                self.assertEqual(manager._completed_receipts, {})

    def test_final_ack_send_failure_preserves_commit_and_receipt_for_replay(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-ack-send-failure-") as root:
            sent = []
            fail_ack = True

            def send(_identity, message):
                nonlocal fail_ack
                if message.get("type") == cp.T_ACK and fail_ack:
                    fail_ack = False
                    raise OSError("injected ACK send failure")
                sent.append(message)

            manager = ClipboardManager(root, "local-device", send, settings)
            payload = b"payload".ljust(cp.MIN_LEGACY_CHUNK_SIZE, b"\0")
            payload_sha = cm.sha256_bytes(payload)
            transfer_id = "ack-send-failure"
            item_id = "ack-send-failure-item"
            completion = cp.build_transfer_complete(transfer_id, item_id, payload_sha)
            store = manager.store("peer-a")
            try:
                manager._on_start("peer-a", cp.build_transfer_start(
                    transfer_id, item_id, payload_sha, len(payload), len(payload)))
                manager._on_chunk("peer-a", cp.build_transfer_chunk(
                    transfer_id, item_id, 0, 0, payload, payload_sha))
                manager._on_complete("peer-a", completion)

                stored_items = store.list_items()
                self.assertEqual(len(stored_items), 1)
                self.assertTrue(stored_items[0]["available"])
                self.assertTrue(store.has_object(payload_sha))
                session = store.transfer_sessions_snapshot()[transfer_id]
                self.assertEqual(session["state"], ctt.TransferSessionState.completed)
                document = json.loads(Path(store.index_path).read_text(encoding="utf-8"))
                self.assertEqual(document["items"][0]["item_id"], item_id)
                self.assertEqual(document["transfer_sessions"][transfer_id]["state"],
                                 ctt.TransferSessionState.completed)
                self.assertIn(transfer_id, manager._completed_receipts)
                self.assertEqual(manager.stats["received_items"], 1)
                self.assertEqual(manager.stats["failed"], 0)

                manager._on_complete("peer-a", completion)
                self.assertEqual(sent, [cp.build_transfer_ack(transfer_id, item_id)])
                self.assertEqual(len(store.list_items()), 1)
                self.assertEqual(manager.stats["received_items"], 1)
                self.assertIn(transfer_id, manager._completed_receipts)
            finally:
                manager.shutdown()

    def test_completed_receive_replays_final_ack_after_restart(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-restart-ack-") as root:
            payload = b"payload".ljust(cp.MIN_LEGACY_CHUNK_SIZE, b"\0")
            payload_sha = cm.sha256_bytes(payload)
            transfer_id = "restart-final-ack"
            item_id = "restart-final-ack-item"
            completion = cp.build_transfer_complete(transfer_id, item_id, payload_sha)
            first = ClipboardManager(root, "local-device", lambda _identity, _msg: None, settings)
            first._on_start("peer-a", cp.build_transfer_start(
                transfer_id, item_id, payload_sha, len(payload), len(payload)))
            first._on_chunk("peer-a", cp.build_transfer_chunk(
                transfer_id, item_id, 0, 0, payload, payload_sha))
            first._on_complete("peer-a", completion)
            session = first.store("peer-a").transfer_sessions_snapshot()[transfer_id]
            self.assertEqual(session["state"], ctt.TransferSessionState.completed)
            self.assertEqual(session["progress"]["payload_sha256"], payload_sha)
            first.shutdown()

            sent = []
            restarted = ClipboardManager(
                root, "local-device", lambda _identity, msg: sent.append(msg), settings)
            try:
                self.assertTrue(restarted.handle("peer-a", completion))
                self.assertEqual(sent, [cp.build_transfer_ack(transfer_id, item_id)])
                self.assertEqual(len(restarted.store("peer-a").list_items()), 1)
            finally:
                restarted.shutdown()

    def test_restart_completion_mismatch_has_no_persisted_ack(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-restart-ack-mismatch-") as root:
            payload = b"payload".ljust(cp.MIN_LEGACY_CHUNK_SIZE, b"\0")
            payload_sha = cm.sha256_bytes(payload)
            transfer_id = "restart-ack-mismatch"
            item_id = "restart-ack-mismatch-item"
            first = ClipboardManager(root, "local-device", lambda _identity, _msg: None, settings)
            first._on_start("peer-a", cp.build_transfer_start(
                transfer_id, item_id, payload_sha, len(payload), len(payload)))
            first._on_chunk("peer-a", cp.build_transfer_chunk(
                transfer_id, item_id, 0, 0, payload, payload_sha))
            first._on_complete(
                "peer-a", cp.build_transfer_complete(transfer_id, item_id, payload_sha))
            first.shutdown()

            sent = []
            restarted = ClipboardManager(
                root, "local-device", lambda _identity, msg: sent.append(msg), settings)
            try:
                mismatches = [
                    ("peer-b", cp.build_transfer_complete(transfer_id, item_id, payload_sha)),
                    ("peer-a", cp.build_transfer_complete(
                        transfer_id, "wrong-item", payload_sha)),
                    ("peer-a", cp.build_transfer_complete(
                        transfer_id, item_id, "f" * 64)),
                    ("peer-a", cp.build_transfer_complete(
                        "wrong-transfer", item_id, payload_sha)),
                ]
                for identity, message in mismatches:
                    self.assertTrue(restarted.handle(identity, message))
                self.assertFalse(any(message.get("type") == cp.T_ACK for message in sent))
            finally:
                restarted.shutdown()

    def test_restart_completion_without_physical_object_has_no_ack(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-restart-ack-missing-") as root:
            payload = b"payload".ljust(cp.MIN_LEGACY_CHUNK_SIZE, b"\0")
            payload_sha = cm.sha256_bytes(payload)
            transfer_id = "restart-ack-missing"
            item_id = "restart-ack-missing-item"
            first = ClipboardManager(root, "local-device", lambda _identity, _msg: None, settings)
            first._on_start("peer-a", cp.build_transfer_start(
                transfer_id, item_id, payload_sha, len(payload), len(payload)))
            first._on_chunk("peer-a", cp.build_transfer_chunk(
                transfer_id, item_id, 0, 0, payload, payload_sha))
            first._on_complete(
                "peer-a", cp.build_transfer_complete(transfer_id, item_id, payload_sha))
            store = first.store("peer-a")
            object_path = store.object_path(store.get_item(item_id)["sha256"])
            first.shutdown()
            os.remove(object_path)

            sent = []
            restarted = ClipboardManager(
                root, "local-device", lambda _identity, msg: sent.append(msg), settings)
            try:
                self.assertTrue(restarted.handle(
                    "peer-a", cp.build_transfer_complete(transfer_id, item_id, payload_sha)))
                self.assertFalse(any(message.get("type") == cp.T_ACK for message in sent))
                stored = restarted.store("peer-a").get_item(item_id)
                self.assertFalse(stored["available"])
                self.assertEqual(stored["payload_state"], "missing")
                self.assertNotIn(
                    transfer_id, restarted.store("peer-a").transfer_sessions_snapshot())
                self.assertEqual(
                    restarted._jobs[transfer_id].status, ctt.TransferStatus.failed)
                self.assertEqual(
                    restarted._jobs[transfer_id].session.state,
                    ctt.TransferSessionState.failed)
            finally:
                restarted.shutdown()

    def test_restart_completion_with_same_size_corrupt_object_has_no_ack(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-restart-ack-corrupt-") as root:
            payload = b"payload".ljust(cp.MIN_LEGACY_CHUNK_SIZE, b"\0")
            payload_sha = cm.sha256_bytes(payload)
            transfer_id = "restart-ack-corrupt"
            item_id = "restart-ack-corrupt-item"
            completion = cp.build_transfer_complete(transfer_id, item_id, payload_sha)
            first = ClipboardManager(root, "local-device", lambda _identity, _msg: None, settings)
            first._on_start("peer-a", cp.build_transfer_start(
                transfer_id, item_id, payload_sha, len(payload), len(payload)))
            first._on_chunk("peer-a", cp.build_transfer_chunk(
                transfer_id, item_id, 0, 0, payload, payload_sha))
            first._on_complete("peer-a", completion)
            store = first.store("peer-a")
            object_path = Path(store.object_path(store.get_item(item_id)["sha256"]))
            first.shutdown()
            object_path.write_bytes(b"x" + payload[1:])

            sent = []
            restarted = ClipboardManager(
                root, "local-device", lambda _identity, msg: sent.append(msg), settings)
            try:
                self.assertTrue(restarted.handle("peer-a", completion))
                self.assertFalse(any(message.get("type") == cp.T_ACK for message in sent))
                stored = restarted.store("peer-a").get_item(item_id)
                self.assertFalse(stored["available"])
                self.assertEqual(stored["payload_state"], "missing")
                self.assertNotIn(
                    transfer_id, restarted.store("peer-a").transfer_sessions_snapshot())
                local = next(provider for provider in stored["providers"]
                             if provider["device_id"] == "local-device")
                self.assertEqual(local["state"], "unavailable")
                self.assertEqual(
                    restarted._jobs[transfer_id].status, ctt.TransferStatus.failed)
                self.assertEqual(
                    restarted._jobs[transfer_id].session.state,
                    ctt.TransferSessionState.failed)
                self.assertTrue(list(object_path.parent.glob(f"{payload_sha}.corrupt-*")))
            finally:
                restarted.shutdown()

    def test_restart_after_corrupt_object_quarantine_rename_failure_rejects_all_evidence(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-corrupt-rename-failure-") as root:
            payload = b"payload".ljust(cp.MIN_LEGACY_CHUNK_SIZE, b"\0")
            payload_sha = cm.sha256_bytes(payload)
            transfer_id = "corrupt-rename-failure"
            item_id = "corrupt-rename-failure-item"
            completion = cp.build_transfer_complete(transfer_id, item_id, payload_sha)
            first = ClipboardManager(root, "local-device", lambda _identity, _msg: None, settings)
            first._on_start("peer-a", cp.build_transfer_start(
                transfer_id, item_id, payload_sha, len(payload), len(payload)))
            first._on_chunk("peer-a", cp.build_transfer_chunk(
                transfer_id, item_id, 0, 0, payload, payload_sha))
            first._on_complete("peer-a", completion)
            store = first.store("peer-a")
            store.record_cache_entry(payload_sha, payload_sha, len(payload))
            object_path = Path(store.object_path(payload_sha))
            first.shutdown()
            object_path.write_bytes(b"x" + payload[1:])

            sent = []
            restarted = ClipboardManager(
                root, "local-device", lambda _identity, message: sent.append(message), settings)
            store = restarted.store("peer-a")
            real_replace = os.replace

            def fail_quarantine(source, destination):
                if (os.path.abspath(source) == os.path.abspath(object_path)
                        and ".corrupt-" in os.path.basename(destination)):
                    raise OSError("injected quarantine rename failure")
                return real_replace(source, destination)

            try:
                with mock.patch.object(cs.os, "replace", side_effect=fail_quarantine):
                    self.assertTrue(restarted.handle("peer-a", completion))
                self.assertFalse(any(message.get("type") == cp.T_ACK for message in sent))
                self.assertTrue(object_path.exists())
                persisted = json.loads(Path(store.index_path).read_text(encoding="utf-8"))
                self.assertEqual(persisted["integrity_tombstones"][payload_sha]["reason"],
                                 "object_integrity_failure")
                self.assertNotIn(str(object_path),
                                 json.dumps(persisted["integrity_tombstones"]))
                self.assertNotIn(transfer_id, store.transfer_sessions_snapshot())
                self.assertIsNone(store.get_cache_entry(payload_sha))
                self.assertFalse(store.get_item(item_id)["available"])
                self.assertEqual(store.get_item(item_id)["payload_state"], "missing")
                self.assertIsNone(store.get_object_path_for_item(item_id))
                manifest_item = store.build_manifest("local-device")["items"][0]
                self.assertFalse(manifest_item["available"])
                self.assertEqual(next(provider for provider in manifest_item["providers"]
                                      if provider["device_id"] == "local-device")["state"],
                                 "unavailable")
                self.assertIn("quarantine retry required",
                               restarted._jobs[transfer_id].error)
            finally:
                restarted.shutdown()

            sent_after_restart = []
            reopened = ClipboardManager(
                root, "local-device",
                lambda _identity, message: sent_after_restart.append(message), settings)
            try:
                reopened_store = reopened.store("peer-a")
                self.assertTrue(object_path.exists())
                self.assertFalse(reopened_store.has_committed_object(payload_sha))
                self.assertIsNone(reopened_store.get_cache_entry(payload_sha))
                self.assertNotIn(transfer_id, reopened_store.transfer_sessions_snapshot())
                loaded = reopened_store.get_item(item_id)
                self.assertFalse(loaded["available"])
                self.assertEqual(loaded["payload_state"], "missing")
                self.assertTrue(all(provider["state"] == "unavailable"
                                    for provider in loaded["providers"]))
                self.assertTrue(reopened.handle("peer-a", completion))
                self.assertFalse(any(message.get("type") == cp.T_ACK
                                     for message in sent_after_restart))
            finally:
                reopened.shutdown()

    def test_verified_repair_atomically_clears_integrity_tombstone(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-corrupt-repair-") as root:
            payload = b"verified repair payload"
            digest = cm.sha256_bytes(payload)
            item_id = "verified-repair-item"
            original_transfer_id = "verified-repair-corrupt"
            store = cs.ClipboardStore(root, "profile")
            item = cm.make_binary_item(digest, len(payload), seq=0)
            item["item_id"] = item_id
            item["origin"]["event_id"] = item_id
            item["providers"] = [{
                "device_id": "local-device", "state": "available",
                "last_seen_at": 1.0, "payload_sha256": digest,
                "payload_size": len(payload),
            }]
            item = cm.version_item(item, payload_state="cached")
            original_session = ctt.TransferSession(
                transfer_id=original_transfer_id, direction="receive", item_id=item_id,
                item_revision=cm.item_revision(item), profile="profile",
                peer_identity="peer-a", provider="peer-a",
                strategy=ctt.LEGACY_ZIP_V1_STRATEGY, manifest_digest=digest,
                logical_bytes=len(payload), file_count=0, remaining_bytes=0,
                state=ctt.TransferSessionState.completed,
                progress={"payload_sha256": digest})
            store.commit_received_item(
                item, original_session.snapshot(), data=payload,
                received_cache=(digest, digest, len(payload), []))
            Path(store.object_path(digest)).write_bytes(b"x" + payload[1:])
            self.assertFalse(store.verify_object(digest, len(payload), digest,
                                                 local_device_id="local-device"))
            store.invalidate_completed_receipt(
                original_transfer_id, item_id, digest, local_device_id="local-device")

            repair_transfer_id = "verified-repair-success"
            repaired = copy.deepcopy(item)
            repaired["payload_state"] = "cached"
            repaired["available"] = True
            for provider in repaired["providers"]:
                provider["state"] = "available"
            repair_session = ctt.TransferSession(
                transfer_id=repair_transfer_id, direction="receive", item_id=item_id,
                item_revision=cm.item_revision(repaired), profile="profile",
                peer_identity="peer-a", provider="peer-a",
                strategy=ctt.LEGACY_ZIP_V1_STRATEGY, manifest_digest=digest,
                logical_bytes=len(payload), file_count=0, remaining_bytes=0,
                state=ctt.TransferSessionState.completed,
                progress={"payload_sha256": digest})
            before_failed_repair = Path(store.index_path).read_bytes()
            real_replace = os.replace

            def fail_repair_index(source, destination):
                if os.path.abspath(destination) == os.path.abspath(store.index_path):
                    raise OSError("injected repair index failure")
                return real_replace(source, destination)

            with mock.patch.object(cs.os, "replace", side_effect=fail_repair_index):
                with self.assertRaises(OSError):
                    store.commit_received_item(
                        repaired, repair_session.snapshot(), data=payload,
                        replace_existing=True,
                        received_cache=(digest, digest, len(payload), []))
            self.assertEqual(Path(store.index_path).read_bytes(), before_failed_repair)
            self.assertFalse(Path(store.object_path(digest)).exists())
            self.assertFalse(store.has_committed_object(digest))
            self.assertNotIn(repair_transfer_id, store.transfer_sessions_snapshot())
            self.assertIsNone(store.get_cache_entry(digest))

            store.commit_received_item(
                repaired, repair_session.snapshot(), data=payload, replace_existing=True,
                received_cache=(digest, digest, len(payload), []))

            document = json.loads(Path(store.index_path).read_text(encoding="utf-8"))
            self.assertNotIn(digest, document["integrity_tombstones"])
            self.assertEqual(document["items"][0]["payload_state"], "cached")
            self.assertIn(digest, document["received_cache"])
            self.assertEqual(document["transfer_sessions"][repair_transfer_id]["state"],
                             ctt.TransferSessionState.completed)
            reopened = cs.ClipboardStore(root, "profile")
            self.assertTrue(reopened.has_committed_object(digest))
            self.assertEqual(reopened.get_data(item_id), payload)
            self.assertTrue(reopened.get_item(item_id)["available"])
            self.assertIsNotNone(reopened.get_cache_entry(digest))
            self.assertIn(repair_transfer_id, reopened.transfer_sessions_snapshot())

    def test_integrity_tombstones_are_bounded_and_preserve_unknown_metadata(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-tombstone-bounds-") as root:
            store = cs.ClipboardStore(root, "profile")
            item, _ = store.add_item(cm.make_text_item("hello", seq=0), data=b"hello")
            index_path = Path(store.index_path)
            document = json.loads(index_path.read_text(encoding="utf-8"))
            document["future_key"] = {"keep": True}
            document["integrity_tombstones"] = {
                f"{index:064x}": {
                    "detected_at": float(index + 1),
                    "reason": "object_integrity_failure",
                    "future_metadata": {"keep": index},
                }
                for index in range(cs.MAX_INTEGRITY_TOMBSTONES + 20)
            }
            index_path.write_text(json.dumps(document), encoding="utf-8")

            reopened = cs.ClipboardStore(root, "profile")
            persisted = json.loads(index_path.read_text(encoding="utf-8"))
            tombstones = persisted["integrity_tombstones"]
            newest = f"{cs.MAX_INTEGRITY_TOMBSTONES + 19:064x}"
            self.assertEqual(len(tombstones), cs.MAX_INTEGRITY_TOMBSTONES)
            self.assertEqual(tombstones[newest]["future_metadata"],
                             {"keep": cs.MAX_INTEGRITY_TOMBSTONES + 19})
            self.assertEqual(persisted["future_key"], {"keep": True})
            self.assertEqual(reopened.get_data(item["item_id"]), b"hello")

    def test_crash_after_quarantine_reopens_without_delivery_or_ack(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-corrupt-quarantine-crash-") as root:
            payload = b"payload".ljust(cp.MIN_LEGACY_CHUNK_SIZE, b"\0")
            payload_sha = cm.sha256_bytes(payload)
            transfer_id = "corrupt-quarantine-crash"
            item_id = "corrupt-quarantine-crash-item"
            completion = cp.build_transfer_complete(transfer_id, item_id, payload_sha)
            first = ClipboardManager(root, "local-device", lambda _identity, _msg: None, settings)
            first._on_start("peer-a", cp.build_transfer_start(
                transfer_id, item_id, payload_sha, len(payload), len(payload)))
            first._on_chunk("peer-a", cp.build_transfer_chunk(
                transfer_id, item_id, 0, 0, payload, payload_sha))
            first._on_complete("peer-a", completion)
            store = first.store("peer-a")
            object_path = Path(store.object_path(payload_sha))
            first.shutdown()
            object_path.write_bytes(b"x" + payload[1:])

            class InjectedCrash(BaseException):
                pass

            crashed = cs.ClipboardStore(root, cs.profile_dir_name("peer-a"))
            self.assertFalse(crashed.verify_object(payload_sha, len(payload), payload_sha))
            with mock.patch.object(crashed, "_save", side_effect=InjectedCrash()):
                with self.assertRaises(InjectedCrash):
                    crashed.invalidate_completed_receipt(
                        transfer_id, item_id, payload_sha, local_device_id="local-device")
            self.assertTrue(object_path.exists())

            sent = []
            reopened = ClipboardManager(
                root, "local-device", lambda _identity, message: sent.append(message), settings)
            try:
                self.assertTrue(reopened.handle("peer-a", completion))
                restored_store = reopened.store("peer-a")
                restored = restored_store.get_item(item_id)
                self.assertFalse(restored["available"])
                self.assertEqual(restored["payload_state"], "missing")
                self.assertIsNone(restored_store.get_object_path_for_item(item_id))
                self.assertFalse(any(message.get("type") == cp.T_ACK for message in sent))
                self.assertNotIn(transfer_id, restored_store.transfer_sessions_snapshot())
            finally:
                reopened.shutdown()

    def test_corrupt_shared_object_invalidates_all_dedup_references(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-corrupt-shared-object-") as root:
            manager = ClipboardManager(
                root, "local-device", lambda _identity, _message: None, settings)
            store = manager.store("peer-a")
            payload = b"shared corrupt payload"
            digest = cm.sha256_bytes(payload)
            item_ids = ["shared-corrupt-item-a", "shared-corrupt-item-b"]
            transfer_ids = ["shared-corrupt-transfer-a", "shared-corrupt-transfer-b"]
            try:
                for item_id, transfer_id in zip(item_ids, transfer_ids):
                    item = cm.make_binary_item(digest, len(payload), seq=0)
                    item["item_id"] = item_id
                    item["origin"]["event_id"] = item_id
                    item["providers"] = [{
                        "device_id": "local-device", "state": "available",
                        "last_seen_at": 1.0, "payload_sha256": digest,
                        "payload_size": len(payload),
                    }]
                    item = cm.version_item(item, payload_state="cached")
                    session = ctt.TransferSession(
                        transfer_id=transfer_id, direction="receive", item_id=item_id,
                        item_revision=cm.item_revision(item), profile=store.profile_id,
                        peer_identity="peer-a", provider="peer-a",
                        strategy=ctt.LEGACY_ZIP_V1_STRATEGY, manifest_digest=digest,
                        logical_bytes=len(payload), file_count=0, remaining_bytes=0,
                        state=ctt.TransferSessionState.completed,
                        progress={"payload_sha256": digest})
                    store.commit_received_item(
                        item, session.snapshot(), data=payload,
                        received_cache=(digest, digest, len(payload), [{
                            "device_id": "peer-a", "state": "available",
                            "last_seen_at": 1.0,
                        }]))
                    manager._restore_session_job_locked(store, session.snapshot())
                    manager._completed_receipts[transfer_id] = (
                        "peer-a", item_id, digest, time.monotonic() + 60)

                object_path = Path(store.object_path(digest))
                object_path.write_bytes(b"x" + payload[1:])
                completion = cp.parse_transfer_complete(cp.build_transfer_complete(
                    transfer_ids[0], item_ids[0], digest))
                self.assertFalse(manager._has_persisted_completed_receipt(
                    "peer-a", completion))

                self.assertFalse(object_path.exists())
                self.assertEqual(store.cache_entries_snapshot(), {})
                self.assertEqual(store.transfer_sessions_snapshot(), {})
                self.assertEqual(manager._completed_receipts, {})
                for item_id, transfer_id in zip(item_ids, transfer_ids):
                    stored = store.get_item(item_id)
                    self.assertFalse(stored["available"])
                    self.assertEqual(stored["payload_state"], "missing")
                    self.assertEqual(next(provider for provider in stored["providers"]
                                          if provider["device_id"] == "local-device")["state"],
                                     "unavailable")
                    self.assertIsNone(store.get_object_path_for_item(item_id))
                    self.assertEqual(manager._jobs[transfer_id].status,
                                     ctt.TransferStatus.failed)
                manifest = store.build_manifest("local-device")
                self.assertTrue(all(not item["available"] for item in manifest["items"]))
                self.assertTrue(all(
                    next(provider for provider in item["providers"]
                         if provider["device_id"] == "local-device")["state"] == "unavailable"
                    for item in manifest["items"]))
            finally:
                manager.shutdown()

    def test_ram_receive_replaces_same_size_corrupt_dedup_object(self):
        self._assert_receive_replaces_same_size_corrupt_object(use_disk=False)

    def test_disk_receive_replaces_same_size_corrupt_dedup_object(self):
        self._assert_receive_replaces_same_size_corrupt_object(use_disk=True)

    def _assert_receive_replaces_same_size_corrupt_object(self, use_disk):
        with tempfile.TemporaryDirectory(prefix="flowshift-receive-corrupt-dedup-") as root:
            payload = b"verified incoming payload".ljust(cp.MIN_LEGACY_CHUNK_SIZE, b"\0")
            payload_sha = cm.sha256_bytes(payload)
            transfer_id = f"corrupt-dedup-{'disk' if use_disk else 'ram'}"
            item_id = f"corrupt-dedup-item-{'disk' if use_disk else 'ram'}"
            sent = []
            manager = ClipboardManager(
                root, "local-device", lambda _identity, message: sent.append(message), settings)
            store = manager.store("peer-a")
            object_path = Path(store.object_path(payload_sha))
            store.write_object(payload_sha, b"x" + payload[1:])
            try:
                threshold = 0 if use_disk else len(payload)
                with mock.patch.object(
                        manager, "_transfer_settings",
                        wraps=manager._transfer_settings) as transfer_settings:
                    configured = manager._transfer_settings()
                    configured["disk_assembler_threshold_bytes"] = threshold
                    transfer_settings.return_value = configured
                    manager._on_start("peer-a", cp.build_transfer_start(
                        transfer_id, item_id, payload_sha, len(payload), len(payload)))
                assembler = manager._assemblers[transfer_id]["asm"]
                if use_disk:
                    self.assertIsInstance(assembler, ctt.DiskChunkAssembler)
                else:
                    self.assertNotIsInstance(assembler, ctt.DiskChunkAssembler)
                manager._on_chunk("peer-a", cp.build_transfer_chunk(
                    transfer_id, item_id, 0, 0, payload, payload_sha))
                manager._on_complete(
                    "peer-a", cp.build_transfer_complete(transfer_id, item_id, payload_sha))

                self.assertEqual(object_path.read_bytes(), payload)
                self.assertTrue(store.verify_object(payload_sha, len(payload), payload_sha))
                self.assertTrue(store.get_item(item_id)["available"])
                self.assertEqual(
                    store.transfer_sessions_snapshot()[transfer_id]["state"],
                    ctt.TransferSessionState.completed)
                self.assertEqual(
                    [message for message in sent if message.get("type") == cp.T_ACK],
                    [cp.build_transfer_ack(transfer_id, item_id)])
                self.assertEqual(manager.stats["received_items"], 1)
                self.assertEqual(manager.stats["failed"], 0)
            finally:
                manager.shutdown()

    def test_mismatched_completion_replays_receive_no_ack(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-complete-replay-mismatch-") as root:
            sent = []
            manager = ClipboardManager(root, "local-device",
                                       lambda _identity, msg: sent.append(msg), settings)
            payload = b"payload".ljust(cp.MIN_LEGACY_CHUNK_SIZE, b"\0")
            payload_sha = cm.sha256_bytes(payload)
            transfer_id = "complete-replay-mismatch"
            item_id = "complete-replay-mismatch-item"
            store = manager.store("peer-a")
            try:
                manager._on_start("peer-a", cp.build_transfer_start(
                    transfer_id, item_id, payload_sha, len(payload), len(payload)))
                manager._on_chunk("peer-a", cp.build_transfer_chunk(
                    transfer_id, item_id, 0, 0, payload, payload_sha))
                with mock.patch.object(store, "commit_received_item",
                                       wraps=store.commit_received_item) as commit:
                    self.assertTrue(manager.handle("peer-a", cp.build_transfer_complete(
                        transfer_id, item_id, payload_sha)))
                    sent.clear()
                    self.assertTrue(manager.handle("peer-b", cp.build_transfer_complete(
                        transfer_id, item_id, payload_sha)))
                    self.assertTrue(manager.handle("peer-a", cp.build_transfer_complete(
                        transfer_id, "wrong-item", payload_sha)))
                    self.assertTrue(manager.handle("peer-a", cp.build_transfer_complete(
                        transfer_id, item_id, "f" * 64)))

                self.assertFalse(any(msg.get("type") == cp.T_ACK for msg in sent))
                self.assertEqual(commit.call_count, 1)
                self.assertEqual(len(store.list_items()), 1)
                self.assertEqual(manager.stats["received_items"], 1)
            finally:
                manager.shutdown()

    def test_completed_receipts_are_bounded(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-complete-receipts-") as root:
            manager = ClipboardManager(root, "local-device",
                                       lambda _identity, _msg: None, settings)
            payload = b"payload".ljust(cp.MIN_LEGACY_CHUNK_SIZE, b"\0")
            payload_sha = cm.sha256_bytes(payload)
            try:
                with mock.patch("clipboard_runtime._MAX_COMPLETED_RECEIPTS", 2):
                    for index in range(3):
                        transfer_id = f"bounded-receipt-{index}"
                        item_id = f"bounded-receipt-item-{index}"
                        manager._on_start("peer-a", cp.build_transfer_start(
                            transfer_id, item_id, payload_sha, len(payload), len(payload)))
                        manager._on_chunk("peer-a", cp.build_transfer_chunk(
                            transfer_id, item_id, 0, 0, payload, payload_sha))
                        manager._on_complete("peer-a", cp.build_transfer_complete(
                            transfer_id, item_id, payload_sha))
                self.assertEqual(list(manager._completed_receipts), [
                    "bounded-receipt-1", "bounded-receipt-2"])
            finally:
                manager.shutdown()

    def test_receive_delete_during_finalization_prevents_resurrection(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-finalize-delete-") as root:
            sent = []
            manager = ClipboardManager(root, "local-device",
                                       lambda _identity, msg: sent.append(msg), settings)
            payload = b"payload".ljust(cp.MIN_LEGACY_CHUNK_SIZE, b"\0")
            payload_sha = cm.sha256_bytes(payload)
            transfer_id = "delete-finalizing"
            item_id = "delete-finalizing-item"
            verified = threading.Event()
            release = threading.Event()
            try:
                manager._on_start("peer-a", cp.build_transfer_start(
                    transfer_id, item_id, payload_sha, len(payload), len(payload)))
                manager._on_chunk("peer-a", cp.build_transfer_chunk(
                    transfer_id, item_id, 0, 0, payload, payload_sha))
                assembler = manager._assemblers[transfer_id]["asm"]
                real_assemble = assembler.assemble

                def blocked_assemble():
                    data = real_assemble()
                    verified.set()
                    release.wait(2.0)
                    return data

                assembler.assemble = blocked_assemble
                thread = threading.Thread(target=manager._on_complete, args=(
                    "peer-a", cp.build_transfer_complete(transfer_id, item_id, payload_sha)))
                thread.start()
                self.assertTrue(verified.wait(1.0))
                self.assertFalse(manager.delete_item("peer-a", item_id))
                release.set()
                thread.join(2.0)
                self.assertFalse(thread.is_alive())
                self.assertIsNone(manager.store("peer-a").get_item(item_id))
                self.assertFalse(any(msg.get("type") == cp.T_ACK for msg in sent))
            finally:
                release.set()
                manager.shutdown()

    def test_receive_clear_after_final_commit_removes_item(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-finalize-clear-") as root:
            manager = ClipboardManager(root, "local-device",
                                       lambda _identity, _msg: None, settings)
            payload = b"payload".ljust(cp.MIN_LEGACY_CHUNK_SIZE, b"\0")
            payload_sha = cm.sha256_bytes(payload)
            transfer_id = "clear-finalized"
            item_id = "clear-finalized-item"
            committed = threading.Event()
            release = threading.Event()
            store = manager.store("peer-a")
            real_commit = store.commit_received_item

            def blocked_commit(*args, **kwargs):
                result = real_commit(*args, **kwargs)
                committed.set()
                release.wait(2.0)
                return result

            try:
                manager._on_start("peer-a", cp.build_transfer_start(
                    transfer_id, item_id, payload_sha, len(payload), len(payload)))
                manager._on_chunk("peer-a", cp.build_transfer_chunk(
                    transfer_id, item_id, 0, 0, payload, payload_sha))
                with mock.patch.object(store, "commit_received_item",
                                       side_effect=blocked_commit):
                    finalizer = threading.Thread(target=manager._on_complete, args=(
                        "peer-a", cp.build_transfer_complete(transfer_id, item_id, payload_sha)))
                    finalizer.start()
                    self.assertTrue(committed.wait(1.0))
                    cleared = threading.Event()
                    clearer = threading.Thread(target=lambda: (
                        manager.clear("peer-a"), cleared.set()))
                    clearer.start()
                    self.assertFalse(cleared.wait(0.05))
                    release.set()
                    finalizer.join(2.0)
                    clearer.join(2.0)
                self.assertIsNone(store.get_item(item_id))
                self.assertEqual(store.list_items(), [])
            finally:
                release.set()
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
                    manager._bind_received_payload(item, "b" * 64, 5)
            finally:
                manager.shutdown()

    def test_legacy_receive_rejects_mismatched_chunks_without_retaining(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-chunk-validation-") as root:
            manager = ClipboardManager(root, "local-device", lambda _identity, _msg: None,
                                       settings)
            payload = b"payload".ljust(cp.MIN_LEGACY_CHUNK_SIZE, b"\0")
            transfer_id = "strict-transfer"
            start = cp.build_transfer_start(
                transfer_id, "strict-item", cm.sha256_bytes(payload), len(payload), len(payload))
            try:
                manager._on_start("peer-a", start)
                assembler = manager._assemblers[transfer_id]["asm"]
                invalid = [
                    cp.build_transfer_chunk(transfer_id, "wrong-item", 0, 0, payload),
                    cp.build_transfer_chunk(transfer_id, "strict-item", 1000, 0, payload),
                    cp.build_transfer_chunk(transfer_id, "strict-item", 0, 1, payload),
                    cp.build_transfer_chunk(transfer_id, "strict-item", 0, 0, payload[:-1]),
                ]
                invalid[-1]["size"] = len(payload)
                for message in invalid:
                    manager._on_chunk("peer-a", message)
                manager._on_chunk("peer-b", cp.build_transfer_chunk(
                    transfer_id, "strict-item", 0, 0, payload))
                self.assertEqual(assembler.bytes_received, 0)
                self.assertEqual(assembler.next_index, 0)
                manager._on_complete("peer-a", cp.build_transfer_complete(
                    transfer_id, "strict-item", cm.sha256_bytes(payload)))
                self.assertIn(transfer_id, manager._assemblers)

                manager._on_chunk("peer-a", cp.build_transfer_chunk(
                    transfer_id, "strict-item", 0, 0, payload))
                manager._on_complete("peer-b", cp.build_transfer_complete(
                    transfer_id, "strict-item", cm.sha256_bytes(payload)))
                self.assertIn(transfer_id, manager._assemblers)
                manager._on_complete("peer-a", cp.build_transfer_complete(
                    transfer_id, "wrong-item", cm.sha256_bytes(payload)))
                self.assertIn(transfer_id, manager._assemblers)
                manager._on_complete("peer-a", cp.build_transfer_complete(
                    transfer_id, "strict-item", cm.sha256_bytes(payload)))
                self.assertNotIn(transfer_id, manager._assemblers)
            finally:
                manager.shutdown()

    def test_malformed_transfer_control_messages_do_not_crash_handle(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-control-validation-") as root:
            manager = ClipboardManager(root, "local-device", lambda _identity, _msg: None,
                                       settings)
            try:
                malformed = [
                    {"type": cp.T_START, "transfer_id": "t", "item_id": "i",
                     "sha256": "a" * 64, "total_size": -1, "chunk_size": 1,
                     "chunk_count": 1},
                    {"type": cp.T_CHUNK, "transfer_id": "t", "item_id": "i",
                     "chunk_index": 0, "offset": 0, "size": 1, "data": "%%%"},
                    {"type": cp.T_COMPLETE, "transfer_id": "t", "item_id": "i",
                     "sha256": "bad"},
                    {"type": cp.T_ERROR, "transfer_id": "t", "item_id": "i",
                     "code": "x" * 129},
                    {"type": cp.T_RESUME, "transfer_id": "t", "item_id": "i",
                     "next_index": -1},
                ]
                for message in malformed:
                    self.assertTrue(manager.handle("peer-a", message))
                self.assertEqual(manager._assemblers, {})
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

    def test_remove_ghost_cache_entries_removes_orphaned(self):
        item = cm.make_text_item("hello", seq=1)
        self.store.add_item(item, data=b"hello")
        self.store.record_cache_entry(item["sha256"], payload_size=10)
        self.store.record_cache_entry("b" * 64, payload_size=10)
        self.store.record_cache_entry("c" * 64, payload_size=10)
        ghost = self.store.remove_ghost_cache_entries()
        self.assertNotIn(item["sha256"], ghost)
        self.assertIn("b" * 64, ghost)
        self.assertIn("c" * 64, ghost)
        self.assertIsNone(self.store.get_cache_entry("b" * 64))
        self.assertIsNotNone(self.store.get_cache_entry(item["sha256"]))

    def test_remove_ghost_cache_entries_noop_when_no_ghosts(self):
        item = cm.make_text_item("hello", seq=1)
        self.store.add_item(item, data=b"hello")
        self.store.record_cache_entry(item["sha256"], payload_size=10)
        ghost = self.store.remove_ghost_cache_entries()
        self.assertEqual(ghost, {})
        self.assertIsNotNone(self.store.get_cache_entry(item["sha256"]))

    def test_evict_cache_respects_per_peer_target_bytes(self):
        self.store.record_cache_entry("a" * 64, payload_size=100)
        self.store.record_cache_entry("b" * 64, payload_size=100)
        self.store.record_cache_entry("c" * 64, payload_size=100)
        # target 150 bytes → evict oldest (LRU order: a, b) until ≤150
        evicted = self.store.evict_cache(target_unique_bytes=150)
        self.assertEqual(len(evicted), 2)
        self.assertIn("a" * 64, evicted)
        self.assertIn("b" * 64, evicted)
        self.assertNotIn("c" * 64, evicted)

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
            item = disabled_mgr._bind_received_payload(item, item["sha256"], 4)
            entry = st.get_cache_entry(item["sha256"])
            self.assertIsNone(entry)
        finally:
            disabled_mgr.shutdown()

    def test_payload_binding_does_not_publish_cache_before_commit(self):
        st = self.manager.store("peer-a")
        item = cm.make_text_item("hello", seq=0)
        item = self.manager._bind_received_payload(item, item["sha256"], 5)
        entry = st.get_cache_entry(item["sha256"])
        self.assertIsNone(entry)

    def test_evict_cache_runs_after_receiving_item(self):
        st = self.manager.store("peer-a")
        # Cache entries that correspond to real items
        item_a = cm.make_text_item("alpha", seq=1)
        st.add_item(item_a, data=b"alpha")
        st.record_cache_entry(item_a["sha256"], payload_size=10)
        item_b = cm.make_text_item("beta", seq=2)
        st.add_item(item_b, data=b"beta")
        st.record_cache_entry(item_b["sha256"], payload_size=10)
        pinned = cm.make_text_item("pinned-one", seq=3)
        st.add_item(pinned, data=b"pinned-one")
        st.set_pinned(pinned["item_id"], True)
        st.record_cache_entry(pinned["sha256"], payload_size=10)
        evicted = self.manager._evict_cache_if_needed("peer-a")
        # item_b is non-pinned, non-current → evictable
        self.assertIn(item_b["sha256"], evicted)
        # pinned is protected → not evicted
        self.assertNotIn(pinned["sha256"], evicted)
        # item_a is current_item (since last add_item with make_current=True
        # for pinned sets current=pinned, but item_a is also there) →
        # current item is protected, but item_a was never set as current,
        # so it IS evictable.
        # After eviction, only pinned and item_a remain in cache.
        self.assertIsNone(st.get_cache_entry(item_b["sha256"]))


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


class TransferPreflightModelTests(unittest.TestCase):
    def test_allows_small_raw_transfer(self):
        result = cm.compute_transfer_preflight(
            payload_size=1000, free_bytes=10_000_000_000)
        self.assertTrue(result["allowed"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["required_download_bytes"], 1000)
        self.assertEqual(result["required_temporary_bytes"], 0)

    def test_allows_small_bundle_with_room(self):
        result = cm.compute_transfer_preflight(
            payload_size=5000, free_bytes=10_000_000_000,
            encoding="deterministic_zip", known_transfer_size=3000,
            logical_size=5000, file_count=3)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["required_download_bytes"], 5000)
        self.assertEqual(result["required_temporary_bytes"], 3000)
        self.assertGreater(result["peak_required_bytes"], 5000)

    def test_rejects_disk_full(self):
        result = cm.compute_transfer_preflight(
            payload_size=10_000_000_000, free_bytes=1000)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "disk_full")

    def test_rejects_too_large(self):
        result = cm.compute_transfer_preflight(
            payload_size=1_000_000_000, free_bytes=10_000_000_000,
            hard_item_bytes=500_000_000)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "too_large")

    def test_rejects_policy_auto_limit(self):
        result = cm.compute_transfer_preflight(
            payload_size=200_000_000, free_bytes=10_000_000_000,
            auto_limit_bytes=100_000_000, allow_manual=False)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "policy")

    def test_allows_policy_when_manual_allowed(self):
        result = cm.compute_transfer_preflight(
            payload_size=200_000_000, free_bytes=10_000_000_000,
            auto_limit_bytes=100_000_000, allow_manual=True)
        self.assertTrue(result["allowed"])

    def test_already_cached_reduces_download(self):
        result = cm.compute_transfer_preflight(
            payload_size=10_000, free_bytes=10_000_000_000,
            already_cached_bytes=7000)
        self.assertEqual(result["required_download_bytes"], 3000)

    def test_already_cached_over_payload_is_zero(self):
        result = cm.compute_transfer_preflight(
            payload_size=5000, free_bytes=10_000_000_000,
            already_cached_bytes=10_000)
        self.assertEqual(result["required_download_bytes"], 0)

    def test_bundle_already_cached_has_no_temporary(self):
        result = cm.compute_transfer_preflight(
            payload_size=10_000, free_bytes=10_000_000_000,
            encoding="deterministic_zip", known_transfer_size=8000,
            already_cached_bytes=8000)
        self.assertEqual(result["required_download_bytes"], 2000)
        self.assertEqual(result["required_temporary_bytes"], 0)

    def test_materialized_size_adds_to_peak(self):
        result = cm.compute_transfer_preflight(
            payload_size=10_000, free_bytes=10_000_000_000,
            materialized_size=5000)
        self.assertEqual(result["peak_required_bytes"], 15_000)

    def test_safety_margin_is_reasonable(self):
        margin = cm.preflight_safety_margin(1_000_000_000)
        self.assertGreaterEqual(margin, 512 * 1024 * 1024)
        margin2 = cm.preflight_safety_margin(1_000)
        self.assertEqual(margin2, 512 * 1024 * 1024)

    def test_invalid_size_returns_rejection(self):
        result = cm.compute_transfer_preflight(
            payload_size=-1, free_bytes=10_000_000_000)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "invalid_size_metadata")

    def test_unknown_transfer_size_uses_logical_size(self):
        result = cm.compute_transfer_preflight(
            payload_size=20_000, free_bytes=10_000_000_000,
            encoding="deterministic_zip", logical_size=15_000,
            known_transfer_size=None)
        self.assertEqual(result["required_temporary_bytes"], 15_000)

    def test_unknown_sizes_fallback_to_payload(self):
        result = cm.compute_transfer_preflight(
            payload_size=20_000, free_bytes=10_000_000_000,
            encoding="deterministic_zip")
        self.assertEqual(result["required_temporary_bytes"], 20_000)

    def test_zero_payload_size_is_allowed(self):
        result = cm.compute_transfer_preflight(
            payload_size=0, free_bytes=10_000_000_000)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["required_download_bytes"], 0)

    def test_exact_enough_space_is_allowed(self):
        payload = 10_000
        margin = cm.preflight_safety_margin(payload)
        free = payload + margin
        result = cm.compute_transfer_preflight(
            payload_size=payload, free_bytes=free)
        self.assertTrue(result["allowed"])

    def test_safety_margin_constants(self):
        self.assertIn("disk_full", cm.PREFLIGHT_REJECTIONS)
        self.assertIn("too_large", cm.PREFLIGHT_REJECTIONS)
        self.assertIn("policy", cm.PREFLIGHT_REJECTIONS)
        self.assertIn("shutting_down", cm.PREFLIGHT_REJECTIONS)


class TransferPreflightIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sent = []
        self.manager = ClipboardManager(
            self.tmp, "dev-self",
            send_fn=lambda identity, msg: self.sent.append(msg),
            settings_fn=lambda: cm.clipboard_settings(
                {"clipboard": {"enabled": True}}))
        self.manager._accepting_work = True

    def tearDown(self):
        self.manager.shutdown()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_receive_preflight_allows_small_item(self):
        st = self.manager.store("peer-a")
        item = cm.make_text_item("hello", seq=1)
        st.add_item(item, data=b"hello")
        pre = self.manager._receive_preflight("peer-a", item)
        self.assertTrue(pre["allowed"])

    def test_receive_preflight_rejects_disk_full(self):
        st = self.manager.store("peer-a")
        item = cm.make_text_item("x" * 10_000_000, seq=1)
        pre = self.manager._receive_preflight("peer-a", item)
        if pre["reason"] == "disk_full":
            self.assertFalse(pre["allowed"])
        else:
            self.assertTrue(pre["allowed"],
                            f"unexpected rejection: {pre.get('reason')}")

    def test_receive_preflight_accounts_cached_bytes(self):
        st = self.manager.store("peer-a")
        sha = "a" * 64
        st.record_cache_entry(sha, payload_size=5000, payload_sha256=sha)
        item = cm.make_text_item("hello", seq=1)
        item["sha256"] = sha
        item["size"] = 5000
        pre = self.manager._receive_preflight("peer-a", item)
        self.assertTrue(pre["allowed"])

    def test_on_start_rejects_when_shut_down(self):
        self.manager._shutting_down = True
        self.manager.handle("peer-a", {
            "type": "clipboard_transfer_start",
            "transfer_id": "t-1", "item_id": "i-1",
            "sha256": "a" * 64, "total_size": cp.MIN_LEGACY_CHUNK_SIZE,
            "chunk_size": cp.MIN_LEGACY_CHUNK_SIZE,
            "chunk_count": 1,
        })
        self.assertTrue(any(
            msg.get("type") == "clipboard_transfer_error"
            for msg in self.sent))

    def test_preflight_protocol_message_roundtrip(self):
        msg = cp.build_preflight("prof", "item-1", "a" * 64, 5000,
                                  encoding="deterministic_zip", logical_size=10000,
                                  file_count=3, known_transfer_size=4000)
        parsed = cp.parse_preflight(msg)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["item_id"], "item-1")
        self.assertEqual(parsed["payload_size"], 5000)

    def test_preflight_response_roundtrip(self):
        detail = {"peak_required_bytes": 5000, "free_bytes": 1_000_000_000}
        resp = cp.build_preflight_response("prof", "item-1", False,
                                           reason="disk_full", detail=detail)
        parsed = cp.parse_preflight_response(resp)
        self.assertIsNotNone(parsed)
        self.assertFalse(parsed["allowed"])
        self.assertEqual(parsed["reason"], "disk_full")

    def test_preflight_request_with_correlation(self):
        msg = cp.build_preflight("prof", "item-1", "a" * 64, 5000, request_id="req-1")
        parsed = cp.parse_preflight(msg)
        self.assertEqual(parsed["request_id"], "req-1")

    def test_preflight_response_with_correlation(self):
        resp = cp.build_preflight_response("prof", "item-1", True, request_id="req-1")
        parsed = cp.parse_preflight_response(resp)
        self.assertEqual(parsed["request_id"], "req-1")

    def test_preflight_handshake_approves_valid_transfer(self):
        sent = []
        sender = ClipboardManager(
            tempfile.mkdtemp(), "dev-src",
            send_fn=lambda identity, msg: sent.append((identity, msg)),
            settings_fn=lambda: cm.clipboard_settings(
                {"clipboard": {"enabled": True}}))
        receiver = ClipboardManager(
            tempfile.mkdtemp(), "dev-dst",
            send_fn=lambda identity, msg: sent.append((identity, msg)),
            settings_fn=lambda: cm.clipboard_settings(
                {"clipboard": {"enabled": True}}))
        try:
            item = cm.make_text_item("hello", seq=1)
            sender.store("peer-b").add_item(item, data=b"hello")
            job = ctt.make_transfer_job("t1", "peer-b", item["item_id"], "send",
                                        item["kind"], item["display_name"], 0,
                                        final_ack_requested=True)
            # Sender sends preflight request via send_fn. Route it to receiver.
            def route(ident, msg):
                sent.append((ident, msg))
                if msg.get("type") == cp.T_PREFLIGHT:
                    receiver.handle(ident, msg)
                elif msg.get("type") == cp.T_PREFLIGHT_RESPONSE:
                    sender.handle(ident, msg)
                elif msg.get("type") in (cp.T_START, cp.T_CHUNK, cp.T_COMPLETE):
                    receiver.handle(ident, msg)
                elif msg.get("type") in (cp.T_ACK, cp.T_ERROR):
                    sender.handle(ident, msg)
            sender.send_fn = route
            receiver.send_fn = route
            # Trigger the send_transfer flow (preflight handshake inside).
            # We need to run _send_transfer in a thread since it waits.
            import threading as _thr
            result = {"ok": False}
            def do_send():
                sender._send_transfer("peer-b", item["item_id"], job)
                result["ok"] = job.status == ctt.TransferStatus.completed
            t = _thr.Thread(target=do_send, daemon=True)
            t.start()
            t.join(timeout=10)
            self.assertTrue(result["ok"], "preflight handshake should allow transfer")
            self.assertTrue(any(msg.get("type") == cp.T_ACK for _ident, msg in sent))
        finally:
            sender.shutdown()
            receiver.shutdown()
            import shutil
            shutil.rmtree(sender.store_root, ignore_errors=True)
            shutil.rmtree(receiver.store_root, ignore_errors=True)

    def test_request_scopes_final_ack_support_to_queued_transfer(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ClipboardManager(root, "local-device", lambda _identity, _msg: None,
                                       settings)
            item = manager.capture_text("peer-a", "payload")
            try:
                with mock.patch.object(manager, "_queue_send_item") as queue:
                    legacy = cp.build_request_items("peer-a", [item["item_id"]])
                    legacy.pop("final_ack")
                    manager._on_request("peer-a", legacy)
                    queue.assert_called_once_with(
                        "peer-a", item["item_id"], final_ack_requested=False)
                    queue.reset_mock()
                    manager._on_request(
                        "peer-a", cp.build_request_items("peer-a", [item["item_id"]]))
                    queue.assert_called_once_with(
                        "peer-a", item["item_id"], final_ack_requested=True)
            finally:
                manager.shutdown()

    def test_preflight_handshake_timeout_blocks_transfer(self):
        sender = ClipboardManager(
            tempfile.mkdtemp(), "dev-src",
            send_fn=lambda identity, msg: None,  # no response -> timeout
            settings_fn=lambda: cm.clipboard_settings(
                {"clipboard": {"enabled": True, "preflight_timeout_sec": 1}}))
        try:
            item = cm.make_text_item("hello", seq=1)
            sender.store("peer-b").add_item(item, data=b"hello")
            job = ctt.make_transfer_job("t2", "peer-b", item["item_id"], "send",
                                        item["kind"], item["display_name"], 0)
            import threading as _thr
            result = {"ok": True}
            def do_send():
                sender._send_transfer("peer-b", item["item_id"], job)
                result["ok"] = job.status == ctt.TransferStatus.failed
            t = _thr.Thread(target=do_send, daemon=True)
            t.start()
            t.join(timeout=5)
            self.assertTrue(result["ok"], "timeout should fail the transfer")
        finally:
            sender.shutdown()
            import shutil
            shutil.rmtree(sender.store_root, ignore_errors=True)

    def test_sender_requires_receiver_final_ack_and_fails_on_receiver_error(self):
        sender_settings = cm.clipboard_settings({"clipboard": {
            "enabled": True, "clipboard_transfer_final_ack_timeout_ms": 100}})
        with tempfile.TemporaryDirectory() as root:
            sender = ClipboardManager(root, "dev-src", lambda _identity, _msg: None,
                                      lambda: sender_settings)
            item = cm.make_text_item("hello", seq=1)
            sender.store("peer-b").add_item(item, data=b"hello")
            legacy_job = ctt.make_transfer_job(
                "legacy-no-ack", "peer-b", item["item_id"], "send",
                item["kind"], item["display_name"], 0)

            def route_legacy(identity, msg):
                if msg.get("type") == cp.T_PREFLIGHT:
                    parsed = cp.parse_preflight(msg)
                    sender.handle(identity, cp.build_preflight_response(
                        parsed["profile_id"], parsed["item_id"], True,
                        request_id=parsed.get("request_id")))

            sender.send_fn = route_legacy
            sender._send_transfer("peer-b", item["item_id"], legacy_job)
            self.assertEqual(legacy_job.status, ctt.TransferStatus.completed)

            legacy_error_job = ctt.make_transfer_job(
                "legacy-receiver-error", "peer-b", item["item_id"], "send",
                item["kind"], item["display_name"], 0)

            def route_legacy_error(identity, msg):
                if msg.get("type") == cp.T_PREFLIGHT:
                    parsed = cp.parse_preflight(msg)
                    sender.handle(identity, cp.build_preflight_response(
                        parsed["profile_id"], parsed["item_id"], True,
                        request_id=parsed.get("request_id")))
                elif msg.get("type") == cp.T_COMPLETE:
                    sender._on_error(identity, cp.build_transfer_error(
                        msg["transfer_id"], msg["item_id"], cp.ERR_ABORTED,
                        "legacy receiver failed"))

            with sender._lock:
                sender._jobs[legacy_error_job.transfer_id] = legacy_error_job
            sender.send_fn = route_legacy_error
            sender._send_transfer("peer-b", item["item_id"], legacy_error_job)
            self.assertEqual(legacy_error_job.status, ctt.TransferStatus.failed)
            self.assertIn("legacy receiver failed", legacy_error_job.error)

            job = ctt.make_transfer_job("no-ack", "peer-b", item["item_id"], "send",
                                        item["kind"], item["display_name"], 0,
                                        final_ack_requested=True)
            sent_mismatched_acks = False

            def route_without_ack(identity, msg):
                nonlocal sent_mismatched_acks
                if msg.get("type") == cp.T_PREFLIGHT:
                    parsed = cp.parse_preflight(msg)
                    sender.handle(identity, cp.build_preflight_response(
                        parsed["profile_id"], parsed["item_id"], True,
                        request_id=parsed.get("request_id")))
                elif msg.get("type") == cp.T_COMPLETE and not sent_mismatched_acks:
                    sent_mismatched_acks = True
                    sender._on_ack("peer-c", cp.build_transfer_ack(
                        msg["transfer_id"], msg["item_id"]))
                    sender._on_ack(identity, cp.build_transfer_ack(
                        "wrong-transfer", msg["item_id"]))
                    sender._on_ack(identity, cp.build_transfer_ack(
                        msg["transfer_id"], "wrong-item"))

            sender.send_fn = route_without_ack
            try:
                sender._send_transfer("peer-b", item["item_id"], job)
                self.assertEqual(job.status, ctt.TransferStatus.failed)
                self.assertIn("ACK timeout", job.error)
                self.assertTrue(sent_mismatched_acks)
                self.assertEqual(sender._pending_final_acks, {})

                error_job = ctt.make_transfer_job(
                    "receiver-error", "peer-b", item["item_id"], "send",
                    item["kind"], item["display_name"], 0,
                    final_ack_requested=True)

                def route_error(identity, msg):
                    if msg.get("type") == cp.T_PREFLIGHT:
                        parsed = cp.parse_preflight(msg)
                        sender.handle(identity, cp.build_preflight_response(
                            parsed["profile_id"], parsed["item_id"], True,
                            request_id=parsed.get("request_id")))
                    elif msg.get("type") == cp.T_COMPLETE:
                        sender._on_error(identity, cp.build_transfer_error(
                            msg["transfer_id"], msg["item_id"], cp.ERR_ABORTED,
                            "receiver store failed"))

                sender.send_fn = route_error
                with sender._lock:
                    sender._jobs[error_job.transfer_id] = error_job
                sender._send_transfer("peer-b", item["item_id"], error_job)
                self.assertEqual(error_job.status, ctt.TransferStatus.failed)
                self.assertIn("receiver store failed", error_job.error)
            finally:
                sender.shutdown()

    def test_sender_replays_completion_after_lost_ack_without_payload_retransmission(self):
        sender_settings = cm.clipboard_settings({"clipboard": {
            "enabled": True, "clipboard_transfer_final_ack_timeout_ms": 500}})
        sender_settings.update({
            "clipboard_transfer_final_ack_retry_interval_ms": 20,
            "clipboard_transfer_final_ack_retry_count": 2,
        })
        with tempfile.TemporaryDirectory() as sender_root, tempfile.TemporaryDirectory() as receiver_root:
            sender = ClipboardManager(
                sender_root, "dev-src", lambda _identity, _message: None,
                lambda: sender_settings)
            receiver = ClipboardManager(receiver_root, "dev-dst", lambda _identity, _message: None,
                                        settings)
            item = cm.make_text_item("hello", seq=1)
            sender.store("peer-b").add_item(item, data=b"hello")
            job = ctt.make_transfer_job(
                "automatic-complete-replay", "peer-b", item["item_id"], "send",
                item["kind"], item["display_name"], 0, final_ack_requested=True)
            sender_messages = []
            ack_count = 0

            def from_sender(identity, message):
                sender_messages.append(message)
                if message.get("type") == cp.T_PREFLIGHT:
                    parsed = cp.parse_preflight(message)
                    sender.handle(identity, cp.build_preflight_response(
                        parsed["profile_id"], parsed["item_id"], True,
                        request_id=parsed.get("request_id")))
                elif message.get("type") in (cp.T_START, cp.T_CHUNK, cp.T_COMPLETE):
                    receiver.handle(identity, message)

            def from_receiver(identity, message):
                nonlocal ack_count
                if message.get("type") == cp.T_ACK:
                    ack_count += 1
                    if ack_count == 1:
                        return
                sender.handle(identity, message)

            sender.send_fn = from_sender
            receiver.send_fn = from_receiver
            try:
                sender._send_transfer("peer-b", item["item_id"], job)
                completes = [message for message in sender_messages
                             if message.get("type") == cp.T_COMPLETE]
                payload_messages = [message for message in sender_messages
                                    if message.get("type") in (cp.T_START, cp.T_CHUNK)]
                self.assertEqual(job.status, ctt.TransferStatus.completed)
                self.assertEqual(len(completes), 2)
                self.assertLessEqual(len(completes), 3)
                self.assertEqual(len(payload_messages), 2)
                self.assertEqual(receiver.stats["received_items"], 1)
                self.assertEqual(sender.stats["sent_items"], 1)
            finally:
                sender.shutdown()
                receiver.shutdown()

    def test_complete_send_failure_retries_completion_then_recovers(self):
        sender_settings = cm.clipboard_settings({"clipboard": {
            "enabled": True, "clipboard_transfer_final_ack_timeout_ms": 500}})
        sender_settings.update({
            "clipboard_transfer_final_ack_retry_interval_ms": 10,
            "clipboard_transfer_final_ack_retry_count": 2,
        })
        with tempfile.TemporaryDirectory() as root:
            sender = ClipboardManager(
                root, "dev-src", lambda _identity, _message: None,
                lambda: sender_settings)
            item = cm.make_text_item("hello", seq=1)
            sender.store("peer-b").add_item(item, data=b"hello")
            job = ctt.make_transfer_job(
                "complete-send-recovery", "peer-b", item["item_id"], "send",
                item["kind"], item["display_name"], 0, final_ack_requested=True)
            message_types = []
            complete_sends = 0

            def route(identity, message):
                nonlocal complete_sends
                message_types.append(message["type"])
                if message["type"] == cp.T_PREFLIGHT:
                    parsed = cp.parse_preflight(message)
                    sender.handle(identity, cp.build_preflight_response(
                        parsed["profile_id"], parsed["item_id"], True,
                        request_id=parsed.get("request_id")))
                elif message["type"] == cp.T_COMPLETE:
                    complete_sends += 1
                    if complete_sends == 1:
                        raise OSError("injected completion send failure")
                    sender.handle(identity, cp.build_transfer_ack(
                        message["transfer_id"], message["item_id"]))

            sender.send_fn = route
            try:
                sender._send_transfer("peer-b", item["item_id"], job)
                self.assertEqual(job.status, ctt.TransferStatus.completed)
                self.assertEqual(complete_sends, 2)
                self.assertEqual(message_types.count(cp.T_START), 1)
                self.assertEqual(message_types.count(cp.T_CHUNK), 1)
                self.assertNotIn(cp.T_ERROR, message_types)
            finally:
                sender.shutdown()

    def test_permanent_complete_send_failure_does_not_requeue_payload(self):
        sender_settings = cm.clipboard_settings({"clipboard": {
            "enabled": True, "clipboard_transfer_final_ack_timeout_ms": 500}})
        sender_settings.update({
            "clipboard_transfer_final_ack_retry_interval_ms": 10,
            "clipboard_transfer_final_ack_retry_count": 2,
        })
        with tempfile.TemporaryDirectory() as root:
            sender = ClipboardManager(
                root, "dev-src", lambda _identity, _message: None,
                lambda: sender_settings)
            item = sender.capture_text("peer-b", "hello")
            message_types = []

            def route(identity, message):
                message_types.append(message["type"])
                if message["type"] == cp.T_PREFLIGHT:
                    parsed = cp.parse_preflight(message)
                    sender.handle(identity, cp.build_preflight_response(
                        parsed["profile_id"], parsed["item_id"], True,
                        request_id=parsed.get("request_id")))
                elif message["type"] == cp.T_COMPLETE:
                    raise OSError("injected permanent completion send failure")

            sender.send_fn = route
            try:
                job = sender._queue_send_item(
                    "peer-b", item["item_id"], final_ack_requested=True)
                deadline = time.monotonic() + 2.0
                while job.status not in (ctt.TransferStatus.failed,
                                         ctt.TransferStatus.cancelled) \
                        and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(job.status, ctt.TransferStatus.failed)
                self.assertIn("completion send failed", job.error)
                self.assertEqual(message_types.count(cp.T_COMPLETE), 3)
                self.assertEqual(message_types.count(cp.T_START), 1)
                self.assertEqual(message_types.count(cp.T_CHUNK), 1)
                self.assertNotIn(cp.T_ERROR, message_types)
                self.assertEqual(job.retry_count, 0)
            finally:
                sender.shutdown()

    def test_shutdown_wakes_final_ack_waiter_and_clears_registry(self):
        sender_settings = cm.clipboard_settings({"clipboard": {
            "enabled": True, "clipboard_transfer_final_ack_timeout_ms": 300000}})
        sender_settings.update({
            "clipboard_transfer_final_ack_retry_interval_ms": 60000,
            "clipboard_transfer_final_ack_retry_count": 5,
        })
        with tempfile.TemporaryDirectory() as root:
            waiting = threading.Event()
            sender = ClipboardManager(
                root, "dev-src", lambda _identity, _message: None,
                lambda: sender_settings)
            item = cm.make_text_item("hello", seq=1)
            sender.store("peer-b").add_item(item, data=b"hello")

            def route(identity, message):
                if message.get("type") == cp.T_PREFLIGHT:
                    parsed = cp.parse_preflight(message)
                    sender.handle(identity, cp.build_preflight_response(
                        parsed["profile_id"], parsed["item_id"], True,
                        request_id=parsed.get("request_id")))
                elif message.get("type") == cp.T_COMPLETE:
                    waiting.set()

            sender.send_fn = route
            job = sender._queue_send_item(
                "peer-b", item["item_id"], final_ack_requested=True)
            self.assertIsNotNone(job)
            self.assertTrue(waiting.wait(2.0))
            self.assertIn(job.transfer_id, sender._pending_final_acks)

            started = time.monotonic()
            self.assertTrue(sender.shutdown(timeout=1.0))
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 1.0)
            self.assertEqual(job.status, ctt.TransferStatus.cancelled)
            self.assertEqual(sender._pending_final_acks, {})
            queue_state = sender._transfer_queue.activity_snapshot()
            self.assertEqual(queue_state["workers_alive"], 0)
            self.assertFalse(queue_state["blocking"])

    def test_shutdown_closes_completion_admission_during_waiter_registration(self):
        sender_settings = cm.clipboard_settings({"clipboard": {
            "enabled": True, "clipboard_transfer_final_ack_timeout_ms": 300000}})
        with tempfile.TemporaryDirectory() as root:
            sender = ClipboardManager(
                root, "dev-src", lambda _identity, _message: None,
                lambda: sender_settings)
            item = sender.capture_text("peer-b", "hello")
            finalizing = threading.Event()
            release = threading.Event()
            shutdown_has_completion_lock = threading.Event()
            message_types = []
            real_advance = ctt.advance_job_session

            class TrackedCompletionLock:
                def __init__(self):
                    self._lock = threading.RLock()

                def __enter__(self):
                    self._lock.acquire()
                    if threading.current_thread().name == "shutdown-racer":
                        shutdown_has_completion_lock.set()
                    return self

                def __exit__(self, exc_type, exc_value, traceback):
                    self._lock.release()

            sender._completion_send_lock = TrackedCompletionLock()

            def route(identity, message):
                message_types.append(message["type"])
                if message["type"] == cp.T_PREFLIGHT:
                    parsed = cp.parse_preflight(message)
                    sender.handle(identity, cp.build_preflight_response(
                        parsed["profile_id"], parsed["item_id"], True,
                        request_id=parsed.get("request_id")))

            def block_finalizing(job, target):
                if target == ctt.TransferSessionState.finalizing:
                    finalizing.set()
                    release.wait(2.0)
                return real_advance(job, target)

            sender.send_fn = route
            with mock.patch.object(ctt, "advance_job_session", side_effect=block_finalizing):
                job = sender._queue_send_item(
                    "peer-b", item["item_id"], final_ack_requested=True)
                self.assertTrue(finalizing.wait(1.0))
                result = []
                shutdown = threading.Thread(
                    name="shutdown-racer", target=lambda: result.append(sender.shutdown(1.0)))
                shutdown.start()
                self.assertTrue(shutdown_has_completion_lock.wait(1.0))
                release.set()
                shutdown.join(2.0)

            self.assertFalse(shutdown.is_alive())
            self.assertEqual(result, [True])
            self.assertEqual(job.status, ctt.TransferStatus.cancelled)
            self.assertEqual(sender._pending_final_acks, {})
            self.assertNotIn(cp.T_COMPLETE, message_types)

    def test_preflight_rejected_sends_zero_payload_bytes(self):
        payload_bytes = []
        sender = ClipboardManager(
            tempfile.mkdtemp(), "dev-src",
            send_fn=lambda identity, msg: payload_bytes.append(msg.get("data", "")),
            settings_fn=lambda: cm.clipboard_settings(
                {"clipboard": {"enabled": True}}))
        receiver = ClipboardManager(
            tempfile.mkdtemp(), "dev-dst",
            send_fn=lambda identity, msg: None,
            settings_fn=lambda: cm.clipboard_settings(
                {"clipboard": {"enabled": True}}))
        try:
            item = cm.make_text_item("hello", seq=1)
            sender.store("peer-b").add_item(item, data=b"hello")
            job = ctt.make_transfer_job("t3", "peer-b", item["item_id"], "send",
                                        item["kind"], item["display_name"], 0)
            def route(ident, msg):
                if msg.get("type") == cp.T_PREFLIGHT:
                    # Respond with rejection
                    receiver.handle(ident, cp.build_preflight_response(
                        ident, msg.get("item_id"), False, reason="too_large",
                        request_id=msg.get("request_id")))
                elif msg.get("type") == cp.T_PREFLIGHT_RESPONSE:
                    sender.handle(ident, msg)
            sender._send_fn = lambda ident, msg: route(ident, msg)
            import threading as _thr
            result = {"sent_any": False}
            def do_send():
                sender._send_transfer("peer-b", item["item_id"], job)
                result["sent_any"] = len(payload_bytes) > 0
            t = _thr.Thread(target=do_send, daemon=True)
            t.start()
            t.join(timeout=5)
            self.assertFalse(result["sent_any"],
                             "rejected preflight must send 0 payload bytes")
        finally:
            sender.shutdown()
            receiver.shutdown()
            import shutil
            shutil.rmtree(sender.store_root, ignore_errors=True)
            shutil.rmtree(receiver.store_root, ignore_errors=True)


class ClipboardDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.manager = ClipboardManager(
            self.tmp, "dev-self",
            send_fn=lambda identity, msg: None,
            settings_fn=lambda: cm.clipboard_settings(
                {"clipboard": {"enabled": True}}))

    def tearDown(self):
        self.manager.shutdown()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_diagnostics_returns_cache_and_lease_snapshot(self):
        st = self.manager.store("peer-a")
        st.record_cache_entry("a" * 64, payload_size=100)
        st.set_lease("item-1", "/tmp/dest")
        diag = self.manager.diagnostics("peer-a")
        self.assertIn("profile_id", diag)
        self.assertIn("store", diag)
        self.assertIn("cache", diag)
        self.assertIn("leases", diag)
        self.assertIn("activity", diag)
        self.assertEqual(diag["profile_id"], "peer-a")
        self.assertEqual(diag["cache"]["entry_count"], 1)
        self.assertEqual(diag["leases"]["active"], 1)

    def test_diagnostics_store_counts(self):
        st = self.manager.store("peer-a")
        st.add_item(cm.make_text_item("first", seq=1), data=b"first", make_current=True)
        st.add_item(cm.make_text_item("second", seq=2), data=b"second")
        st.set_pinned(st.list_items()[0]["item_id"], True)
        diag = self.manager.diagnostics("peer-a")
        self.assertEqual(diag["store"]["item_count"], 2)
        self.assertGreater(diag["store"]["total_bytes"], 0)
        self.assertEqual(diag["store"]["available_count"], 2)
        self.assertEqual(diag["store"]["pinned_count"], 1)

    def test_item_preview_text_is_model_truncated(self):
        st = self.manager.store("peer-a")
        long_text = "A" * 1000
        item = cm.make_text_item(long_text, seq=1)
        st.add_item(item, data=long_text.encode("utf-8"))
        items = self.manager.list_items("peer-a")
        self.assertIsNotNone(items)
        first = items[0]
        self.assertEqual(first.get("preview_text", ""), long_text)

    def test_diagnostics_activity_snapshot_included(self):
        st = self.manager.store("peer-a")
        st.add_item(cm.make_text_item("hello", seq=1), data=b"hello")
        diag = self.manager.diagnostics("peer-a")
        self.assertIn("blocking", diag["activity"])
        self.assertIn("shutting_down", diag["activity"])


if __name__ == "__main__":
    unittest.main()
