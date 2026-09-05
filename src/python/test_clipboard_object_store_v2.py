"""Publication/receipt end-state tests with real receiver staging and disk indexes."""
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import unittest
import uuid
from unittest import mock

import clipboard_files as files
import clipboard_manifest_v2 as manifests
import clipboard_model as model
import clipboard_preflight_v2 as preflight
import clipboard_object_store_v2 as objects
import clipboard_resume_v2 as resume
import clipboard_store as stores
import clipboard_streaming_v2 as streaming


class ObjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = stores.ClipboardStore(str(self.root), "a")
        self.objects = self.store.object_store_v2
        self.source = self.root / "source.bin"
        self.source.write_bytes(b"identical payload" * 100)

    def prepare_stage(self, paths=None, store=None):
        store = store or self.store
        provisional = files.make_file_item(paths or [self.source])
        # Receiver metadata has no usable sender-local paths.
        local = provisional["files"]
        remote = copy.deepcopy(provisional)
        remote.pop("files")
        remote["source_available"] = False
        remote = model.version_item(remote, payload_state="receiving")
        provisional, _ = store.add_item(remote, make_current=True)
        identifier = uuid.uuid4().hex
        manifest = provisional["batch_manifest"]
        acceptance = preflight.accept_preflight(identifier, manifest,
            preflight.estimate_stream_v2(manifest, free_bytes=10 ** 12),
            now=0, expires_at=10 ** 12)
        source = streaming.SequentialFileStream(identifier, manifest, local,
                                                accepted_preflight=acceptance)
        stage = streaming.IncomingTransferStage.create(
            str(self.root / "incoming"), identifier, manifest,
            journal_store=resume.ResumeJournalStore(self.root / "journals"),
            peer_id="peer-a", profile_id=store.profile_id, provider_id="device:peer-a",
            accepted_preflight=acceptance)
        self.addCleanup(source.close)
        return provisional, source, stage

    def stage(self, paths=None, store=None):
        provisional, source, stage = self.prepare_stage(paths, store)
        for chunk in source.iter_chunks():
            stage.accept(chunk)
        result = stage.finalize(source.completion())
        return provisional, result

    def commit(self, provisional, result, store=None):
        store = store or self.store
        publication = store.object_store_v2.publish_staged_transfer(result)
        item, evicted = store.commit_received_v2_item(provisional, publication)
        self.assertEqual(evicted, [])
        self.assertTrue(store.verify_received_v2_publication(publication))
        return item, publication

    def assert_published(self, store, provisional, item, revision, payload):
        self.assertEqual(item["item_id"], provisional["item_id"])
        self.assertEqual(item["item_revision"], provisional["item_revision"] + 1)
        self.assertEqual(item["payload_state"], "cached")
        self.assertEqual(store.revision, revision + 1)
        self.assertEqual(store.list_items(), [item])
        self.assertEqual(store.current_item_id, item["item_id"])
        entry, = item["batch_manifest"]["entries"]
        self.assertEqual(entry["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(Path(store.object_store_v2.object_path(entry["sha256"])).read_bytes(),
                         payload)
        document = json.loads(Path(store.index_path).read_text())
        self.assertEqual(len(document["v2_receipts"]), 1)

    def test_resumable_publish_completes_journal_only_after_durable_index(self):
        provisional, source, stage = self.prepare_stage()
        payload = self.source.read_bytes()
        for chunk in source.iter_chunks():
            stage.accept(chunk)
        result = stage.finalize(source.completion())
        journals = resume.ResumeJournalStore(self.root / "journals")
        self.assertEqual(journals.load("incoming", stage.transfer_id).state, "finalizing")
        self.assertEqual(stage.journal.profile_id, self.store.profile_id)
        self.assertEqual(Path(result.files[0].path).read_bytes(), payload)
        revision = self.store.revision
        original_commit = resume.ResumeJournalStore.commit
        completed = []

        def observe_commit(journal_store, candidate, expected_generation):
            if candidate.state == "completed":
                # A fresh reader proves the receipt is on disk, not just in memory.
                durable = stores.ClipboardStore(str(self.root), "a")
                item = durable.get_item(provisional["item_id"])
                self.assert_published(durable, provisional, item, revision, payload)
                self.assertEqual(journals.load("incoming", stage.transfer_id).state,
                                 "finalizing")
                self.assertTrue(Path(stage.stage_directory).is_dir())
                completed.append(candidate.transfer_id)
            return original_commit(journal_store, candidate, expected_generation)

        with mock.patch.object(resume.ResumeJournalStore, "commit", new=observe_commit):
            item, evicted = stage.publish(self.store, provisional)
        self.assertEqual(evicted, [])
        self.assertEqual(completed, [stage.transfer_id])
        self.assertEqual(journals.load("incoming", stage.transfer_id).state, "completed")
        self.assertFalse(Path(stage.stage_directory).exists())
        self.assert_published(self.store, provisional, item, revision, payload)
        restarted = stores.ClipboardStore(str(self.root), "a")
        self.assert_published(restarted, provisional, item, revision, payload)

    def test_resumable_index_failure_reopens_rehashes_and_publishes_once(self):
        provisional, source, stage = self.prepare_stage()
        payload = self.source.read_bytes()
        for chunk in source.iter_chunks():
            stage.accept(chunk)
        result = stage.finalize(source.completion())
        revision = self.store.revision
        index = Path(self.store.index_path).read_bytes()
        with mock.patch.object(stores, "_atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                stage.publish(self.store, provisional)
        journals = resume.ResumeJournalStore(self.root / "journals")
        journal = journals.load("incoming", stage.transfer_id)
        self.assertEqual(journal.state, "finalizing")
        self.assertEqual(Path(self.store.index_path).read_bytes(), index)
        self.assertEqual(self.store.revision, revision)
        self.assertEqual(self.store.list_items(), [provisional])
        self.assertEqual(Path(result.files[0].path).read_bytes(), payload)
        self.assertEqual(journal.entries[0]["durable_offset"], len(payload))
        self.assertTrue(journal.entries[0]["completed"])
        self.assertEqual(journal.entries[0]["storage_state"], "verified")
        self.assertEqual(self.objects.collect_garbage(), 0)

        restarted = stores.ClipboardStore(str(self.root), "a")
        manifest = provisional["batch_manifest"]
        acceptance = preflight.accept_preflight(stage.transfer_id, manifest,
            preflight.estimate_stream_v2(manifest, free_bytes=10 ** 12),
            now=0, expires_at=10 ** 12)
        self.assertEqual(acceptance.estimate.remaining_payload_bytes, len(payload))
        reopened = streaming.IncomingTransferStage.reopen(
            self.root / "incoming", stage.transfer_id, manifest,
            journal_store=journals, peer_id=journal.peer_id,
            profile_id=restarted.profile_id, provider_id=journal.provider_id,
            accepted_preflight=acceptance)
        recovered = reopened.recover_finalization()
        self.assertEqual(recovered.finalized_manifest, result.finalized_manifest)
        self.assertEqual(recovered.files[0].sha256, hashlib.sha256(payload).hexdigest())
        item, evicted = reopened.publish(restarted, provisional)
        self.assertEqual(evicted, [])
        self.assertEqual(journals.load("incoming", stage.transfer_id).state, "completed")
        self.assertFalse(Path(reopened.stage_directory).exists())
        self.assert_published(restarted, provisional, item, revision, payload)
        durable = stores.ClipboardStore(str(self.root), "a")
        self.assert_published(durable, provisional, item, revision, payload)

    def test_complete_publication_before_index_commit_retains_stage_and_journal(self):
        provisional, source, stage = self.prepare_stage()
        for chunk in source.iter_chunks():
            stage.accept(chunk)
        result = stage.finalize(source.completion())
        publication = self.objects.publish_staged_transfer(result)
        journals = resume.ResumeJournalStore(self.root / "journals")
        journal = journals.load("incoming", stage.transfer_id)
        revision = self.store.revision
        with self.assertRaises(streaming.StreamV2Error) as caught:
            stage.complete_publication(self.store, publication)
        self.assertEqual(caught.exception.code, "publication_incomplete")
        self.assertEqual(journals.load("incoming", stage.transfer_id), journal)
        self.assertEqual(journal.state, "finalizing")
        self.assertEqual(Path(result.files[0].path).read_bytes(), self.source.read_bytes())
        self.assertEqual(self.store.list_items(), [provisional])
        self.assertEqual(self.store.revision, revision)
        self.assertFalse(self.store.verify_received_v2_publication(publication))
        item, evicted = stage.publish(self.store, provisional)
        self.assertEqual(evicted, [])
        self.assertEqual(journals.load("incoming", stage.transfer_id).state, "completed")
        self.assertFalse(Path(stage.stage_directory).exists())
        self.assert_published(self.store, provisional, item, revision, self.source.read_bytes())

    def test_resumable_publication_rejects_wrong_profile_even_with_durable_receipt(self):
        provisional, source, stage = self.prepare_stage()
        for chunk in source.iter_chunks():
            stage.accept(chunk)
        result = stage.finalize(source.completion())
        other = stores.ClipboardStore(str(self.root), "b")
        other_provisional, _ = other.add_item(provisional, make_current=True)
        journal = stage.journal
        with self.assertRaises(streaming.StreamV2Error) as caught:
            stage.publish(other, other_provisional)
        self.assertEqual(caught.exception.code, "publication_incomplete")
        self.assertEqual(other.list_items(), [other_provisional])
        publication = self.objects.publish_staged_transfer(result)
        other.commit_received_v2_item(other_provisional, publication)
        self.assertTrue(other.verify_received_v2_publication(publication))
        with self.assertRaises(streaming.StreamV2Error) as caught:
            stage.complete_publication(other, publication)
        self.assertEqual(caught.exception.code, "publication_incomplete")
        journals = resume.ResumeJournalStore(self.root / "journals")
        self.assertEqual(journals.load("incoming", stage.transfer_id), journal)
        self.assertEqual(Path(result.files[0].path).read_bytes(), self.source.read_bytes())
        self.assertEqual(self.store.list_items(), [provisional])
        self.assertFalse(self.store.verify_received_v2_publication(publication))

    def test_source_tamper_prevents_completion_and_publication(self):
        provisional, source, stage = self.prepare_stage()
        chunks = source.iter_chunks()
        stage.accept(next(chunks))
        # Mutate after the last payload but before sender completion validation.
        self.source.write_bytes(b"changed source")
        with self.assertRaises(streaming.StreamV2Error) as caught:
            next(chunks)
        self.assertEqual(caught.exception.code, "source_changed")
        with self.assertRaises(streaming.StreamV2Error) as caught:
            source.completion()
        self.assertEqual(caught.exception.code, "source_unavailable")
        with self.assertRaises(streaming.StreamV2Error) as caught:
            stage.publish(self.store, provisional)
        self.assertEqual(caught.exception.code, "publication_incomplete")
        self.assertEqual(self.store.list_items(), [provisional])
        self.assertEqual(list(Path(self.objects.objects_root).glob("*/*")), [])
        journals = resume.ResumeJournalStore(self.root / "journals")
        self.assertNotIn(journals.load("incoming", stage.transfer_id).state,
                         ("finalizing", "completed"))
        self.assertTrue(Path(stage.stage_directory).is_dir())

    def test_receiver_tamper_before_finalize_or_publish_never_commits(self):
        for boundary in ("finalize", "publish"):
            with self.subTest(boundary=boundary):
                provisional, source, stage = self.prepare_stage()
                for chunk in source.iter_chunks():
                    stage.accept(chunk)
                if boundary == "publish":
                    result = stage.finalize(source.completion())
                    path = Path(result.files[0].path)
                else:
                    path = Path(stage.stage_directory) / "0.part"
                path.write_bytes(b"z" * self.source.stat().st_size)
                # Make fingerprint invalidation deterministic on coarse filesystems.
                info = path.stat()
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
                index = Path(self.store.index_path).read_bytes()
                error = streaming.StreamV2Error if boundary == "finalize" else objects.ObjectStoreV2Error
                with self.assertRaises(error) as caught:
                    if boundary == "finalize":
                        stage.finalize(source.completion())
                    else:
                        stage.publish(self.store, provisional)
                self.assertEqual(caught.exception.code, "stage_changed")
                self.assertEqual(Path(self.store.index_path).read_bytes(), index)
                self.assertEqual(self.store.get_item(provisional["item_id"]), provisional)
                self.assertEqual(list(Path(self.objects.objects_root).glob("*/*")), [])
                journals = resume.ResumeJournalStore(self.root / "journals")
                self.assertEqual(journals.load("incoming", stage.transfer_id).state,
                                 "failed" if boundary == "finalize" else "finalizing")
                self.assertEqual(path.read_bytes(), b"z" * self.source.stat().st_size)

    def test_rename_handoff_same_size_restoremtime_tamper_never_commits(self):
        for boundary in ("part", "verified"):
            with self.subTest(boundary=boundary):
                provisional, source, stage = self.prepare_stage()
                payload = self.source.read_bytes()
                tampered = b"z" * len(payload)
                for chunk in source.iter_chunks():
                    stage.accept(chunk)
                index = Path(self.store.index_path).read_bytes()
                revision = self.store.revision
                original_replace = streaming.resume_v2.durable_replace
                attempts = []
                writes = []

                def tamper_during_replace(part, verified):
                    if Path(part) != Path(stage.stage_directory) / "0.part":
                        return original_replace(part, verified)
                    path = Path(part if boundary == "part" else verified)
                    if boundary == "verified":
                        original_replace(part, verified)
                    info = path.stat()
                    attempts.append(path)
                    # Windows must deny this open; POSIX must detect the write
                    # even though the payload length and mtime are unchanged.
                    with open(path, "r+b", buffering=0) as handle:
                        self.assertEqual(handle.write(tampered), len(tampered))
                        os.fsync(handle.fileno())
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
                    self.assertEqual(path.stat().st_size, info.st_size)
                    self.assertEqual(path.stat().st_mtime_ns, info.st_mtime_ns)
                    writes.append(path)
                    if boundary == "part":
                        original_replace(part, verified)

                with mock.patch.object(streaming.resume_v2, "durable_replace",
                                       side_effect=tamper_during_replace):
                    with self.assertRaises(streaming.StreamV2Error) as caught:
                        stage.finalize(source.completion())
                attempted = Path(stage.stage_directory) / (
                    "0.part" if boundary == "part" else "0.verified")
                self.assertEqual(attempts, [attempted])
                if os.name == "nt":
                    self.assertEqual(writes, [])
                    self.assertIsInstance(caught.exception.__cause__, PermissionError)
                    self.assertEqual(attempted.read_bytes(), payload)
                else:
                    self.assertEqual(writes, [attempted])
                    self.assertEqual(caught.exception.code, "stage_changed")
                    self.assertEqual((Path(stage.stage_directory) / "0.verified").read_bytes(),
                                     tampered)
                with self.assertRaises(streaming.StreamV2Error) as caught:
                    stage.publish(self.store, provisional)
                self.assertEqual(caught.exception.code, "publication_incomplete")
                self.assertEqual(Path(self.store.index_path).read_bytes(), index)
                self.assertEqual(self.store.revision, revision)
                self.assertEqual(self.store.get_item(provisional["item_id"]), provisional)
                self.assertNotIn(stage.transfer_id, json.loads(index).get("v2_receipts", {}))
                journals = resume.ResumeJournalStore(self.root / "journals")
                self.assertEqual(journals.load("incoming", stage.transfer_id).state, "failed")
                self.assertTrue(Path(stage.stage_directory).is_dir())
                self.assertEqual(list(Path(self.objects.pending_root).iterdir()), [])
                self.assertEqual(list(Path(self.objects.objects_root).glob("*/*")), [])
                self.assertEqual(list(Path(self.objects.manifests_root).glob("*/*")), [])

    def test_crash_before_cleanup_leaves_completed_journal_and_durable_item(self):
        provisional, source, stage = self.prepare_stage()
        payload = self.source.read_bytes()
        for chunk in source.iter_chunks():
            stage.accept(chunk)
        result = stage.finalize(source.completion())
        revision = self.store.revision
        with mock.patch.object(stage, "_cleanup", side_effect=OSError("crash before cleanup")):
            with self.assertRaisesRegex(OSError, "crash before cleanup"):
                stage.publish(self.store, provisional)
        journals = resume.ResumeJournalStore(self.root / "journals")
        self.assertEqual(journals.load("incoming", stage.transfer_id).state, "completed")
        self.assertEqual(Path(result.files[0].path).read_bytes(), payload)
        restarted = stores.ClipboardStore(str(self.root), "a")
        item = restarted.get_item(provisional["item_id"])
        self.assert_published(restarted, provisional, item, revision, payload)
        journal = journals.load("incoming", stage.transfer_id)
        index = Path(restarted.index_path).read_bytes()
        for _ in range(2):
            self.assertTrue(streaming.IncomingTransferStage.cleanup_completed(
                self.root / "incoming", stage.transfer_id, provisional["batch_manifest"],
                journal_store=journals, store=restarted, peer_id=journal.peer_id,
                profile_id=journal.profile_id, provider_id=journal.provider_id))
            self.assertFalse(Path(stage.stage_directory).exists())
            self.assertEqual(journals.load("incoming", stage.transfer_id), journal)
            self.assertEqual(Path(restarted.index_path).read_bytes(), index)
            self.assert_published(restarted, provisional, item, revision, payload)

    def test_completed_cleanup_refuses_failed_receipt_and_unknown_stage_data(self):
        for obstruction in ("receipt", "unknown_file", "unknown_directory"):
            with self.subTest(obstruction=obstruction):
                provisional, source, stage = self.prepare_stage()
                for chunk in source.iter_chunks():
                    stage.accept(chunk)
                result = stage.finalize(source.completion())
                with mock.patch.object(stage, "_cleanup", side_effect=OSError("crash")):
                    with self.assertRaisesRegex(OSError, "crash"):
                        stage.publish(self.store, provisional)
                journals = resume.ResumeJournalStore(self.root / "journals")
                journal = journals.load("incoming", stage.transfer_id)
                self.assertEqual(journal.state, "completed")
                index_path = Path(self.store.index_path)
                committed_index = index_path.read_bytes()
                unexpected = Path(stage.stage_directory) / "user-data"
                if obstruction == "receipt":
                    document = json.loads(committed_index)
                    del document["v2_receipts"][stage.transfer_id]
                    index_path.write_text(json.dumps(document))
                elif obstruction == "unknown_file":
                    unexpected.write_bytes(b"preserve user data")
                else:
                    unexpected.mkdir()
                    (unexpected / "nested.bin").write_bytes(b"preserve user data")
                index = index_path.read_bytes()
                restarted = stores.ClipboardStore(str(self.root), "a")
                with self.assertRaises(streaming.StreamV2Error) as caught:
                    streaming.IncomingTransferStage.cleanup_completed(
                        self.root / "incoming", stage.transfer_id, provisional["batch_manifest"],
                        journal_store=journals, store=restarted, peer_id=journal.peer_id,
                        profile_id=journal.profile_id, provider_id=journal.provider_id)
                self.assertEqual(caught.exception.code,
                                 "publication_incomplete" if obstruction == "receipt"
                                 else "corrupt_partial")
                self.assertEqual(journals.load("incoming", stage.transfer_id), journal)
                self.assertEqual(index_path.read_bytes(), index)
                self.assertEqual(Path(result.files[0].path).read_bytes(), self.source.read_bytes())
                self.assertEqual(Path(self.objects.object_path(result.files[0].sha256)).read_bytes(),
                                 self.source.read_bytes())
                if obstruction == "unknown_file":
                    self.assertEqual(unexpected.read_bytes(), b"preserve user data")
                elif obstruction == "unknown_directory":
                    self.assertEqual((unexpected / "nested.bin").read_bytes(), b"preserve user data")
                index_path.write_bytes(committed_index)

    def test_failed_index_cancel_purge_and_gc_release_pins_but_keep_shared_objects(self):
        payload = self.source.read_bytes()
        for shared in (False, True):
            with self.subTest(shared=shared):
                if shared:
                    other = stores.ClipboardStore(str(self.root), "b")
                    other_provisional, other_source, other_stage = self.prepare_stage(store=other)
                    for chunk in other_source.iter_chunks():
                        other_stage.accept(chunk)
                    other_result = other_stage.finalize(other_source.completion())
                    other_revision = other.revision
                    other_item, evicted = other_stage.publish(other, other_provisional)
                    self.assertEqual(evicted, [])
                    self.assertFalse(Path(other_stage.stage_directory).exists())
                provisional, source, stage = self.prepare_stage()
                for chunk in source.iter_chunks():
                    stage.accept(chunk)
                result = stage.finalize(source.completion())
                index = Path(self.store.index_path).read_bytes()
                revision = self.store.revision
                with mock.patch.object(stores, "_atomic_write_json", side_effect=OSError("disk full")):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        stage.publish(self.store, provisional)
                journals = resume.ResumeJournalStore(self.root / "journals")
                self.assertEqual(journals.load("incoming", stage.transfer_id).state, "finalizing")
                pending = Path(self.objects.pending_root) / stage.transfer_id
                target = Path(self.objects.object_path(result.files[0].sha256))
                manifest_path = Path(self.objects.manifest_path(
                    result.finalized_manifest["manifest_digest"]))
                self.assertEqual({path.name for path in pending.iterdir()},
                                 {"receipt.json", "manifest.json", result.files[0].sha256})
                self.assertEqual(self.objects.collect_garbage(), 0)
                stage.cancel()
                self.assertEqual(journals.load("incoming", stage.transfer_id).state, "cancelled")
                self.assertTrue(pending.is_dir())
                self.assertEqual(Path(result.files[0].path).read_bytes(), payload)
                stage.purge(object_store=self.store.object_store_v2)
                self.assertIsNone(stage.journal)
                self.assertFalse(Path(stage.stage_directory).exists())
                self.assertFalse(pending.exists())
                with self.assertRaises(resume.ResumeJournalError) as caught:
                    journals.load("incoming", stage.transfer_id)
                self.assertEqual(caught.exception.code, "not_found")
                self.assertEqual(list(Path(self.objects.pending_root).iterdir()), [])
                self.objects.collect_garbage()
                self.assertFalse(manifest_path.exists())
                self.assertEqual(Path(self.store.index_path).read_bytes(), index)
                self.assertEqual(self.store.revision, revision)
                self.assertEqual(self.store.get_item(provisional["item_id"]), provisional)
                if shared:
                    self.assertEqual(result.files[0].sha256, other_result.files[0].sha256)
                    self.assertEqual(target.read_bytes(), payload)
                    restarted = stores.ClipboardStore(str(self.root), "b")
                    self.assert_published(restarted, other_provisional, other_item,
                                          other_revision, payload)
                    self.assertEqual(list(Path(self.objects.objects_root).glob("*/*")), [target])
                    self.assertEqual(list(Path(self.objects.manifests_root).glob("*/*")),
                                     [Path(self.objects.manifest_path(
                                         other_item["batch_manifest"]["manifest_digest"]))])
                else:
                    self.assertFalse(target.exists())
                    self.assertEqual(list(Path(self.objects.objects_root).glob("*/*")), [])
                    self.assertEqual(list(Path(self.objects.manifests_root).glob("*/*")), [])
                self.assertEqual(self.objects.collect_garbage(), 0)

    def test_real_staging_commit_retry_and_restart_keep_one_row_and_revision(self):
        provisional, result = self.stage()
        revision = self.store.revision
        item, publication = self.commit(provisional, result)
        self.assertEqual(item["item_id"], provisional["item_id"])
        self.assertEqual(item["item_revision"], provisional["item_revision"] + 1)
        self.assertEqual(self.store.revision, revision + 1)
        self.assertEqual(len(self.store.list_items()), 1)
        self.assertEqual(self.store.current_item_id, item["item_id"])
        for _ in range(2):
            replay, evicted = self.store.commit_received_v2_item(provisional, publication)
            self.assertEqual(replay, item)
            self.assertEqual(evicted, [])
            self.assertEqual(self.store.revision, revision + 1)
        restarted = stores.ClipboardStore(str(self.root), "a")
        replay, _ = restarted.commit_received_v2_item(provisional, publication)
        self.assertEqual(replay, item)
        self.assertEqual(restarted.revision, revision + 1)
        self.assertTrue(restarted.verify_received_v2_publication(publication))

    def test_index_failure_rolls_back_and_pins_survive_gc_then_restart_retry(self):
        provisional, result = self.stage()
        publication = self.objects.publish_staged_transfer(result)
        before = self.store.index_path
        raw = Path(before).read_bytes()
        revision = self.store.revision
        with mock.patch.object(stores, "_atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.commit_received_v2_item(provisional, publication)
        self.assertEqual(Path(before).read_bytes(), raw)
        self.assertEqual(self.store.revision, revision)
        self.assertEqual(self.store.get_item(provisional["item_id"]), provisional)
        self.assertFalse(self.store.verify_received_v2_publication(publication))
        self.assertEqual(self.objects.collect_garbage(), 0)
        self.assertTrue(all(Path(f.path).exists() for f in result.files))
        restarted = stores.ClipboardStore(str(self.root), "a")
        item, _ = restarted.commit_received_v2_item(provisional, publication)
        self.assertEqual(item["payload_state"], "cached")
        self.assertTrue(restarted.verify_received_v2_publication(publication))
        self.assertFalse(Path(self.objects._pending(publication)).exists())

    def test_post_replace_failure_is_reconciled_without_second_revision(self):
        provisional, result = self.stage()
        publication = self.objects.publish_staged_transfer(result)
        original = stores._atomic_write_json

        def fail_after_replace(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("lost completion")

        revision = self.store.revision
        with mock.patch.object(stores, "_atomic_write_json", side_effect=fail_after_replace):
            with self.assertRaises(OSError):
                self.store.commit_received_v2_item(provisional, publication)
        self.assertEqual(self.store.revision, revision)
        self.assertTrue(self.store.verify_received_v2_publication(publication))
        self.store.commit_received_v2_item(provisional, publication)
        self.assertEqual(self.store.revision, revision + 1)
        self.assertEqual(len(self.store.list_items()), 1)

    def test_no_second_full_read_in_normal_publication_commit_and_availability(self):
        provisional, source, stage = self.prepare_stage()
        for chunk in source.iter_chunks():
            stage.accept(chunk)
        # POSIX needs a handoff rehash because it cannot exclude writers.
        # Windows finalization must rely on its deny-WRITE handle without rereading.
        if os.name != "nt":
            result = stage.finalize(source.completion())
        # New objects use receiver fingerprint evidence. Hashing the index and
        # canonical metadata is fine; hashing payload again is not.
        real_hash = hashlib.sha256
        payload = self.source.read_bytes()

        class GuardHash:
            def __init__(self, data=b""):
                self.inner = real_hash(data)
            def update(self, data):
                if data and data in payload:
                    raise AssertionError("full payload reread")
                self.inner.update(data)
            def digest(self):
                return self.inner.digest()
            def hexdigest(self):
                return self.inner.hexdigest()

        with mock.patch.object(objects.hashlib, "sha256", GuardHash):
            if os.name == "nt":
                result = stage.finalize(source.completion())
            item, publication = self.commit(provisional, result)
            self.assertTrue(self.objects.item_is_publishable(item))
            self.assertTrue(self.store.verify_received_v2_publication(publication))

    def test_same_bytes_cross_profile_dedup_and_gc_lifecycle(self):
        provisional, result = self.stage()
        first, pub1 = self.commit(provisional, result)
        second_store = stores.ClipboardStore(str(self.root), "b")
        second_provisional, second_result = self.stage(store=second_store)
        second, pub2 = self.commit(second_provisional, second_result, second_store)
        self.assertNotEqual(first["item_id"], second["item_id"])
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(pub1.object_hashes, pub2.object_hashes)
        target = Path(self.objects.object_path(pub1.object_hashes[0]))
        self.assertEqual(target.read_bytes(), self.source.read_bytes())
        self.assertEqual(len(list(Path(self.objects.objects_root).glob("*/*"))), 1)
        for evidence in (result, second_result):
            for file in evidence.files:
                os.unlink(file.path)  # Streaming may do this only after receipt.
        self.store.delete_item(first["item_id"])
        self.objects.collect_garbage()
        self.assertTrue(target.exists())
        self.assertTrue(second_store.verify_received_v2_publication(pub2))
        second_store.delete_item(second["item_id"])
        self.objects.collect_garbage()
        self.assertFalse(target.exists())
        self.assertFalse(Path(self.objects.manifest_path(pub2.manifest_digest)).exists())

    def test_uncommitted_dedup_is_pinned_when_other_profile_is_deleted(self):
        provisional, result = self.stage()
        first, pub1 = self.commit(provisional, result)
        other = stores.ClipboardStore(str(self.root), "b")
        p2, r2 = self.stage(store=other)
        pub2 = other.object_store_v2.publish_staged_transfer(r2)
        os.unlink(result.files[0].path)
        self.store.delete_item(first["item_id"])
        self.objects.collect_garbage()
        self.assertTrue(Path(self.objects.object_path(pub1.object_hashes[0])).exists())
        other.commit_received_v2_item(p2, pub2)
        self.assertTrue(other.verify_received_v2_publication(pub2))

    def test_recover_corrupt_existing_dedup_object_without_mutating_old_inode(self):
        provisional, result = self.stage()
        digest = result.files[0].sha256
        target = Path(self.objects.object_path(digest))
        target.parent.mkdir(parents=True)
        target.write_bytes(b"bad")
        retained = self.root / "old-corrupt-link"
        os.link(target, retained)
        self.commit(provisional, result)
        self.assertEqual(target.read_bytes(), self.source.read_bytes())
        self.assertEqual(retained.read_bytes(), b"bad")

    def test_tamper_and_object_loss_fail_availability_even_with_legacy_blob(self):
        provisional, result = self.stage()
        item, publication = self.commit(provisional, result)
        target = Path(self.objects.object_path(publication.object_hashes[0]))
        target.write_bytes(b"x" * target.stat().st_size)
        self.assertFalse(self.objects.item_is_publishable(item))
        self.assertFalse(self.store.verify_received_v2_publication(publication))
        target.unlink()
        Path(self.store.object_path(item["sha256"])).write_bytes(b"not a ZIP")
        self.assertFalse(self.store._item_payload_available_locked(item))
        self.assertIsNone(self.store.get_data(item["item_id"]))
        self.assertIsNone(self.store.get_object_path_for_item(item["item_id"]))
        restarted = stores.ClipboardStore(str(self.root), "a")
        self.assertEqual(restarted.get_item(item["item_id"])["payload_state"], "missing")

    def test_stage_requires_fingerprint_and_rejects_tamper_paths_duplicates_and_hashes(self):
        provisional, result = self.stage()
        original = result.files[0]
        for changes in ({"fingerprint": None}, {"path": str(self.source)},
                        {"sha256": "../bad"}, {"entry_index": True}, {"size": True}):
            candidate = replace(result, files=(replace(original, **changes),))
            with self.subTest(changes=changes), self.assertRaises(objects.ObjectStoreV2Error):
                self.objects.publish_staged_transfer(candidate)
        candidate = replace(result, files=(original, original))
        with self.assertRaises(objects.ObjectStoreV2Error):
            self.objects.publish_staged_transfer(candidate)
        Path(original.path).write_bytes(b"z" * original.size)
        with self.assertRaises(objects.ObjectStoreV2Error):
            self.objects.publish_staged_transfer(result)
        self.assertEqual(list(Path(self.objects.objects_root).glob("*/*")), [])

    def test_staging_replay_and_restart_rehashed_evidence(self):
        provisional, result = self.stage()
        publication = self.objects.publish_staged_transfer(result)
        self.assertEqual(self.objects.publish_staged_transfer(result), publication)
        restarted = objects.ClipboardObjectStoreV2(self.root)
        # Restart contract: reopen rehashes; stale stat-only evidence is never
        # refreshed automatically by the object store.
        refreshed = []
        for file in result.files:
            with open(file.path, "rb", buffering=0) as handle:
                self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), file.sha256)
                refreshed.append(replace(file, fingerprint=objects.staged_fingerprint(
                    os.fstat(handle.fileno()), handle)))
        result = replace(result, files=tuple(refreshed))
        self.assertEqual(restarted.publish_staged_transfer(result), publication)
        self.store.commit_received_v2_item(provisional, publication)
        self.assertTrue(self.store.verify_received_v2_publication(publication))

    def test_same_bytes_in_two_files_keeps_two_entries_one_object(self):
        twin = self.root / "twin.bin"
        twin.write_bytes(self.source.read_bytes())
        provisional, result = self.stage([self.source, twin])
        item, publication = self.commit(provisional, result)
        self.assertEqual(item["file_count"], 2)
        self.assertEqual(len(publication.object_hashes), 1)
        self.assertEqual(len(list(Path(self.objects.objects_root).glob("*/*"))), 1)

    def test_cross_process_lock_times_out_and_recovers(self):
        program = (
            "import sys; from clipboard_object_store_v2 import ClipboardObjectStoreV2; "
            "s=ClipboardObjectStoreV2(sys.argv[1], lock_timeout=0.1); "
            "\nwith s.locked(): pass\n")
        with self.objects.locked():
            start = time.monotonic()
            result = subprocess.run([sys.executable, "-c", program, str(self.root)],
                                    capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"object store lock failed", result.stderr)
            self.assertLess(time.monotonic() - start, 5)
        result = subprocess.run([sys.executable, "-c", program, str(self.root)],
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_stale_profile_writer_cannot_overwrite_receipt(self):
        provisional, result = self.stage()
        stale = stores.ClipboardStore(str(self.root), "a")
        item, publication = self.commit(provisional, result)
        with self.assertRaises(ValueError):
            stale.delete_item(item["item_id"])
        self.assertTrue(self.store.verify_received_v2_publication(publication))

    def test_publication_fsync_failure_retains_stage_and_allows_reopen_retry(self):
        provisional, result = self.stage()
        original = objects._fsync_directory
        target_dir = os.path.dirname(self.objects.object_path(result.files[0].sha256))

        def fail_publication_directory(path):
            if os.fspath(path) == target_dir:
                raise OSError("directory sync failed")
            original(path)

        with mock.patch.object(objects, "_fsync_directory", side_effect=fail_publication_directory):
            with self.assertRaises(OSError):
                self.objects.publish_staged_transfer(result)
        self.assertTrue(Path(result.files[0].path).exists())
        self.assertEqual(self.store.get_item(provisional["item_id"])["batch_manifest"],
                         provisional["batch_manifest"])
        refreshed = []
        for file in result.files:
            with open(file.path, "rb", buffering=0) as handle:
                self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), file.sha256)
                refreshed.append(replace(file, fingerprint=objects.staged_fingerprint(
                    os.fstat(handle.fileno()), handle)))
        result = replace(result, files=tuple(refreshed))
        self.commit(provisional, result)

    def test_no_replace_does_not_unlink_a_racing_destination(self):
        provisional, result = self.stage()
        target = self.objects.object_path(result.files[0].sha256)
        original = os.link

        def raced_link(source, destination, *args, **kwargs):
            if os.fspath(destination) == target:
                Path(target).write_bytes(b"other writer")
                raise FileExistsError("destination won race")
            return original(source, destination, *args, **kwargs)

        with mock.patch.object(objects.os, "link", side_effect=raced_link):
            with self.assertRaises(FileExistsError):
                self.objects.publish_staged_transfer(result)
        self.assertEqual(Path(target).read_bytes(), b"other writer")
        self.assertTrue(Path(result.files[0].path).exists())

    def test_cross_volume_failure_never_falls_back_to_copy(self):
        import errno
        provisional, result = self.stage()
        target = self.objects.object_path(result.files[0].sha256)
        original = os.link

        def cross_volume(source, destination, *args, **kwargs):
            if os.fspath(destination) == target:
                raise OSError(errno.EXDEV, "cross volume")
            return original(source, destination, *args, **kwargs)

        with mock.patch.object(objects.os, "link", side_effect=cross_volume):
            with self.assertRaises(OSError):
                self.objects.publish_staged_transfer(result)
        self.assertFalse(Path(target).exists())
        self.assertEqual(Path(result.files[0].path).read_bytes(), self.source.read_bytes())

    def test_corrupt_manifest_is_repaired_and_pending_binding_is_not(self):
        provisional, result = self.stage()
        target = Path(self.objects.manifest_path(result.finalized_manifest["manifest_digest"]))
        target.parent.mkdir(parents=True)
        target.write_bytes(b"corrupt")
        publication = self.objects.publish_staged_transfer(result)
        self.assertEqual(target.read_bytes(), manifests.canonical_manifest_bytes(publication.manifest))
        Path(self.objects._pending(publication), "receipt.json").write_bytes(b"conflict")
        with self.assertRaises(objects.ObjectStoreV2Error):
            self.objects.publish_staged_transfer(result)
        with self.assertRaises(ValueError):
            self.store.commit_received_v2_item(provisional, publication)

    def test_failed_post_commit_pin_cleanup_does_not_rollback_index(self):
        provisional, result = self.stage()
        publication = self.objects.publish_staged_transfer(result)
        revision = self.store.revision
        with mock.patch.object(self.objects, "_release_committed_pins", side_effect=OSError("busy")):
            with self.assertRaises(OSError):
                self.store.commit_received_v2_item(provisional, publication)
        self.assertEqual(self.store.revision, revision + 1)
        self.assertTrue(self.store.verify_received_v2_publication(publication))
        self.store.commit_received_v2_item(provisional, publication)
        self.assertEqual(self.store.revision, revision + 1)
        self.assertFalse(Path(self.objects._pending(publication)).exists())

    def test_ancestor_reparse_check_without_windows_symlink_privilege(self):
        provisional, result = self.stage()
        actual = objects.clipboard_paths._is_reparse_point
        stage_dir = os.path.dirname(result.files[0].path)

        def unsafe(path, info):
            return os.fspath(path) == stage_dir or actual(path, info)

        with mock.patch.object(objects.clipboard_paths, "_is_reparse_point", side_effect=unsafe):
            with self.assertRaises(objects.ObjectStoreV2Error):
                self.objects.publish_staged_transfer(result)
        self.assertEqual(list(Path(self.objects.objects_root).glob("*/*")), [])

    def test_evicted_commit_rolls_back_without_receipt(self):
        provisional, result = self.stage()
        publication = self.objects.publish_staged_transfer(result)
        self.store.set_current(None)
        revision = self.store.revision
        with self.assertRaises(ValueError):
            self.store.commit_received_v2_item(provisional, publication, enforce=(0, 0))
        self.assertEqual(self.store.revision, revision)
        self.assertEqual(len(self.store.list_items()), 1)
        self.assertFalse(self.store.verify_received_v2_publication(publication))

    def test_reparse_ancestor_and_object_symlink_fail_closed(self):
        provisional, result = self.stage()
        target = Path(self.objects.object_path(result.files[0].sha256))
        target.parent.mkdir(parents=True)
        try:
            target.symlink_to(self.source)
        except OSError as exc:
            self.skipTest(f"symlink privilege unavailable: {exc}")
        with self.assertRaises(objects.ObjectStoreV2Error):
            self.objects.publish_staged_transfer(result)
        self.assertEqual(self.source.read_bytes(), b"identical payload" * 100)
        link = self.root / "redirect"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(objects.ObjectStoreV2Error):
            objects.ClipboardObjectStoreV2(link / "new-store")
        self.assertFalse((self.root / "new-store").exists())

    def test_binding_and_actual_receipt_required_not_arbitrary_item_dict(self):
        provisional, result = self.stage()
        publication = self.objects.publish_staged_transfer(result)
        fake_item = model.finalize_file_item_v2(provisional, publication.manifest)
        self.assertTrue(self.objects.item_is_publishable(fake_item))
        self.assertFalse(self.store.verify_received_v2_publication(publication))
        changed = objects.PublishedManifest(uuid.uuid4().hex,
            publication.provisional_manifest_digest, publication.manifest_digest,
            publication.manifest, publication.object_hashes)
        with self.assertRaises(ValueError):
            self.store.commit_received_v2_item(provisional, changed)
        changed = objects.PublishedManifest(publication.transfer_id, "f" * 64,
            publication.manifest_digest, publication.manifest, publication.object_hashes)
        with self.assertRaises(ValueError):
            self.store.commit_received_v2_item(provisional, changed)
        self.store.commit_received_v2_item(provisional, publication)
        document = json.loads(Path(self.store.index_path).read_text())
        document.pop("v2_receipts")
        Path(self.store.index_path).write_text(json.dumps(document))
        self.assertFalse(self.store.verify_received_v2_publication(publication))

    def test_directory_only_and_empty_files(self):
        directory = self.root / "empty-folder"
        directory.mkdir()
        for paths in ([directory], [self.source]):
            self.source.write_bytes(b"")
            provisional, result = self.stage(paths)
            item, publication = self.commit(provisional, result)
            self.assertEqual(item["batch_manifest"], manifests.finalize_manifest(
                provisional["batch_manifest"], {f.entry_index: f.sha256 for f in result.files}))
            self.assertEqual(item["size"], 0)
            self.assertTrue(self.store.verify_received_v2_publication(publication))

    def test_model_rejects_structural_or_fingerprint_changes(self):
        provisional, result = self.stage()
        for key, value in (("path", "other.bin"), ("mtime_ns", 1),
                           ("source_fingerprint", {"different": True})):
            entries = copy.deepcopy(result.finalized_manifest["entries"])
            entries[0][key] = value
            for entry in entries:
                entry.pop("index")
            changed = manifests.build_manifest(provisional["item_id"],
                result.finalized_manifest["item_revision"], entries)
            with self.subTest(key=key), self.assertRaises(ValueError):
                model.finalize_file_item_v2(provisional, changed)

    def test_schema_one_never_advertises_v2_as_zip_or_legacy_data(self):
        provisional, result = self.stage()
        item, publication = self.commit(provisional, result)
        with self.assertRaises(ValueError):
            model.manifest_item(item)
        public = self.store.build_manifest("local")
        self.assertEqual(public["items"], [])
        self.assertIsNone(public["current_item_id"])
        forged = copy.deepcopy(item)
        forged["schema_version"] = 1
        with self.assertRaises(ValueError):
            model.version_item(forged)
        with self.assertRaises(ValueError):
            self.store.add_item(item, replace_existing=True)
        self.assertTrue(self.store.verify_received_v2_publication(publication))
        self.assertEqual(json.loads(Path(self.store.index_path).read_text())["schema_version"], 3)

    def test_legacy_init_remains_lazy_and_no_schema_migration(self):
        root = self.root / "legacy"
        store = stores.ClipboardStore(str(root), "p")
        self.assertIsNone(store._object_store_v2)
        item, _ = store.add_item(model.make_text_item("hello", 1), b"hello")
        reopened = stores.ClipboardStore(str(root), "p")
        self.assertIsNone(reopened._object_store_v2)
        self.assertFalse((root / "objects").exists())
        self.assertEqual(reopened.get_data(item["item_id"]), b"hello")
        self.assertEqual(json.loads(Path(store.index_path).read_text())["schema_version"], 2)

    def test_open_handle_fingerprint_matches_path(self):
        _, result = self.stage()
        path = result.files[0].path
        with open(path, "rb", buffering=0) as handle:
            self.assertEqual(objects.staged_fingerprint(objects._regular(path)),
                             objects.staged_fingerprint(os.fstat(handle.fileno()), handle))

    def test_gc_refuses_corrupt_profile_and_preserves_unreferenced_objects(self):
        provisional, result = self.stage()
        item, publication = self.commit(provisional, result)
        os.unlink(result.files[0].path)
        self.store.delete_item(item["item_id"])
        Path(self.store.index_path).write_text("{bad")
        with self.assertRaises(ValueError):
            self.objects.collect_garbage()
        self.assertTrue(Path(self.objects.object_path(publication.object_hashes[0])).exists())


if __name__ == "__main__":
    unittest.main()
