"""Focused tests for clipboard V2 path and batch manifest foundations."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import clipboard_manifest_v2 as manifest_v2
import clipboard_paths as paths
import clipboard_protocol as protocol
import clipboard_transfer as transfer


def file_entry(path, size=0, **updates):
    entry = {
        "path": path,
        "type": "file",
        "size": size,
        "mtime_ns": 0,
        "source_fingerprint": {"size": size, "strong": True},
        "hash_state": "unhashed",
        "sha256": None,
    }
    entry.update(updates)
    return entry


def directory_entry(path):
    return {
        "path": path,
        "type": "directory",
        "size": 0,
        "mtime_ns": 0,
        "source_fingerprint": {},
        "hash_state": "unhashed",
        "sha256": None,
    }


class RemotePathTests(unittest.TestCase):
    def test_separator_normalization_and_unicode(self):
        self.assertEqual(paths.canonical_relative_path("Ordner\\Gr\u00f6e.txt"), "Ordner/Gr\u00f6e.txt")

    def test_rejects_unsafe_windows_paths(self):
        invalid = {
            "empty": "",
            "absolute": "/root/file",
            "UNC": r"\\server\share\file",
            "device": r"\\?\C:\file",
            "drive": r"C:\file",
            "dot": "folder/./file",
            "parent": "folder/../file",
            "NUL": "folder/a\x00b",
            "ADS": "folder/file:stream",
            "invalid character": "folder/a?.txt",
            "empty component": "folder//file",
            "trailing dot": "folder/name.",
            "trailing space": "folder/name ",
            "reserved extension": "folder/CON.txt",
            "reserved mixed case": "folder/com1.Log",
        }
        for label, candidate in invalid.items():
            with self.subTest(label=label), self.assertRaises(paths.PathValidationError):
                paths.canonical_relative_path(candidate)

    def test_enforces_utf8_path_and_component_limits(self):
        with self.assertRaisesRegex(paths.PathValidationError, "component"):
            paths.canonical_relative_path("\u00e9" * 128)
        components = ["a" * 255] * 5
        with self.assertRaisesRegex(paths.PathValidationError, "1024"):
            paths.canonical_relative_path("/".join(components))

    def test_rejects_duplicates_case_collisions_and_file_ancestors(self):
        with self.assertRaisesRegex(paths.PathValidationError, "duplicate"):
            paths.validate_path_entries([file_entry("a.txt"), file_entry("a.txt")])
        with self.assertRaisesRegex(paths.PathValidationError, "case collision"):
            paths.validate_path_entries([file_entry("File.txt"), file_entry("file.txt")])
        with self.assertRaisesRegex(paths.PathValidationError, "case collision"):
            paths.validate_path_entries([file_entry("\u00c4.txt"), file_entry("\u00e4.txt")])
        with self.assertRaisesRegex(paths.PathValidationError, "case collision"):
            paths.validate_path_entries([file_entry("Data/a.txt"), file_entry("data/b.txt")])
        with self.assertRaisesRegex(paths.PathValidationError, "ancestor"):
            paths.validate_path_entries([file_entry("folder"), file_entry("folder/child.txt")])

    def test_windows_collision_key_is_non_expanding(self):
        self.assertEqual(
            paths.validate_path_entries([
                file_entry("Stra\u00dfe.txt"), file_entry("Strasse.txt")]),
            ["Stra\u00dfe.txt", "Strasse.txt"],
        )
        with self.assertRaisesRegex(paths.PathValidationError, "case collision"):
            paths.validate_path_entries([file_entry("File.txt"), file_entry("file.txt")])

    def test_allows_directory_ancestors(self):
        self.assertEqual(
            paths.validate_path_entries([directory_entry("folder"), file_entry("folder/child.txt")]),
            ["folder", "folder/child.txt"],
        )

    def test_safe_target_is_contained_and_rejects_reparse_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(os.path.join(root, "existing"))
            target = paths.safe_target_path(root, "existing/new/file.txt")
            self.assertEqual(target, Path(root).resolve() / "existing" / "new" / "file.txt")

            real_check = paths._is_reparse_point

            def mark_existing_as_reparse(path, path_stat):
                if Path(path) == Path(root) / "existing":
                    return True
                return real_check(path, path_stat)

            with mock.patch.object(paths, "_is_reparse_point", side_effect=mark_existing_as_reparse):
                with self.assertRaisesRegex(paths.PathValidationError, "reparse"):
                    paths.safe_target_path(root, "existing/file.txt")


class LegacyGeometryTests(unittest.TestCase):
    def test_rejects_hostile_tiny_chunks_and_excessive_count(self):
        with self.assertRaisesRegex(ValueError, "geometry"):
            protocol.build_transfer_start(
                "tiny-transfer", "tiny-item", "a" * 64,
                protocol.MIN_LEGACY_CHUNK_SIZE, 1)
        hostile = {
            "type": protocol.T_START, "transfer_id": "tiny-transfer",
            "item_id": "tiny-item", "sha256": "a" * 64,
            "total_size": protocol.MAX_LEGACY_CHUNK_COUNT + 1,
            "chunk_size": 1,
            "chunk_count": protocol.MAX_LEGACY_CHUNK_COUNT + 1,
        }
        self.assertIsNone(protocol.parse_transfer_start(hostile))
        with self.assertRaisesRegex(ValueError, "geometry"):
            protocol.ChunkAssembler(1, protocol.MAX_LEGACY_CHUNK_COUNT + 1)
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "geometry"):
                transfer.DiskChunkAssembler(
                    1, protocol.MAX_LEGACY_CHUNK_COUNT + 1, "a" * 64,
                    os.path.join(root, "payload.part"))

    def test_zero_size_geometry_remains_valid(self):
        start = protocol.build_transfer_start(
            "zero-transfer", "zero-item", "a" * 64, 0, 1)
        self.assertEqual(start["chunk_count"], 0)
        self.assertIsNotNone(protocol.parse_transfer_start(start))

    def test_final_ack_parser_is_strict(self):
        ack = protocol.build_transfer_ack("ack-transfer", "ack-item")
        self.assertEqual(protocol.parse_transfer_ack(ack)["status"],
                         protocol.ACK_FINAL_COMPLETE)
        for mutation in (
                dict(ack, status="ok"), dict(ack, peer="unexpected"),
                {"type": protocol.T_ACK, "transfer_id": "ack-transfer"}):
            self.assertIsNone(protocol.parse_transfer_ack(mutation))

    def test_request_items_final_ack_is_explicit_and_strict(self):
        request = protocol.build_request_items("profile", ["item"])
        self.assertIs(protocol.parse_request_items(request)["final_ack"], True)
        legacy = dict(request)
        legacy.pop("final_ack")
        self.assertIs(protocol.parse_request_items(legacy)["final_ack"], False)
        self.assertIsNone(protocol.parse_request_items(dict(request, final_ack=1)))


class ManifestTests(unittest.TestCase):
    def test_finalization_advances_revision_once_and_rejects_conflicts(self):
        provisional = manifest_v2.build_manifest(
            "item-final", 9,
            [file_entry("a", 3), directory_entry("folder"), file_entry("z", 0)])
        hashes = {
            0: hashlib.sha256(b"abc").hexdigest(),
            2: hashlib.sha256(b"").hexdigest(),
        }
        finalized = manifest_v2.finalize_manifest(provisional, hashes)
        self.assertEqual(finalized["item_revision"], 10)
        self.assertNotEqual(finalized["manifest_digest"], provisional["manifest_digest"])
        self.assertEqual(
            [(entry["hash_state"], entry["sha256"]) for entry in finalized["entries"]],
            [("verified", hashes[0]), ("unhashed", None), ("verified", hashes[2])])
        self.assertEqual(manifest_v2.finalize_manifest(finalized, hashes), finalized)
        with self.assertRaisesRegex(manifest_v2.ManifestValidationError, "exactly"):
            manifest_v2.finalize_manifest(provisional, {0: hashes[0]})
        with self.assertRaisesRegex(manifest_v2.ManifestValidationError, "conflicts"):
            manifest_v2.finalize_manifest(finalized, {0: "0" * 64, 2: hashes[2]})

    def test_canonical_digest_round_trip_preserves_special_entries(self):
        manifest = manifest_v2.build_manifest(
            "copy-event-1",
            7,
            [
                file_entry("Z-empty.bin"),
                file_entry("\u0394\u03bf\u03ba\u03b9\u03bc\u03ae/\u03ba\u03b5\u03b9\u03bc\u03b5\u03bd\u03bf.txt", 4),
                directory_entry("empty-directory"),
                directory_entry("\u0394\u03bf\u03ba\u03b9\u03bc\u03ae"),
            ],
        )
        encoded = manifest_v2.canonical_manifest_bytes(manifest)
        parsed = manifest_v2.parse_manifest(encoded)

        self.assertEqual(parsed, manifest)
        self.assertIn("\u0394\u03bf\u03ba\u03b9\u03bc\u03ae".encode("utf-8"), encoded)
        self.assertNotIn(b" ", encoded)
        self.assertEqual([entry["index"] for entry in parsed["entries"]], [0, 1, 2, 3])
        self.assertEqual(
            [entry["path"] for entry in parsed["entries"]],
            sorted(entry["path"] for entry in parsed["entries"]),
        )
        self.assertEqual(parsed["total_size"], 4)
        self.assertEqual(parsed["file_count"], 2)
        self.assertEqual(parsed["directory_count"], 2)
        digest_input = dict(parsed)
        digest_input.pop("manifest_digest")
        self.assertEqual(
            parsed["manifest_digest"],
            hashlib.sha256(manifest_v2.canonical_json_bytes(digest_input)).hexdigest(),
        )

    def test_rejects_wrong_schema_protocol_digest_order_and_indices(self):
        base = manifest_v2.build_manifest("item-1", 1, [file_entry("a"), file_entry("b")])
        mutations = []
        for field, value in (
            ("schema_version", 1),
            ("schema_version", 2.0),
            ("protocol_major", 1),
            ("protocol_major", 2.0),
        ):
            changed = dict(base)
            changed[field] = value
            mutations.append(changed)
        changed = dict(base)
        changed["manifest_digest"] = "0" * 64
        mutations.append(changed)
        changed = dict(base)
        changed["entries"] = list(reversed(base["entries"]))
        changed["manifest_digest"] = manifest_v2.manifest_digest(changed)
        mutations.append(changed)
        changed = dict(base)
        changed["entries"] = [dict(entry) for entry in base["entries"]]
        changed["entries"][1]["index"] = 8
        changed["manifest_digest"] = manifest_v2.manifest_digest(changed)
        mutations.append(changed)

        for changed in mutations:
            with self.subTest(changed=changed), self.assertRaises(manifest_v2.ManifestValidationError):
                manifest_v2.validate_manifest(changed)

    def test_rejects_noncanonical_json_and_unknown_fields(self):
        manifest = manifest_v2.build_manifest("item-2", 0, [file_entry("zero")])
        pretty = ("\n" + manifest_v2.canonical_manifest_bytes(manifest).decode("utf-8")).encode("utf-8")
        with self.assertRaisesRegex(manifest_v2.ManifestValidationError, "not canonical"):
            manifest_v2.parse_manifest(pretty)
        manifest["unexpected"] = True
        with self.assertRaisesRegex(manifest_v2.ManifestValidationError, "unknown"):
            manifest_v2.validate_manifest(manifest)

    def test_rejects_uint64_and_configured_size_cap_violations(self):
        with self.assertRaisesRegex(manifest_v2.ManifestValidationError, "unsigned 64-bit"):
            manifest_v2.build_manifest("item-3", 0, [file_entry("huge", 1 << 64)])
        limits = manifest_v2.ManifestLimits(max_total_size=10, max_file_size=6)
        with self.assertRaisesRegex(manifest_v2.ManifestValidationError, "file exceeds"):
            manifest_v2.build_manifest("item-3", 0, [file_entry("large", 7)], limits=limits)
        with self.assertRaisesRegex(manifest_v2.ManifestValidationError, "total size"):
            manifest_v2.build_manifest(
                "item-3", 0, [file_entry("a", 6), file_entry("b", 5)], limits=limits
            )

    def test_rejects_count_and_manifest_byte_caps(self):
        count_limits = manifest_v2.ManifestLimits(max_files=1, max_entries=2)
        with self.assertRaisesRegex(manifest_v2.ManifestValidationError, "file count"):
            manifest_v2.build_manifest(
                "item-4", 0, [file_entry("a"), file_entry("b")], limits=count_limits
            )
        size_limits = manifest_v2.ManifestLimits(max_manifest_bytes=200)
        with self.assertRaisesRegex(manifest_v2.ManifestValidationError, "size limit"):
            manifest_v2.build_manifest("item-4", 0, [file_entry("a")], limits=size_limits)

    def test_manifest_rejects_path_collision_as_a_whole(self):
        with self.assertRaisesRegex(manifest_v2.ManifestValidationError, "case collision"):
            manifest_v2.build_manifest(
                "item-5", 0, [directory_entry("Data"), file_entry("data/value.bin")]
            )


if __name__ == "__main__":
    unittest.main()
