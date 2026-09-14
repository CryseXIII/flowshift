"""End-state tests for V2 manifest materialization (hardlink / verified copy)."""
import copy
import errno
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest import mock

import clipboard_files as files
import clipboard_materialize_v2 as materialize
import clipboard_model as model
import clipboard_preflight_v2 as preflight
import clipboard_resume_v2 as resume
import clipboard_store as stores
import clipboard_streaming_v2 as streaming
from clipboard_runtime import ClipboardManager


def _settings():
    return model.clipboard_settings({"clipboard": {"enabled": True}})


def publish_paths(store, root, paths):
    """Transfer ``paths`` end-to-end into ``store`` and return the committed item."""
    provisional = files.make_file_item(paths)
    local = provisional["files"]
    remote = copy.deepcopy(provisional)
    remote.pop("files")
    remote["source_available"] = False
    remote = model.version_item(remote, payload_state="receiving")
    provisional, _ = store.add_item(remote, make_current=True)
    identifier = uuid.uuid4().hex
    manifest = provisional["batch_manifest"]
    acceptance = preflight.accept_preflight(
        identifier, manifest, preflight.estimate_stream_v2(manifest, free_bytes=10 ** 12),
        now=0, expires_at=10 ** 12)
    source = streaming.SequentialFileStream(identifier, manifest, local,
                                            accepted_preflight=acceptance)
    try:
        stage = streaming.IncomingTransferStage.create(
            str(Path(root) / "incoming"), identifier, manifest,
            journal_store=resume.ResumeJournalStore(Path(root) / "journals"),
            peer_id="peer-a", profile_id=store.profile_id, provider_id="device:peer-a",
            accepted_preflight=acceptance)
        for chunk in source.iter_chunks():
            stage.accept(chunk)
        result = stage.finalize(source.completion())
    finally:
        source.close()
    publication = store.object_store_v2.publish_staged_transfer(result)
    item, evicted = store.commit_received_v2_item(provisional, publication)
    assert evicted == []
    assert store.verify_received_v2_publication(publication)
    return item


def _tree(root):
    """Return {relative_posix_path: bytes|None(dir)} for a materialized tree."""
    found = {}
    for current, dirs, names in os.walk(root):
        rel = os.path.relpath(current, root)
        for name in dirs:
            found[Path(rel, name).as_posix()] = None
        for name in names:
            found[Path(rel, name).as_posix()] = Path(current, name).read_bytes()
    return found


def _zips_under(root):
    return [str(path) for path in Path(root).rglob("*.zip")]


class MaterializeManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flowshift-materialize-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = stores.ClipboardStore(str(self.root / "store"), "a")
        self.objects = self.store.object_store_v2
        source = self.root / "source"
        (source / "batch" / "nested" / "deep").mkdir(parents=True)
        (source / "batch" / "empty-dir").mkdir()
        self.payloads = {
            "batch/top.bin": b"top level payload " * 500,
            "batch/nested/inner.txt": b"inner",
            "batch/nested/deep/leaf.dat": bytes(range(256)) * 40,
            "batch/nested/zero.bin": b"",
        }
        for rel, data in self.payloads.items():
            (source / rel).write_bytes(data)
        self.item = publish_paths(self.store, self.root / "store", [source / "batch"])
        self.manifest = self.store.v2_manifest_for_item(self.item["item_id"])
        self.assertIsNotNone(self.manifest)
        # Baseline link counts (stage/pin links may still reference the objects).
        self.nlink = {rel: os.stat(self.object_path(rel)).st_nlink for rel in self.payloads}
        self.dest = self.root / "leases" / "profile" / self.item["item_id"]
        self.dest.parent.mkdir(parents=True)

    def object_path(self, rel):
        return self.objects.object_path(hashlib.sha256(self.payloads[rel]).hexdigest())

    def assert_tree_matches(self):
        expected = {rel: data for rel, data in self.payloads.items()}
        expected.update({"batch": None, "batch/nested": None, "batch/nested/deep": None,
                         "batch/empty-dir": None})
        self.assertEqual(_tree(self.dest), expected)

    def assert_nothing_left(self):
        self.assertFalse(self.dest.exists())
        self.assertEqual(os.listdir(self.dest.parent), [])

    def test_hardlink_materialization_shares_object_inodes(self):
        result = materialize.materialize_manifest(self.objects, self.manifest, self.dest)
        self.assertEqual(result.strategy, "hardlink")
        self.assertEqual(result.roots, (str(self.dest / "batch"),))
        self.assertEqual((result.linked_files, result.copied_files), (4, 0))
        self.assertEqual(result.bytes, sum(len(data) for data in self.payloads.values()))
        self.assert_tree_matches()
        for rel in self.payloads:
            linked = os.stat(self.dest / rel)
            obj = os.stat(self.object_path(rel))
            self.assertEqual((linked.st_dev, linked.st_ino), (obj.st_dev, obj.st_ino), rel)
            self.assertEqual(obj.st_nlink, self.nlink[rel] + 1, rel)
        self.assertEqual(os.listdir(self.dest.parent), [self.item["item_id"]])
        self.assertEqual(_zips_under(self.root), [])

    def test_forced_copy_fallback_verifies_content(self):
        with mock.patch.object(os, "link", side_effect=OSError(errno.EXDEV, "cross-device")):
            result = materialize.materialize_manifest(self.objects, self.manifest, self.dest)
        self.assertEqual(result.strategy, "copy")
        self.assertEqual((result.linked_files, result.copied_files), (0, 4))
        self.assert_tree_matches()
        for rel, data in self.payloads.items():
            copied = os.stat(self.dest / rel)
            obj = os.stat(self.object_path(rel))
            self.assertNotEqual((copied.st_dev, copied.st_ino), (obj.st_dev, obj.st_ino), rel)
            self.assertEqual(obj.st_nlink, self.nlink[rel], rel)
            self.assertEqual(Path(self.object_path(rel)).read_bytes(), data)
            self.assertEqual(hashlib.sha256((self.dest / rel).read_bytes()).hexdigest(),
                             hashlib.sha256(data).hexdigest())
        self.assertEqual(os.listdir(self.dest.parent), [self.item["item_id"]])

    def test_mixed_strategy_when_one_link_fails(self):
        real_link = os.link
        target_object = os.path.normcase(self.object_path("batch/nested/deep/leaf.dat"))

        def flaky_link(src, dst, *args, **kwargs):
            if os.path.normcase(src) == target_object:
                raise OSError(errno.EXDEV, "cross-device")
            return real_link(src, dst, *args, **kwargs)

        with mock.patch.object(os, "link", side_effect=flaky_link):
            result = materialize.materialize_manifest(self.objects, self.manifest, self.dest)
        self.assertEqual(result.strategy, "mixed")
        self.assertEqual((result.linked_files, result.copied_files), (3, 1))
        self.assert_tree_matches()
        obj = os.stat(self.object_path("batch/nested/deep/leaf.dat"))
        self.assertEqual(obj.st_nlink, self.nlink["batch/nested/deep/leaf.dat"])
        self.assertEqual(os.stat(self.object_path("batch/top.bin")).st_nlink, self.nlink["batch/top.bin"] + 1)

    def test_permission_error_on_link_falls_back_to_copy(self):
        with mock.patch.object(os, "link", side_effect=PermissionError(errno.EPERM, "denied")):
            result = materialize.materialize_manifest(self.objects, self.manifest, self.dest)
        self.assertEqual(result.strategy, "copy")
        self.assert_tree_matches()

    def test_link_identity_mismatch_is_treated_as_failure(self):
        # A "link" that produces a different inode must not be trusted.
        def fake_link(src, dst, *args, **kwargs):
            Path(dst).write_bytes(Path(src).read_bytes())

        with mock.patch.object(os, "link", side_effect=fake_link):
            result = materialize.materialize_manifest(self.objects, self.manifest, self.dest)
        self.assertEqual(result.strategy, "copy")
        self.assert_tree_matches()

    def test_corrupt_object_during_copy_aborts_without_leftovers(self):
        rel = "batch/nested/deep/leaf.dat"
        path = self.object_path(rel)
        original = Path(path).read_bytes()
        tampered = bytes([original[0] ^ 0xFF]) + original[1:]
        Path(path).write_bytes(tampered)
        self.addCleanup(Path(path).write_bytes, original)
        with mock.patch.object(os, "link", side_effect=OSError(errno.EXDEV, "cross-device")):
            with self.assertRaises(materialize.MaterializationError) as caught:
                materialize.materialize_manifest(self.objects, self.manifest, self.dest)
        self.assertEqual(caught.exception.code, "object_corrupt")
        self.assert_nothing_left()

    def test_missing_object_aborts_without_leftovers(self):
        os.unlink(self.object_path("batch/nested/inner.txt"))
        with self.assertRaises(materialize.MaterializationError) as caught:
            materialize.materialize_manifest(self.objects, self.manifest, self.dest)
        self.assertEqual(caught.exception.code, "object_missing")
        self.assert_nothing_left()
        # Untouched objects were not modified by the aborted attempt.
        self.assertEqual(os.stat(self.object_path("batch/top.bin")).st_nlink, self.nlink["batch/top.bin"])

    def test_object_size_mismatch_aborts(self):
        path = self.object_path("batch/top.bin")
        with open(path, "ab") as handle:
            handle.write(b"x")
        with self.assertRaises(materialize.MaterializationError) as caught:
            materialize.materialize_manifest(self.objects, self.manifest, self.dest)
        self.assertEqual(caught.exception.code, "object_size_mismatch")
        self.assert_nothing_left()

    def test_non_empty_destination_is_refused(self):
        self.dest.mkdir()
        (self.dest / "stale.txt").write_bytes(b"old")
        with self.assertRaises(materialize.MaterializationError) as caught:
            materialize.materialize_manifest(self.objects, self.manifest, self.dest)
        self.assertEqual(caught.exception.code, "dest_not_empty")
        self.assertEqual(os.listdir(self.dest), ["stale.txt"])
        self.assertEqual(os.listdir(self.dest.parent), [self.item["item_id"]])

    def test_empty_existing_destination_is_reused(self):
        self.dest.mkdir()
        result = materialize.materialize_manifest(self.objects, self.manifest, self.dest)
        self.assertEqual(result.strategy, "hardlink")
        self.assert_tree_matches()

    def test_size_limit_is_enforced_before_writing(self):
        total = self.manifest["total_size"]
        with self.assertRaises(materialize.MaterializationError) as caught:
            materialize.materialize_manifest(self.objects, self.manifest, self.dest,
                                             hard_item_bytes=total - 1)
        self.assertEqual(caught.exception.code, "size_limit")
        self.assert_nothing_left()
        result = materialize.materialize_manifest(self.objects, self.manifest, self.dest,
                                                  hard_item_bytes=total)
        self.assertEqual(result.bytes, total)

    def test_path_escape_in_manifest_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["entries"][0]["path"] = "../escape"
        with self.assertRaises(materialize.MaterializationError) as caught:
            materialize.materialize_manifest(self.objects, manifest, self.dest)
        self.assertEqual(caught.exception.code, "manifest_invalid")
        self.assert_nothing_left()

    def test_unlinking_materialized_files_keeps_store_objects(self):
        materialize.materialize_manifest(self.objects, self.manifest, self.dest)
        for rel in self.payloads:
            os.unlink(self.dest / rel)
        for rel, data in self.payloads.items():
            self.assertEqual(Path(self.object_path(rel)).read_bytes(), data)
            self.assertEqual(os.stat(self.object_path(rel)).st_nlink, self.nlink[rel])


class RuntimeMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flowshift-materialize-runtime-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ClipboardManager(str(self.root / "runtime"), "local-device",
                                        lambda _identity, _msg: None, _settings)
        self.addCleanup(self.manager.shutdown)
        self.identity = "peer-a"
        self.store = self.manager.store(self.identity)
        source = self.root / "source" / "docs"
        (source / "sub").mkdir(parents=True)
        self.payloads = {"docs/readme.txt": b"hello v2 " * 300, "docs/sub/data.bin": b"\x00\x01" * 700}
        for rel, data in self.payloads.items():
            (self.root / "source" / rel).write_bytes(data)
        self.item = publish_paths(self.store, self.root / "runtime", [source])
        self.item_id = self.item["item_id"]
        self.dest_root = self.root / "dest"
        self.dest_root.mkdir()
        self.dest = self.dest_root / stores.profile_dir_name(self.identity) / self.item_id

    def object_bytes(self):
        return {rel: Path(self.store.object_store_v2.object_path(
            hashlib.sha256(data).hexdigest())).read_bytes() for rel, data in self.payloads.items()}

    def test_materialize_release_and_rematerialize(self):
        self.assertIsNone(self.store.get_object_path_for_item(self.item_id))
        result = self.manager.materialize_files_result(self.identity, self.item_id, str(self.dest_root))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["paths"], [str(self.dest / "docs")])
        self.assertTrue(result["lease"])
        self.assertEqual(result["strategy"], "hardlink")
        self.assertEqual((self.dest / "docs" / "readme.txt").read_bytes(), self.payloads["docs/readme.txt"])
        self.assertEqual((self.dest / "docs" / "sub" / "data.bin").read_bytes(), self.payloads["docs/sub/data.bin"])
        lease = self.store.get_lease(self.item_id)
        self.assertEqual(lease["dest_path"], str(self.dest))
        self.assertEqual(lease["state"], model.LEASE_ACTIVE)
        self.assertEqual(_zips_under(self.root), [])

        released = self.store.release_leases_for_item(self.item_id)
        self.assertEqual(released, [self.item_id])
        self.assertIsNone(self.store.get_lease(self.item_id))
        self.assertFalse((self.dest / "docs").exists())
        self.assertEqual([name for name in os.listdir(self.dest)
                          if not name.endswith(".active")], [])
        self.assertEqual(self.object_bytes(), self.payloads)

        again = self.manager.materialize_files_result(self.identity, self.item_id, str(self.dest_root))
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["paths"], [str(self.dest / "docs")])
        self.assertEqual((self.dest / "docs" / "readme.txt").read_bytes(), self.payloads["docs/readme.txt"])
        self.assertEqual(self.store.get_lease(self.item_id)["dest_path"], str(self.dest))

    def test_release_lease_keeps_objects_and_delete_item_releases_lease(self):
        result = self.manager.materialize_files_result(self.identity, self.item_id, str(self.dest_root))
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.store.release_lease(self.item_id))
        self.assertFalse((self.dest / "docs").exists())
        self.assertEqual(self.object_bytes(), self.payloads)

        result = self.manager.materialize_files_result(self.identity, self.item_id, str(self.dest_root))
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.manager.delete_item(self.identity, self.item_id))
        self.assertIsNone(self.store.get_item(self.item_id))
        self.assertIsNone(self.store.get_lease(self.item_id))
        self.assertFalse((self.dest / "docs").exists())
        # Shared objects survive item deletion; GC owns their lifecycle.
        self.assertEqual(self.object_bytes(), self.payloads)

    def test_missing_object_reports_not_present(self):
        digest = hashlib.sha256(self.payloads["docs/sub/data.bin"]).hexdigest()
        os.unlink(self.store.object_store_v2.object_path(digest))
        result = self.manager.materialize_files_result(self.identity, self.item_id, str(self.dest_root))
        self.assertEqual(result, {"ok": False, "error": "file data not present (download/retry)"})
        self.assertFalse(self.dest.exists())
        self.assertIsNone(self.store.get_lease(self.item_id))

    def test_insufficient_disk_space_blocks_materialization(self):
        with mock.patch("clipboard_transfer.check_disk_space", return_value={
                "ok": False, "required_bytes": 10, "free_bytes": 1}):
            result = self.manager.materialize_files_result(self.identity, self.item_id, str(self.dest_root))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "Nicht genug Speicherplatz")
        self.assertFalse(self.dest.exists())
        self.assertIsNone(self.store.get_lease(self.item_id))

    def test_materialization_error_is_reported_without_lease(self):
        digest = hashlib.sha256(self.payloads["docs/readme.txt"]).hexdigest()
        path = self.store.object_store_v2.object_path(digest)
        original = Path(path).read_bytes()
        Path(path).write_bytes(b"X" + original[1:])
        # Same-size tampering is caught by publication verification first.
        result = self.manager.materialize_files_result(self.identity, self.item_id, str(self.dest_root))
        self.assertEqual(result, {"ok": False, "error": "file data not present (download/retry)"})
        self.assertFalse(self.dest.exists())
        Path(path).write_bytes(original)
        with mock.patch.object(materialize, "materialize_manifest",
                               side_effect=materialize.MaterializationError("object_corrupt", "bad")):
            result = self.manager.materialize_files_result(self.identity, self.item_id, str(self.dest_root))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "object_corrupt")
        self.assertIn("object_corrupt", result["error"])
        self.assertFalse(self.dest.exists())
        self.assertIsNone(self.store.get_lease(self.item_id))


if __name__ == "__main__":
    unittest.main()
