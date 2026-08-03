"""FlowShift clipboard file/batch bundling tests (pure + filesystem, any OS)."""
import os
import sys
import tempfile
import threading
import unittest
import zipfile
import stat
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_files as cf
import clipboard_model as cbm

_failures = []


def check(cond, label):
    if cond:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}")
        _failures.append(label)


tmp = tempfile.mkdtemp(prefix="fs_clipfiles_")
d = os.path.join(tmp, "src")
os.makedirs(os.path.join(d, "sub"), exist_ok=True)
p1 = os.path.join(d, "a.txt")
p2 = os.path.join(d, "b.txt")
p3 = os.path.join(d, "sub", "c.txt")
open(p1, "w").write("alpha")
open(p2, "w").write("bravo")
open(p3, "w").write("charlie")


# ── metadata-only scan ──────────────────────────────────────────────
scan = cf.scan_paths([p1, p2, p3])
check(scan["file_count"] == 3, "scan finds 3 files")
check(scan["total_size"] == len("alpha") + len("bravo") + len("charlie"), "scan total size")
rels = sorted(e["rel"] for e in scan["files"])
check(rels == ["a.txt", "b.txt", "sub/c.txt"], "scan rel paths (with common base)")
check(all(e["sha256"] is None and e["hash_state"] == "unhashed" for e in scan["files"]),
      "scan leaves file content unhashed")

# Directory drop is walked.
scan_dir = cf.scan_paths([d])
check(scan_dir["file_count"] == 3, "directory drop is walked recursively")
check(any(e["type"] == "directory" and e["rel"] == "src/sub" for e in scan_dir["entries"]),
      "directory entries are explicit")


# ── metadata identity is stable + dedup-friendly ────────────────────
csha1 = cf.content_sha(scan["files"])
csha2 = cf.content_sha(cf.scan_paths([p3, p1, p2])["files"])   # different order
check(csha1 == csha2, "metadata identity independent of input order")
open(p1, "w").write("ALPHA-changed")
csha3 = cf.content_sha(cf.scan_paths([p1, p2, p3])["files"])
check(csha3 != csha1, "metadata identity changes when size/mtime changes")
open(p1, "w").write("alpha")  # restore


# ── deterministic zip build + unpack round-trip ─────────────────────
scan = cf.scan_paths([p1, p2, p3])
blob1 = cf.build_bundle_bytes(scan["files"], scan["compressible_ratio"])
blob2 = cf.build_bundle_bytes(scan["files"], scan["compressible_ratio"])
check(blob1 == blob2, "zip bundle is deterministic (same bytes -> dedup)")
check(cbm.sha256_bytes(blob1) == cbm.sha256_bytes(blob2), "zip blob sha stable")

dest = os.path.join(tmp, "out")
extracted = cf.unpack_bundle(blob1, dest)
check(len(extracted) == 3, "unpack extracts 3 files")
got = {}
for f in extracted:
    got[os.path.relpath(f, dest).replace("\\", "/")] = open(f).read()
check(got.get("a.txt") == "alpha" and got.get("sub/c.txt") == "charlie",
      "unpacked files have original content + structure")


# ── make_file_item (single vs batch) ────────────────────────────────
single = cf.make_file_item([p1])
check(single["kind"] == cbm.KIND_FILE and single["file_count"] == 1, "single file -> KIND_FILE")
check(single["display_name"] == "a.txt", "single file display_name = filename")
batch = cf.make_file_item([p1, p2, p3])
check(batch["kind"] == cbm.KIND_FILE_BATCH and batch["file_count"] == 3, "many -> KIND_FILE_BATCH")
check("Dateien" in batch["display_name"], "batch display_name mentions file count")
check(batch["schema_version"] == 2 and batch["item_revision"] == 0,
      "file item uses persisted schema 2 and revision 0")
check(batch["content_sha256"] is None and batch["hash_state"] == "unhashed",
      "file item does not claim a final content hash")
check(batch["sha256"] == batch["legacy_provisional_sha256"],
      "legacy provisional identity is explicit")
check(batch["batch_manifest"]["item_id"] == batch["item_id"],
      "canonical batch manifest is integrated into the item")
check(cf.local_source_paths(batch) and len(cf.local_source_paths(batch)) == 3,
      "local_source_paths returns the source files")

# lazy bundle from a captured item round-trips
blob = cf.bundle_for_item(batch)
check(blob is not None, "bundle_for_item builds a blob")
dest2 = os.path.join(tmp, "out2")
ex2 = cf.unpack_bundle(blob, dest2)
check(len(ex2) == 3, "bundle_for_item blob unpacks to 3 files")

# Same content set -> same content identity (dedup across copies).
batch2 = cf.make_file_item([p3, p2, p1])
check(batch2["sha256"] == batch["sha256"], "same source snapshot -> same metadata identity")


# ── path-traversal guard ────────────────────────────────────────────
import io as _io, zipfile as _zip
buf = _io.BytesIO()
with _zip.ZipFile(buf, "w") as zf:
    zf.writestr("../evil.txt", "nope")
safe_dest = os.path.join(tmp, "safe")
res = cf.unpack_bundle(buf.getvalue(), safe_dest)
check(not os.path.exists(os.path.join(tmp, "evil.txt")), "unpack blocks path traversal")


class MetadataCaptureTests(unittest.TestCase):
    def test_capture_never_opens_source_contents_and_lazy_zip_does(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "payload.bin")
            with open(path, "wb") as handle:
                handle.write(b"payload")
            real_open = open

            def reject_source_open(candidate, *args, **kwargs):
                if os.path.abspath(os.fspath(candidate)) == os.path.abspath(path):
                    raise AssertionError("capture opened source contents")
                return real_open(candidate, *args, **kwargs)

            with mock.patch("builtins.open", side_effect=reject_source_open):
                item = cf.make_file_item([path])
            self.assertIsNotNone(item)
            self.assertIsNone(item["content_sha256"])
            self.assertEqual(cf.unpack_bundle(cf.bundle_for_item(item), os.path.join(root, "out")),
                             [os.path.join(root, "out", "payload.bin")])

    def test_empty_directory_is_in_manifest_and_legacy_zip(self):
        with tempfile.TemporaryDirectory() as root:
            empty = os.path.join(root, "empty")
            os.mkdir(empty)
            item = cf.make_file_item([empty])
            entries = item["batch_manifest"]["entries"]
            self.assertEqual([(entry["path"], entry["type"]) for entry in entries],
                             [("empty", "directory")])
            cf.unpack_bundle(cf.bundle_for_item(item), os.path.join(root, "out"))
            self.assertTrue(os.path.isdir(os.path.join(root, "out", "empty")))

    def test_reparse_source_is_rejected_without_following(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "source")
            os.mkdir(source)
            path_stat = os.lstat(source)
            fake_stat = mock.Mock(wraps=path_stat)
            fake_stat.st_mode = path_stat.st_mode
            fake_stat.st_file_attributes = cf._FILE_ATTRIBUTE_REPARSE_POINT
            with mock.patch.object(cf.os, "lstat", return_value=fake_stat):
                with self.assertRaisesRegex(cf.CaptureLimitError, "reparse"):
                    cf.scan_paths([source])

    def test_lazy_zip_rejects_source_mutation_during_read(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "payload.bin")
            with open(path, "wb") as handle:
                handle.write(b"a" * (cf.CHUNK_READ * 2))
            item = cf.make_file_item([path])
            real_copy = cf.shutil.copyfileobj
            mutated = threading.Event()

            def mutate_after_first_read(src, dst, length=0):
                block = src.read(length)
                dst.write(block)
                with open(path, "ab") as handle:
                    handle.write(b"changed")
                mutated.set()
                real_copy(src, dst, length)

            with mock.patch.object(cf.shutil, "copyfileobj", side_effect=mutate_after_first_read):
                with self.assertRaisesRegex(cf.CaptureLimitError, "changed during"):
                    cf.bundle_for_item(item)
            self.assertTrue(mutated.is_set())

    def test_legacy_hashed_source_detects_same_size_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "legacy.bin")
            with open(path, "wb") as handle:
                handle.write(b"original")
            legacy = {
                "abspath": path, "rel": "legacy.bin", "type": "file", "size": 8,
                "sha256": cbm.sha256_bytes(b"original"),
            }
            self.assertTrue(cf.local_sources_available({"files": [legacy]}))
            with open(path, "wb") as handle:
                handle.write(b"mutated!")
            self.assertFalse(cf.local_sources_available({"files": [legacy]}))
            with self.assertRaisesRegex(cf.CaptureLimitError, "changed before"):
                cf.build_bundle_bytes([legacy])

    def test_legacy_hashed_source_is_rechecked_after_bundle_read(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "legacy.bin")
            with open(path, "wb") as handle:
                handle.write(b"original")
            legacy = {
                "abspath": path, "rel": "legacy.bin", "type": "file", "size": 8,
                "sha256": cbm.sha256_bytes(b"original"),
            }
            real_build = cf.build_bundle_to_zipfile

            def mutate_after_build(target, entries, compressible_ratio=1.0):
                real_build(target, entries, compressible_ratio)
                with open(path, "wb") as handle:
                    handle.write(b"mutated!")

            with mock.patch.object(cf, "build_bundle_to_zipfile", side_effect=mutate_after_build):
                with self.assertRaisesRegex(cf.CaptureLimitError, "changed during"):
                    cf.build_bundle_bytes([legacy])

    def test_scan_stops_before_retaining_unbounded_directory(self):
        class EndlessDirectory:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                index = getattr(self, "index", 0)
                self.index = index + 1
                child = mock.Mock()
                child.name = f"child-{index}"
                child.path = os.path.join(self.root, child.name)
                return child

        with tempfile.TemporaryDirectory() as root:
            endless = EndlessDirectory()
            endless.root = root
            with mock.patch.object(cf.os, "scandir", return_value=endless):
                with self.assertRaisesRegex(cf.CaptureLimitError, "entry count"):
                    cf.scan_paths([root], max_files=2, max_directories=1)
            self.assertLessEqual(endless.index, 3)

    def test_root_materialization_includes_empty_directory(self):
        with tempfile.TemporaryDirectory() as root:
            empty = os.path.join(root, "empty")
            os.mkdir(empty)
            item = cf.make_file_item([empty])
            bundle = os.path.join(root, "bundle.zip")
            cf.build_bundle_to_file(item["files"], bundle)
            roots = cf.unpack_bundle_roots_file(bundle, os.path.join(root, "out"))
            self.assertEqual(roots, [os.path.join(root, "out", "empty")])
            self.assertTrue(os.path.isdir(roots[0]))

    def test_strict_root_materialization_rejects_archive_without_partial_writes(self):
        invalid_sets = (
            (("good.txt", b"good"), ("../escape.txt", b"bad")),
            (("good.txt", b"good"), ("CON.txt", b"bad")),
            (("File.txt", b"one"), ("file.txt", b"two")),
            (("parent", b"file"), ("parent/child.txt", b"child")),
            (("name.", b"bad"),),
        )
        for index, members in enumerate(invalid_sets):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as root:
                bundle = os.path.join(root, "bundle.zip")
                with zipfile.ZipFile(bundle, "w") as archive:
                    for name, data in members:
                        archive.writestr(name, data)
                dest = os.path.join(root, "out")
                with self.assertRaises(ValueError):
                    cf.unpack_bundle_roots_file(bundle, dest)
                self.assertFalse(os.path.exists(dest), "strict validation writes nothing")

    def test_strict_root_materialization_enforces_announced_size_and_file_count(self):
        with tempfile.TemporaryDirectory() as root:
            bundle = os.path.join(root, "bundle.zip")
            with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("large.txt", b"x" * 4096)
            for suffix, kwargs, message in (
                    ("size", {"expected_logical_bytes": 1, "expected_file_count": 1,
                              "hard_item_bytes": 8192}, "logical size"),
                    ("count", {"expected_logical_bytes": 4096, "expected_file_count": 2,
                               "hard_item_bytes": 8192}, "file count"),
                    ("hard", {"expected_logical_bytes": 4096, "expected_file_count": 1,
                              "hard_item_bytes": 1024}, "exceeds limit")):
                dest = os.path.join(root, f"out-{suffix}")
                with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, message):
                    cf.unpack_bundle_roots_file(bundle, dest, **kwargs)
                self.assertFalse(os.path.exists(dest), "rejected archive leaves no partial output")

    def test_strict_root_materialization_rejects_symlink_entry(self):
        with tempfile.TemporaryDirectory() as root:
            bundle = os.path.join(root, "bundle.zip")
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr(link, "target")
            dest = os.path.join(root, "out")
            with self.assertRaisesRegex(ValueError, "symlink|reparse"):
                cf.unpack_bundle_roots_file(bundle, dest)
            self.assertFalse(os.path.exists(dest))

    def test_strict_root_materialization_rejects_existing_reparse_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            bundle = os.path.join(root, "bundle.zip")
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr("existing/file.txt", b"payload")
            dest = os.path.join(root, "out")
            os.mkdir(dest)
            os.mkdir(os.path.join(dest, "existing"))
            real_check = cf.cpaths._is_reparse_point

            def mark_existing(path, path_stat):
                if os.path.normcase(os.fspath(path)) == os.path.normcase(
                        os.path.join(dest, "existing")):
                    return True
                return real_check(path, path_stat)

            with mock.patch.object(cf.cpaths, "_is_reparse_point", side_effect=mark_existing):
                with self.assertRaisesRegex(ValueError, "reparse"):
                    cf.unpack_bundle_roots_file(bundle, dest)
            self.assertEqual(os.listdir(os.path.join(dest, "existing")), [])


print()
suite_result = unittest.TextTestRunner(verbosity=0).run(
    unittest.defaultTestLoader.loadTestsFromTestCase(MetadataCaptureTests))
if not suite_result.wasSuccessful():
    _failures.append("metadata capture unittest regressions")
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {', '.join(_failures)}")
    sys.exit(1)
print("All clipboard file tests passed.")
