"""Focused filesystem regressions for object fingerprints, locks and cleanup."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

import clipboard_files as files
import clipboard_manifest_v2 as manifests
import clipboard_model as model
import clipboard_object_store_v2 as objects
import clipboard_store as stores


class ObjectStoreSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = stores.ClipboardStore(str(self.root), "profile")
        self.objects = self.store.object_store_v2
        self.payload = b"verified receiver payload"

    def prepare(self):
        source = self.root / "source.bin"
        source.write_bytes(self.payload)
        provisional = files.make_file_item([source])
        provisional.pop("files")
        provisional["source_available"] = False
        provisional = model.version_item(provisional, payload_state="receiving")
        provisional, _ = self.store.add_item(provisional)
        transfer_id = uuid.uuid4().hex
        directory = self.root / "incoming" / transfer_id
        directory.mkdir(parents=True)
        path = directory / "0.verified"
        # Local receiver evidence, acquired from the same open handle used to
        # write and hash. No streaming mocks or changes to the parent's tests.
        with open(path, "w+b", buffering=0) as handle:
            handle.write(self.payload)
            os.fsync(handle.fileno())
            digest = hashlib.sha256(self.payload).hexdigest()
            fingerprint = objects.staged_fingerprint(os.fstat(handle.fileno()), handle)
        final = manifests.finalize_manifest(provisional["batch_manifest"], {0: digest})
        result = SimpleNamespace(
            transfer_id=transfer_id,
            provisional_manifest_digest=provisional["batch_manifest"]["manifest_digest"],
            finalized_manifest=final,
            files=(SimpleNamespace(entry_index=0, size=len(self.payload), sha256=digest,
                                   path=str(path), fingerprint=fingerprint),))
        return provisional, result

    def committed(self):
        provisional, result = self.prepare()
        publication = self.objects.publish_staged_transfer(result)
        item, evicted = self.store.commit_received_v2_item(provisional, publication)
        self.assertEqual(evicted, [])
        self.assertTrue(self.store.verify_received_v2_publication(publication))
        return item, publication, result

    def overwrite_restore_mtime(self, path):
        before = objects.staged_fingerprint(objects._regular(path))
        original = os.stat(path)
        with open(path, "r+b", buffering=0) as handle:
            handle.write(b"x" * original.st_size)
            os.fsync(handle.fileno())
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        after = objects.staged_fingerprint(objects._regular(path))
        self.assertEqual(before[:4], after[:4])
        self.assertNotEqual(before[4], after[4])

    def test_stale_stage_rejected_after_same_size_overwrite_and_restored_mtime(self):
        provisional, result = self.prepare()
        self.overwrite_restore_mtime(result.files[0].path)
        with self.assertRaises(objects.ObjectStoreV2Error) as error:
            self.objects.publish_staged_transfer(result)
        self.assertEqual(error.exception.code, "stage_changed")
        self.assertEqual(self.store.get_item(provisional["item_id"]), provisional)
        self.assertEqual(list(Path(self.objects.objects_root).glob("*/*")), [])

    def test_write_during_pin_never_publishes_old_digest_with_new_content(self):
        for dedup in (False, True):
            with self.subTest(dedup=dedup):
                provisional, result = self.prepare()
                target = Path(self.objects.object_path(result.files[0].sha256))
                if dedup:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        target.unlink()
                    target.write_bytes(self.payload)  # Distinct dedup inode.
                original = self.objects._pin
                attempted = []

                def injected_pin(source, destination):
                    if Path(source) == target:
                        attempted.append(True)
                        self.overwrite_restore_mtime(source)
                    return original(source, destination)

                with mock.patch.object(self.objects, "_pin", side_effect=injected_pin):
                    with self.assertRaises((OSError, objects.ObjectStoreV2Error)):
                        self.objects.publish_staged_transfer(result)
                self.assertEqual(attempted, [True])
                document = json.loads(Path(self.store.index_path).read_bytes())
                self.assertNotIn(result.transfer_id, document.get("v2_receipts", {}))
                self.assertEqual(self.store.get_item(provisional["item_id"]), provisional)
                if os.name == "nt":
                    self.assertEqual(target.read_bytes(), self.payload)
                else:
                    self.assertFalse(self.objects._matches(str(target), len(self.payload),
                                                           result.files[0].sha256))

    @unittest.skipUnless(os.name == "nt", "Windows mandatory share exclusion")
    def test_handoff_denies_write_through_alias_and_allows_rename_and_fsync(self):
        _, result = self.prepare()
        path = Path(result.files[0].path)
        alias = path.with_name("alias")
        renamed = path.with_name("renamed.verified")
        os.link(path, alias)
        with objects.open_verified_handoff(path) as handle:
            before = objects.staged_fingerprint(os.fstat(handle.fileno()), handle)
            with self.assertRaises(OSError):
                with open(alias, "r+b", buffering=0):
                    self.fail("write-sharing exclusion was not enforced")
            objects.resume_v2.durable_replace(path, renamed)
            os.fsync(handle.fileno())
            after = objects.staged_fingerprint(os.fstat(handle.fileno()), handle)
            self.assertEqual(before[:4], after[:4])
            self.assertEqual(after, objects.staged_fingerprint(objects._regular(renamed)))
        self.assertEqual(renamed.read_bytes(), self.payload)

    @unittest.skipUnless(os.name == "nt", "Windows mandatory share exclusion")
    def test_existing_writer_causes_immediate_retryable_handoff_failure(self):
        _, result = self.prepare()
        with open(result.files[0].path, "r+b", buffering=0):
            with self.assertRaises(objects.ObjectStoreV2Error) as error:
                with objects.open_verified_handoff(result.files[0].path):
                    self.fail("existing writer was allowed")
        self.assertEqual(error.exception.code, "stage_busy")
        self.assertTrue(error.exception.retryable)

    @unittest.skipUnless(os.name == "nt", "Windows avoids POSIX rehash fallback")
    def test_protected_publication_and_commit_do_not_rehash_payload(self):
        provisional, result = self.prepare()
        real_hash = hashlib.sha256

        class MetadataHash:
            def __init__(inner, data=b""):
                inner.hasher = real_hash(data)
            def update(inner, data):
                if data and data in self.payload:
                    raise AssertionError("payload reread during normal Windows handoff")
                inner.hasher.update(data)
            def digest(inner):
                return inner.hasher.digest()
            def hexdigest(inner):
                return inner.hasher.hexdigest()

        with mock.patch.object(objects.hashlib, "sha256", MetadataHash):
            publication = self.objects.publish_staged_transfer(result)
            item, _ = self.store.commit_received_v2_item(provisional, publication)
            self.assertTrue(self.store.verify_received_v2_publication(publication))
        self.assertEqual(item["payload_state"], "cached")

    def test_cached_object_rejected_after_same_size_overwrite_and_restored_mtime(self):
        item, publication, _ = self.committed()
        self.assertTrue(self.objects.item_is_publishable(item))
        self.overwrite_restore_mtime(self.objects.object_path(publication.object_hashes[0]))
        self.assertFalse(self.objects.item_is_publishable(item))
        self.assertFalse(self.store.verify_received_v2_publication(publication))
        reopened = stores.ClipboardStore(str(self.root), "profile")
        self.assertEqual(reopened.get_item(item["item_id"])["payload_state"], "missing")

    def test_path_and_handle_use_identical_change_time(self):
        _, result = self.prepare()
        with open(result.files[0].path, "rb", buffering=0) as handle:
            info = os.fstat(handle.fileno())
            expected = objects.staged_fingerprint(info, handle)
            self.assertEqual(expected, objects.staged_fingerprint(info, handle.fileno()))
            self.assertEqual(expected, objects.staged_fingerprint(objects._regular(result.files[0].path)))

    @unittest.skipUnless(os.name == "nt", "Windows handle API")
    def test_windows_raw_stat_without_handle_fails_closed(self):
        _, result = self.prepare()
        with self.assertRaises(objects.ObjectStoreV2Error) as error:
            objects.staged_fingerprint(os.stat(result.files[0].path))
        self.assertEqual(error.exception.code, "fingerprint_unavailable")

    @unittest.skipUnless(os.name == "nt", "Windows handle API")
    def test_windows_query_failure_has_no_creation_time_fallback(self):
        _, result = self.prepare()
        with open(result.files[0].path, "rb", buffering=0) as handle:
            query = mock.Mock(return_value=0)
            with mock.patch("ctypes.WinDLL", return_value=SimpleNamespace(
                    GetFileInformationByHandleEx=query)):
                with self.assertRaises(objects.ObjectStoreV2Error) as error:
                    objects.staged_fingerprint(os.fstat(handle.fileno()), handle)
            self.assertEqual(error.exception.code, "fingerprint_unavailable")

    def hold_process_lock(self):
        program = (
            "import sys; from clipboard_object_store_v2 import ClipboardObjectStoreV2; "
            "s=ClipboardObjectStoreV2(sys.argv[1]); "
            "\nwith s.locked():\n print('locked', flush=True)\n sys.stdin.readline()\n")
        process = subprocess.Popen([sys.executable, "-c", program, str(self.root)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_process, process)
        self.assertEqual(process.stdout.readline().strip(), "locked")
        return process

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.communicate("release\n", timeout=10)

    def test_busy_validation_and_load_do_not_persist_missing(self):
        item, publication, _ = self.committed()
        raw = Path(self.store.index_path).read_bytes()
        process = self.hold_process_lock()
        self.objects.lock_timeout = 0.05
        for operation in (lambda: self.objects.validate_publication(publication),
                          lambda: self.objects.item_is_publishable(item),
                          lambda: self.store.verify_received_v2_publication(publication),
                          self.store._load):
            with self.subTest(operation=operation), self.assertRaises(objects.ObjectStoreV2Error) as error:
                operation()
            self.assertTrue(error.exception.retryable)
            self.assertEqual(error.exception.code, "store_busy")
            self.assertEqual(Path(self.store.index_path).read_bytes(), raw)
            self.assertEqual(self.store.get_item(item["item_id"])["payload_state"], "cached")
        self.stop_process(process)
        self.store._load()
        self.assertEqual(self.store.get_item(item["item_id"])["payload_state"], "cached")
        self.assertTrue(self.store.verify_received_v2_publication(publication))
        self.assertEqual(Path(self.store.index_path).read_bytes(), raw)

    def test_corrupt_index_recovery_resets_cas_after_locked_rename(self):
        self.committed()
        index = Path(self.store.index_path)
        bad = b"{broken index"
        index.write_bytes(bad)
        reopened = stores.ClipboardStore(str(self.root), "profile")
        self.assertFalse(reopened.read_only)
        self.assertEqual(reopened.list_items(), [])
        self.assertIn("corrupt_index", reopened.load_error)
        backups = list(index.parent.glob("index.backup-corrupt-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), bad)
        document = json.loads(index.read_bytes())
        self.assertEqual(document["items"], [])
        self.assertEqual(reopened._v2_index_digest, hashlib.sha256(index.read_bytes()).digest())
        text, _ = reopened.add_item(model.make_text_item("after recovery", 0), b"after recovery")
        self.assertEqual(reopened.get_data(text["item_id"]), b"after recovery")

    def test_busy_recovery_leaves_corrupt_index_and_memory_untouched(self):
        self.committed()
        items = self.store.list_items()
        index = Path(self.store.index_path)
        bad = b"{broken"
        index.write_bytes(bad)
        self.store._v2_index_digest = hashlib.sha256(bad).digest()
        process = self.hold_process_lock()
        self.objects.lock_timeout = 0.05
        with self.assertRaises(objects.ObjectStoreV2Error) as error:
            self.store._recover_corrupt(ValueError("corrupt"))
        self.assertTrue(error.exception.retryable)
        self.assertEqual(index.read_bytes(), bad)
        self.assertEqual(self.store.list_items(), items)
        self.assertEqual(list(index.parent.glob("index.backup-corrupt-*.json")), [])
        self.stop_process(process)
        self.store._load()
        self.assertEqual(json.loads(index.read_bytes())["items"], [])

    def test_recovery_does_not_rename_concurrently_replaced_index(self):
        self.committed()
        items = self.store.list_items()
        index = Path(self.store.index_path)
        new_document = json.loads(index.read_bytes())
        new_document["revision"] += 1
        replacement = json.dumps(new_document).encode()
        index.write_bytes(replacement)
        with self.assertRaises(objects.ObjectStoreV2Error) as error:
            self.store._recover_corrupt(ValueError("stale corrupt read"))
        self.assertEqual(error.exception.code, "index_changed")
        self.assertTrue(error.exception.retryable)
        self.assertEqual(index.read_bytes(), replacement)
        self.assertEqual(self.store.list_items(), items)
        self.assertEqual(list(index.parent.glob("index.backup-corrupt-*.json")), [])

    def test_discard_is_transfer_bound_and_never_deletes_objects_or_stage(self):
        provisional, result = self.prepare()
        publication = self.objects.publish_staged_transfer(result)
        pending = Path(self.objects._pending(publication))
        before = {entry.name: entry.read_bytes() for entry in pending.iterdir()}
        for identifier, digest in ((publication.transfer_id, "f" * 64),
                                   ("../outside", publication.provisional_manifest_digest)):
            with self.assertRaises(objects.ObjectStoreV2Error):
                self.objects.discard_pending(identifier, digest)
            self.assertEqual({entry.name: entry.read_bytes() for entry in pending.iterdir()}, before)
        self.assertTrue(self.objects.discard_pending(publication.transfer_id,
                                                    publication.provisional_manifest_digest))
        self.assertFalse(pending.exists())
        self.assertTrue(Path(result.files[0].path).exists())
        self.assertTrue(Path(self.objects.object_path(publication.object_hashes[0])).exists())
        self.assertTrue(Path(self.objects.manifest_path(publication.manifest_digest)).exists())
        self.assertEqual(self.store.get_item(provisional["item_id"]), provisional)
        self.assertFalse(self.objects.discard_pending(publication.transfer_id,
                                                     publication.provisional_manifest_digest))

    def test_discard_rejects_unknown_names_before_deleting_any_pin(self):
        _, result = self.prepare()
        publication = self.objects.publish_staged_transfer(result)
        pending = Path(self.objects._pending(publication))
        extra = pending / "not-owned"
        extra.write_bytes(b"user data")
        names = sorted(entry.name for entry in pending.iterdir())
        with self.assertRaises(objects.ObjectStoreV2Error):
            self.objects.discard_pending(publication.transfer_id, publication.provisional_manifest_digest)
        self.assertEqual(sorted(entry.name for entry in pending.iterdir()), names)
        self.assertEqual(extra.read_bytes(), b"user data")

    def test_discard_rejects_corrupt_receipt_and_unsafe_ancestor(self):
        _, result = self.prepare()
        publication = self.objects.publish_staged_transfer(result)
        pending = Path(self.objects._pending(publication))
        receipt = pending / "receipt.json"
        original = receipt.read_bytes()
        receipt.write_bytes(b"{}")
        with self.assertRaises(objects.ObjectStoreV2Error):
            self.objects.discard_pending(publication.transfer_id, publication.provisional_manifest_digest)
        self.assertTrue((pending / publication.object_hashes[0]).exists())
        receipt.write_bytes(original)
        real_reparse = objects.clipboard_paths._is_reparse_point
        with mock.patch.object(objects.clipboard_paths, "_is_reparse_point", side_effect=lambda path, info:
                               Path(path) == pending or real_reparse(path, info)):
            with self.assertRaises(objects.ObjectStoreV2Error):
                self.objects.discard_pending(publication.transfer_id, publication.provisional_manifest_digest)
        self.assertEqual(receipt.read_bytes(), original)

    def test_discard_partial_failure_retains_receipt_and_can_retry(self):
        _, result = self.prepare()
        publication = self.objects.publish_staged_transfer(result)
        pending = Path(self.objects._pending(publication))
        original = os.unlink

        def failed_unlink(path, *args, **kwargs):
            if Path(path).name == "manifest.json":
                raise PermissionError("pin in use")
            return original(path, *args, **kwargs)

        with mock.patch.object(objects.os, "unlink", side_effect=failed_unlink):
            with self.assertRaises(PermissionError):
                self.objects.discard_pending(publication.transfer_id, publication.provisional_manifest_digest)
        self.assertTrue((pending / "receipt.json").exists())
        self.assertTrue(self.objects.discard_pending(publication.transfer_id,
                                                    publication.provisional_manifest_digest))
        self.assertFalse(pending.exists())

    def test_discard_cannot_remove_other_transfer_pins(self):
        _, first = self.prepare()
        pub1 = self.objects.publish_staged_transfer(first)
        _, second = self.prepare()
        pub2 = self.objects.publish_staged_transfer(second)
        other = Path(self.objects._pending(pub2))
        contents = {path.name: path.read_bytes() for path in other.iterdir()}
        self.assertTrue(self.objects.discard_pending(pub1.transfer_id, pub1.provisional_manifest_digest))
        self.assertEqual({path.name: path.read_bytes() for path in other.iterdir()}, contents)
        self.assertTrue(self.objects.validate_publication(pub2, require_pending=True))

    def test_discard_before_manifest_publication_and_gc_preserves_stage(self):
        _, result = self.prepare()
        real_install = self.objects._install_bytes

        def fail_manifest(target, payload, **kwargs):
            if str(target).startswith(self.objects.manifests_root):
                raise OSError("interrupted publication")
            return real_install(target, payload, **kwargs)

        with mock.patch.object(self.objects, "_install_bytes", side_effect=fail_manifest):
            with self.assertRaises(OSError):
                self.objects.publish_staged_transfer(result)
        self.assertTrue(self.objects.discard_pending(result.transfer_id, result.provisional_manifest_digest))
        self.assertEqual(self.objects.collect_garbage(), 0)
        target = Path(self.objects.object_path(result.files[0].sha256))
        self.assertEqual(target.read_bytes(), self.payload)
        os.unlink(result.files[0].path)
        self.assertEqual(self.objects.collect_garbage(), 1)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
