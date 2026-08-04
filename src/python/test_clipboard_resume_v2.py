"""Focused tests for transport-neutral persistent clipboard resume journals."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import uuid
from unittest import mock

import clipboard_manifest_v2 as manifest_v2
import clipboard_resume_v2 as resume


def identifier():
    return uuid.uuid4().hex


def file_entry(path, size, payload=None):
    return {
        "path": path, "type": "file", "size": size, "mtime_ns": 7,
        "source_fingerprint": {"size": size, "file_id": 19},
        "hash_state": "verified" if payload is not None else "unhashed",
        "sha256": hashlib.sha256(payload).hexdigest() if payload is not None else None,
    }


def directory_entry(path):
    return {
        "path": path, "type": "directory", "size": 0, "mtime_ns": 7,
        "source_fingerprint": {"file_id": 20}, "hash_state": "unhashed",
        "sha256": None,
    }


class ResumeTestCase(unittest.TestCase):
    def setUp(self):
        self.ids = {
            "transfer_id": identifier(), "peer_id": identifier(),
            "profile_id": identifier(), "provider_id": identifier(),
        }
        self.manifest = manifest_v2.build_manifest(identifier(), 4, [
            file_entry("a.bin", 3, b"abc"), directory_entry("empty"),
            file_entry("y-zero", 0, b""), file_entry("z.bin", 4),
        ])

    def create(self, store, direction="incoming", manifest=None):
        method = getattr(store, f"create_{direction}")
        return method(manifest=manifest or self.manifest, created_ns=100, **self.ids)


class SchemaTests(ResumeTestCase):
    def test_product_identity_domains_are_not_forced_to_uuid(self):
        with tempfile.TemporaryDirectory() as root:
            store = resume.ResumeJournalStore(root)
            manifest = manifest_v2.build_manifest("item-1", 0, [file_entry("a", 1)])
            journal = store.create_incoming(
                transfer_id=identifier(), peer_id="device_879c6b39",
                profile_id="profile-default", provider_id="device:peer-a",
                manifest=manifest)
            self.assertEqual(journal.item_id, "item-1")
            self.assertEqual(journal.provider_id, "device:peer-a")

    def test_concrete_roundtrip_is_canonical_digest_bound_and_path_free(self):
        with tempfile.TemporaryDirectory() as root:
            journal = self.create(resume.ResumeJournalStore(root))
            encoded = resume.canonical_journal_bytes(journal)
            parsed = resume.parse_journal(encoded)
            document = json.loads(encoded)

            self.assertEqual(parsed, journal)
            self.assertEqual(encoded, json.dumps(
                document, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8"))
            self.assertEqual(document["schema_version"], 1)
            self.assertEqual(document["protocol_major"], 2)
            self.assertEqual(document["direction"], "incoming")
            self.assertEqual(document["generation"], 0)
            self.assertEqual(document["journal_digest"], resume.journal_digest(document))
            self.assertEqual([entry["type"] for entry in document["entries"]],
                             ["file", "directory", "file", "file"])
            self.assertNotIn(b"a.bin", encoded)
            self.assertNotIn(b"empty", encoded)
            self.assertNotIn(b"y-zero", encoded)
            self.assertNotIn(b"z.bin", encoded)
            self.assertNotIn(b"path", encoded.lower())

    def test_outgoing_schema_binds_only_receiver_observations(self):
        with tempfile.TemporaryDirectory() as root:
            journal = self.create(resume.ResumeJournalStore(root), "outgoing")
            first = journal.entries[0]
            self.assertEqual(set(first), {
                "index", "type", "size", "source_fingerprint", "expected_sha256",
                "receiver_verified_offset", "receiver_durable_offset",
            })
            self.assertEqual(first["receiver_verified_offset"], 0)
            self.assertEqual(first["receiver_durable_offset"], 0)

    def test_journal_and_entry_set_digest_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            journal = self.create(resume.ResumeJournalStore(root))
        tampered_digest = journal.to_dict()
        tampered_digest["journal_digest"] = "0" * 64
        with self.assertRaises(resume.ResumeJournalError) as caught:
            resume.validate_journal(tampered_digest)
        self.assertEqual(caught.exception.code, "digest_mismatch")

        tampered_entry = journal.to_dict()
        tampered_entry["entries"][0]["source_fingerprint"]["file_id"] = 99
        tampered_entry["journal_digest"] = resume.journal_digest(tampered_entry)
        with self.assertRaises(resume.ResumeJournalError) as caught:
            resume.validate_journal(tampered_entry)
        self.assertEqual(caught.exception.code, "digest_mismatch")

    def test_duplicate_noncanonical_unknown_and_oversized_payloads_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            journal = self.create(resume.ResumeJournalStore(root))
            encoded = resume.canonical_journal_bytes(journal)
        duplicate = b'{"schema_version":1,"schema_version":1}'
        with self.assertRaises(resume.ResumeJournalError) as caught:
            resume.parse_journal(duplicate)
        self.assertEqual(caught.exception.code, "duplicate_key")
        with self.assertRaises(resume.ResumeJournalError) as caught:
            resume.parse_journal(b"\n" + encoded)
        self.assertEqual(caught.exception.code, "noncanonical_json")
        unknown = journal.to_dict()
        unknown["source_path"] = "C:\\private"
        with self.assertRaises(resume.ResumeJournalError) as caught:
            resume.validate_journal(unknown)
        self.assertEqual(caught.exception.code, "invalid_journal")
        with self.assertRaises(resume.ResumeJournalError) as caught:
            resume.parse_journal(b" " * (resume.MAX_JOURNAL_BYTES + 1))
        self.assertEqual(caught.exception.code, "journal_too_large")
        with self.assertRaises(resume.ResumeJournalError) as caught:
            resume.parse_journal(b'{"schema_version":2,"opaque":true}')
        self.assertEqual(caught.exception.code, "future_schema")

    def test_ids_fingerprints_integers_and_policy_are_strict(self):
        with tempfile.TemporaryDirectory() as root:
            store = resume.ResumeJournalStore(root)
            bad_ids = dict(self.ids, transfer_id=str(uuid.uuid4()))
            with self.assertRaises(resume.ResumeJournalError):
                store.create_incoming(manifest=self.manifest, **bad_ids)
            private = manifest_v2.build_manifest(identifier(), 1, [{
                **file_entry("a", 1),
                "source_fingerprint": {"source_path": "C:\\Users\\secret"},
            }])
            with self.assertRaises(resume.ResumeJournalError) as caught:
                store.create_incoming(manifest=private, **self.ids)
            self.assertEqual(caught.exception.code, "private_path")
        self.assertEqual(resume.CheckpointPolicy().byte_interval, 8 * 1024 * 1024)
        self.assertEqual(resume.CheckpointPolicy().time_interval, 0.25)
        for args in ((0, .25), (1, 0), (True, .25), (1, float("inf"))):
            with self.subTest(args=args), self.assertRaises(ValueError):
                resume.CheckpointPolicy(*args)

    def test_validated_data_is_detached_and_nested_state_is_read_only(self):
        with tempfile.TemporaryDirectory() as root:
            journal = self.create(resume.ResumeJournalStore(root))
        source = journal.to_dict()
        validated = resume.validate_journal(source)
        source["entries"][0]["source_fingerprint"]["size"] = 999
        self.assertEqual(validated.entries[0]["source_fingerprint"]["size"], 3)
        with self.assertRaises(TypeError):
            validated.entries[0]["source_fingerprint"]["size"] = 2


class GeometryTests(ResumeTestCase):
    def test_incoming_progress_is_sequential_durable_and_generation_safe(self):
        with tempfile.TemporaryDirectory() as root:
            store = resume.ResumeJournalStore(root)
            initial = self.create(store)
            prefix = hashlib.sha256(b"ab").hexdigest()
            partial = resume.update_incoming_progress(
                initial, 0, verified_offset=3, durable_offset=2,
                prefix_sha256=prefix, state="transferring", updated_ns=101)
            self.assertEqual(initial.entries[0]["verified_offset"], 0)
            self.assertEqual(partial.generation, 1)
            committed = store.commit(partial, expected_generation=0)
            self.assertEqual(committed.entries[0]["durable_offset"], 2)
            complete_hash = hashlib.sha256(b"abc").hexdigest()
            complete = resume.update_incoming_progress(
                committed, 0, verified_offset=3, durable_offset=3,
                prefix_sha256=complete_hash, completed=True,
                receiver_sha256=complete_hash, updated_ns=102)
            committed = store.commit(complete, 1)
            zero_hash = hashlib.sha256(b"").hexdigest()
            zero = resume.update_incoming_progress(
                committed, 2, verified_offset=0, durable_offset=0,
                prefix_sha256=zero_hash, completed=True,
                receiver_sha256=zero_hash, updated_ns=103)
            committed = store.commit(zero, 2)
            self.assertTrue(committed.entries[2]["completed"])
            self.assertEqual(committed.entries[1]["storage_state"], "directory")

    def test_impossible_and_nonsequential_geometry_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            initial = self.create(resume.ResumeJournalStore(root))
        with self.assertRaises(resume.ResumeJournalError) as caught:
            resume.update_incoming_progress(
                initial, 0, verified_offset=1, durable_offset=2,
                prefix_sha256="a" * 64)
        self.assertEqual(caught.exception.code, "invalid_geometry")
        with self.assertRaises(resume.ResumeJournalError) as caught:
            resume.update_incoming_progress(
                initial, 3, verified_offset=1, durable_offset=1,
                prefix_sha256="a" * 64)
        self.assertEqual(caught.exception.code, "invalid_geometry")

    def test_outgoing_observations_allow_verified_rollback_but_not_durable_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            store = resume.ResumeJournalStore(root)
            initial = self.create(store, "outgoing")
            observed = resume.update_outgoing_progress(
                initial, 0, receiver_verified_offset=3,
                receiver_durable_offset=2, updated_ns=101)
            observed = store.commit(observed, 0)
            recovered = resume.update_outgoing_progress(
                observed, 0, receiver_verified_offset=2,
                receiver_durable_offset=2, updated_ns=102)
            self.assertEqual(store.commit(recovered, 1).entries[0][
                "receiver_verified_offset"], 2)
            regressed = resume.update_outgoing_progress(
                recovered, 0, receiver_verified_offset=1,
                receiver_durable_offset=1, updated_ns=103)
            with self.assertRaises(resume.ResumeJournalError) as caught:
                store.commit(regressed, 2)
            self.assertEqual(caught.exception.code, "invalid_transition")

    def test_peer_and_manifest_mismatch_are_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            journal = self.create(resume.ResumeJournalStore(root))
        self.assertTrue(resume.validate_resume_match(
            journal, manifest=self.manifest, peer_id=self.ids["peer_id"],
            profile_id=self.ids["profile_id"], provider_id=self.ids["provider_id"]))
        with self.assertRaises(resume.ResumeJournalError) as caught:
            resume.validate_resume_match(
                journal, manifest=self.manifest, peer_id=identifier(),
                profile_id=self.ids["profile_id"], provider_id=self.ids["provider_id"])
        self.assertEqual(caught.exception.code, "peer_mismatch")
        changed = manifest_v2.build_manifest(
            self.manifest["item_id"], 5, [file_entry("different", 1)])
        with self.assertRaises(resume.ResumeJournalError) as caught:
            resume.validate_resume_match(
                journal, manifest=changed, peer_id=self.ids["peer_id"],
                profile_id=self.ids["profile_id"], provider_id=self.ids["provider_id"])
        self.assertEqual(caught.exception.code, "manifest_mismatch")

    def test_resume_pair_builds_immutable_path_free_durable_plan(self):
        with tempfile.TemporaryDirectory() as root:
            store = resume.ResumeJournalStore(root)
            incoming = self.create(store)
            outgoing_ids = dict(self.ids)
            outgoing = store.create_outgoing(
                manifest=self.manifest, created_ns=100, **outgoing_ids)
            prefix = hashlib.sha256(b"ab").hexdigest()
            incoming = store.commit(resume.update_incoming_progress(
                incoming, 0, verified_offset=2, durable_offset=2,
                prefix_sha256=prefix, state="transferring", updated_ns=101), 0)
            outgoing = store.commit(resume.update_outgoing_progress(
                outgoing, 0, receiver_verified_offset=2,
                receiver_durable_offset=2, state="transferring", updated_ns=101), 0)

            plan = resume.validate_resume_pair(outgoing, incoming, self.manifest)

            self.assertEqual(plan.transfer_id, self.ids["transfer_id"])
            self.assertEqual(plan.files[0], resume.ResumeFile(0, 3, 2, prefix, False))
            self.assertEqual(plan.durable_bytes, 2)
            self.assertNotIn("path", repr(plan).lower())
            with self.assertRaises((AttributeError, TypeError)):
                plan.files[0].durable_offset = 1

    def test_resume_pair_rejects_direction_identity_and_non_durable_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            store = resume.ResumeJournalStore(root)
            incoming = self.create(store)
            outgoing = store.create_outgoing(
                manifest=self.manifest, created_ns=100, **self.ids)
            with self.assertRaises(resume.ResumeJournalError) as caught:
                resume.validate_resume_pair(incoming, outgoing, self.manifest)
            self.assertEqual(caught.exception.code, "direction_mismatch")

            prefix = hashlib.sha256(b"a").hexdigest()
            incoming = store.commit(resume.update_incoming_progress(
                incoming, 0, verified_offset=1, durable_offset=1,
                prefix_sha256=prefix, updated_ns=101), 0)
            recovered = resume.validate_resume_pair(outgoing, incoming, self.manifest)
            self.assertEqual(recovered.files[0].durable_offset, 1)

            other_ids = dict(self.ids, transfer_id=identifier(), peer_id=identifier())
            other = store.create_outgoing(
                manifest=self.manifest, created_ns=100, **other_ids)
            with self.assertRaises(resume.ResumeJournalError) as caught:
                resume.validate_resume_pair(other, incoming, self.manifest)
            self.assertEqual(caught.exception.code, "resume_mismatch")

    def test_completed_part_can_advance_to_verified_without_hash_change(self):
        with tempfile.TemporaryDirectory() as root:
            store = resume.ResumeJournalStore(root)
            journal = self.create(store)
            digest = hashlib.sha256(b"abc").hexdigest()
            journal = store.commit(resume.update_incoming_progress(
                journal, 0, verified_offset=3, durable_offset=3,
                prefix_sha256=digest, completed=True, receiver_sha256=digest,
                storage_state="partial", state="transferring", updated_ns=101), 0)
            self.assertEqual(journal.entries[0]["storage_state"], "partial")
            verified = resume.update_incoming_progress(
                journal, 0, verified_offset=3, durable_offset=3,
                prefix_sha256=digest, completed=True, receiver_sha256=digest,
                storage_state="verified", state="finalizing", updated_ns=102)
            verified = store.commit(verified, 1)
            self.assertEqual(verified.entries[0]["storage_state"], "verified")
            self.assertEqual(verified.entries[0]["prefix_sha256"], digest)


class StoreTests(ResumeTestCase):
    def test_completed_state_requires_every_file_verified(self):
        with tempfile.TemporaryDirectory() as root:
            journal = self.create(resume.ResumeJournalStore(root))
        with self.assertRaises(resume.ResumeJournalError) as caught:
            resume.update_journal_state(journal, "completed")
        self.assertEqual(caught.exception.code, "invalid_geometry")

    def test_generation_cas_is_shared_between_store_instances(self):
        with tempfile.TemporaryDirectory() as root:
            first_store = resume.ResumeJournalStore(root)
            second_store = resume.ResumeJournalStore(root)
            initial = self.create(first_store)
            first = resume.update_journal_state(initial, "paused", updated_ns=101)
            stale = resume.update_journal_state(initial, "failed", updated_ns=102)
            first_store.commit(first, 0)
            with self.assertRaises(resume.ResumeJournalError) as caught:
                second_store.commit(stale, 0)
            self.assertEqual(caught.exception.code, "generation_conflict")

    def test_generation_cas_rejects_stale_commit(self):
        with tempfile.TemporaryDirectory() as root:
            store = resume.ResumeJournalStore(root)
            initial = self.create(store)
            first = resume.update_journal_state(initial, "paused", updated_ns=101)
            self.assertEqual(store.commit(first, 0).state, "paused")
            stale = resume.update_journal_state(initial, "failed", updated_ns=102)
            with self.assertRaises(resume.ResumeJournalError) as caught:
                store.commit(stale, 0)
            self.assertEqual(caught.exception.code, "generation_conflict")
            self.assertEqual(store.load("incoming", initial.transfer_id).state, "paused")

    def test_failed_replace_preserves_old_generation_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as root:
            store = resume.ResumeJournalStore(root)
            initial = self.create(store)
            next_journal = resume.update_journal_state(initial, "paused", updated_ns=101)
            with mock.patch.object(resume, "durable_replace", side_effect=OSError("replace")):
                with self.assertRaises(resume.ResumeJournalError) as caught:
                    store.commit(next_journal, 0)
            self.assertEqual(caught.exception.code, "store_io")
            loaded = store.load("incoming", initial.transfer_id)
            self.assertEqual((loaded.generation, loaded.state), (0, "created"))
            self.assertEqual(
                sorted(name for name in os.listdir(store.incoming_directory)
                       if not name.endswith(".lock")),
                [f"{initial.transfer_id}.json"])

    def test_commit_flushes_fsyncs_and_closes_before_replace(self):
        with tempfile.TemporaryDirectory() as root:
            store = resume.ResumeJournalStore(root)
            initial = self.create(store)
            next_journal = resume.update_journal_state(initial, "paused", updated_ns=101)
            real_replace = os.replace
            real_fsync = os.fsync
            calls = []

            class HandleProxy:
                def __init__(self, handle):
                    self.handle = handle
                    self.closed_before_exit = False

                def __getattr__(self, name):
                    return getattr(self.handle, name)

                def __enter__(self):
                    self.handle.__enter__()
                    return self

                def __exit__(self, *args):
                    result = self.handle.__exit__(*args)
                    self.closed_before_exit = self.handle.closed
                    calls.append("close")
                    return result

                def flush(self):
                    calls.append("flush")
                    return self.handle.flush()

            real_open = open
            proxy = None

            def tracked_open(path, mode="r", *args, **kwargs):
                nonlocal proxy
                handle = real_open(path, mode, *args, **kwargs)
                if mode == "xb":
                    proxy = HandleProxy(handle)
                    return proxy
                return handle

            def checked_replace(source, target):
                calls.append("replace")
                self.assertTrue(proxy.closed_before_exit)
                return real_replace(source, target)

            def tracked_fsync(descriptor):
                calls.append("fsync")
                return real_fsync(descriptor)

            with mock.patch.object(resume, "open", side_effect=tracked_open, create=True), \
                    mock.patch.object(resume.os, "fsync", side_effect=tracked_fsync) as fsync, \
                    mock.patch.object(resume, "durable_replace", side_effect=checked_replace):
                committed = store.commit(next_journal, 0)
            self.assertEqual(committed.generation, 1)
            self.assertGreaterEqual(fsync.call_count, 1)
            self.assertLess(calls.index("flush"), calls.index("close"))
            self.assertLess(calls.index("fsync"), calls.index("close"))
            self.assertLess(calls.index("close"), calls.index("replace"))

    def test_startup_classifies_valid_quarantines_bad_and_leaves_future_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            store = resume.ResumeJournalStore(root)
            valid = self.create(store)
            malformed_id = identifier()
            malformed_path = os.path.join(store.outgoing_directory, f"{malformed_id}.json")
            with open(malformed_path, "wb") as handle:
                handle.write(b'{"schema_version":1,"schema_version":1}')
            future_id = identifier()
            future_path = os.path.join(store.incoming_directory, f"{future_id}.json")
            future = json.dumps({"schema_version": 2, "opaque": True},
                                sort_keys=True, separators=(",", ":")).encode()
            with open(future_path, "wb") as handle:
                handle.write(future)
            unrelated = os.path.join(store.incoming_directory, "notes.txt")
            with open(unrelated, "w", encoding="ascii") as handle:
                handle.write("keep")

            inventory = store.scan_startup()

            self.assertEqual([item.transfer_id for item in inventory.incoming],
                             [valid.transfer_id])
            self.assertEqual(inventory.outgoing, ())
            self.assertEqual(inventory.future, (f"incoming/{future_id}.json",))
            self.assertTrue(os.path.exists(future_path))
            with open(future_path, "rb") as handle:
                self.assertEqual(handle.read(), future)
            self.assertTrue(os.path.exists(unrelated))
            self.assertEqual(len(inventory.quarantined), 1)
            self.assertFalse(os.path.exists(malformed_path))

    def test_scan_quarantines_oversized_and_unknown_supported_journals(self):
        with tempfile.TemporaryDirectory() as root:
            store = resume.ResumeJournalStore(root)
            oversized_id = identifier()
            with open(os.path.join(store.incoming_directory, f"{oversized_id}.json"), "wb") as handle:
                handle.truncate(resume.MAX_JOURNAL_BYTES + 1)
            unknown_id = identifier()
            with open(os.path.join(store.outgoing_directory, f"{unknown_id}.json"), "wb") as handle:
                handle.write(b'{"schema_version":1,"unknown":true}')
            inventory = store.scan_startup()
            self.assertEqual(len(inventory.quarantined), 2)
            self.assertEqual(inventory.incoming, ())
            self.assertEqual(inventory.outgoing, ())

    def test_manual_quarantine_and_purge_are_isolated_to_journal(self):
        with tempfile.TemporaryDirectory() as root:
            store = resume.ResumeJournalStore(root)
            incoming = self.create(store)
            payload = os.path.join(root, "payload.part")
            with open(payload, "wb") as handle:
                handle.write(b"payload")
            quarantine_name = store.quarantine("incoming", incoming.transfer_id)
            self.assertTrue(os.path.exists(os.path.join(
                store.quarantine_directory, quarantine_name)))
            with open(payload, "rb") as handle:
                self.assertEqual(handle.read(), b"payload")

            outgoing_ids = dict(self.ids, transfer_id=identifier())
            outgoing = store.create_outgoing(
                manifest=self.manifest, created_ns=100, **outgoing_ids)
            self.assertTrue(store.purge("outgoing", outgoing.transfer_id))
            self.assertFalse(store.purge("outgoing", outgoing.transfer_id))
            with open(payload, "rb") as handle:
                self.assertEqual(handle.read(), b"payload")
            self.assertTrue(os.path.exists(os.path.join(
                store.quarantine_directory, quarantine_name)))


if __name__ == "__main__":
    unittest.main()
