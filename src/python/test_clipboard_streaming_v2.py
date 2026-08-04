"""Focused end-state tests for direct file streaming and receiver staging V2."""
from __future__ import annotations

import builtins
import errno
import hashlib
import os
import tempfile
import unittest
import uuid
from unittest import mock

import clipboard_files
import clipboard_manifest_v2 as manifest_v2
import clipboard_resume_v2 as resume
import clipboard_streaming_v2 as streaming


def transfer_id():
    return uuid.uuid4().hex


def captured_manifest(paths, revision=3):
    scan = clipboard_files.scan_paths(paths)
    manifest = manifest_v2.build_manifest(
        transfer_id(), revision,
        [clipboard_files._manifest_entry(entry) for entry in scan["entries"]])
    return scan["entries"], manifest


def stream_to_stage(source, stage):
    receipts = []
    for chunk in source.iter_chunks():
        receipts.append(stage.accept(chunk))
    return receipts, stage.finalize(source.completion())


def resumable_stage(root, identifier, manifest, *, policy=None):
    journal_store = resume.ResumeJournalStore(os.path.join(root, "journals"))
    ids = {"peer_id": transfer_id(), "profile_id": transfer_id(),
           "provider_id": transfer_id()}
    stage = streaming.IncomingTransferStage.create(
        os.path.join(root, "incoming"), identifier, manifest,
        journal_store=journal_store, checkpoint_policy=policy, **ids)
    return stage, journal_store, ids


def outgoing_for_plan(store, incoming, manifest):
    outgoing = store.create_outgoing(
        transfer_id=incoming.transfer_id, peer_id=incoming.peer_id,
        profile_id=incoming.profile_id, provider_id=incoming.provider_id,
        manifest=manifest)
    for entry in incoming.entries:
        if entry["type"] == "file" and entry["durable_offset"]:
            outgoing = store.commit(resume.update_outgoing_progress(
                outgoing, entry["index"],
                receiver_verified_offset=entry["durable_offset"],
                receiver_durable_offset=entry["durable_offset"],
                state="transferring"), outgoing.generation)
    return outgoing


class DirectStreamingRoundTripTests(unittest.TestCase):
    def test_streams_multiple_files_empty_files_and_directories_by_index(self):
        with tempfile.TemporaryDirectory() as root:
            source_root = os.path.join(root, "Quellordner")
            os.mkdir(source_root)
            nested = os.path.join(source_root, "Gr\u00f6sse")
            os.mkdir(nested)
            first_path = os.path.join(source_root, "a.bin")
            second_path = os.path.join(nested, "b.txt")
            empty_path = os.path.join(nested, "empty")
            with open(first_path, "wb") as handle:
                handle.write(b"abcdefgh")
            with open(second_path, "wb") as handle:
                handle.write(b"12345")
            open(empty_path, "wb").close()
            entries, manifest = captured_manifest([source_root])
            identifier = transfer_id()
            source = streaming.SequentialFileStream(
                identifier, manifest, entries, chunk_size=3)
            stage = streaming.IncomingTransferStage(
                os.path.join(root, "incoming"), identifier, manifest)

            receipts, result = stream_to_stage(source, stage)

            offsets_by_entry = {}
            for receipt in receipts:
                offsets_by_entry.setdefault(receipt.entry_index, []).append(receipt.offset)
            self.assertEqual(sorted(offsets_by_entry.values()), [[0, 3], [0, 3, 6]])
            self.assertTrue(all(receipt.verified_offset == receipt.offset + receipt.length
                                for receipt in receipts))
            self.assertEqual(result.finalized_manifest["item_revision"], 4)
            files_by_index = {entry["index"]: entry for entry in manifest["entries"]
                              if entry["type"] == "file"}
            source_by_path = {entry["rel"]: entry for entry in entries
                              if entry["type"] == "file"}
            self.assertEqual({file.entry_index for file in result.files}, set(files_by_index))
            for staged in result.files:
                with open(staged.path, "rb") as handle:
                    payload = handle.read()
                expected_entry = files_by_index[staged.entry_index]
                with open(source_by_path[expected_entry["path"]]["abspath"], "rb") as handle:
                    expected_payload = handle.read()
                self.assertEqual(payload, expected_payload)
                self.assertEqual(hashlib.sha256(payload).hexdigest(), staged.sha256)
                self.assertTrue(staged.path.endswith(f"{staged.entry_index}.verified"))
            self.assertFalse(any(name.endswith(".part")
                                 for name in os.listdir(stage.stage_directory)))
            self.assertFalse(any(entry["type"] == "directory"
                                 and os.path.exists(os.path.join(
                                     stage.stage_directory, f'{entry["index"]}.verified'))
                                 for entry in manifest["entries"]))

    def test_directory_only_manifest_completes_without_payload_files(self):
        with tempfile.TemporaryDirectory() as root:
            source_root = os.path.join(root, "empty-tree")
            os.mkdir(source_root)
            os.mkdir(os.path.join(source_root, "child"))
            entries, manifest = captured_manifest([source_root])
            identifier = transfer_id()
            source = streaming.SequentialFileStream(identifier, manifest, entries)
            stage = streaming.IncomingTransferStage(
                os.path.join(root, "incoming"), identifier, manifest)

            self.assertEqual(list(source.iter_chunks()), [])
            result = stage.finalize(source.completion())

            self.assertEqual(result.files, ())
            self.assertEqual(os.listdir(stage.stage_directory), [])
            self.assertEqual(result.finalized_manifest["item_revision"], 3)

    def test_queue_chunk_is_immutable_and_checksum_verified(self):
        payload = bytearray(b"abc")
        chunk = streaming.PayloadChunk(
            transfer_id(), 0, 0, payload, hashlib.sha256(payload).digest())
        payload[:] = b"zzz"
        frozen = streaming.freeze_payload_chunk(chunk)
        self.assertEqual(frozen.payload, b"abc")
        self.assertEqual(streaming.payload_chunk_size(frozen), 3)
        with self.assertRaisesRegex(streaming.StreamV2Error, "checksum"):
            streaming.PayloadChunk(transfer_id(), 0, 0, b"abc", b"x" * 32)

    def test_completion_evidence_is_strict_and_detached(self):
        identifier = transfer_id()
        digest = "a" * 64
        hashes = {0: "b" * 64}
        fingerprints = {0: {"version": 1, "nested": {"value": 1}}}
        completion = streaming.SourceStreamCompletion(
            identifier, digest, 1, hashes, fingerprints)
        hashes[0] = "c" * 64
        fingerprints[0]["version"] = 2
        self.assertEqual(completion.file_hashes[0], "b" * 64)
        self.assertEqual(completion.source_fingerprints[0]["version"], 1)
        for invalid_total in (True, -1, streaming.MAX_LOGICAL_OFFSET + 1):
            with self.subTest(total=invalid_total), self.assertRaises(streaming.StreamV2Error):
                streaming.SourceStreamCompletion(
                    identifier, digest, invalid_total, hashes, fingerprints)
        with self.assertRaises(streaming.StreamV2Error):
            streaming.SourceStreamCompletion(
                identifier, digest, 1, {True: "b" * 64}, fingerprints)

    def test_completion_requires_exhaustion_and_stream_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "value")
            with open(path, "wb") as handle:
                handle.write(b"data")
            entries, manifest = captured_manifest([path])
            source = streaming.SequentialFileStream(transfer_id(), manifest, entries)
            with self.assertRaisesRegex(streaming.StreamV2Error, "not complete"):
                source.completion()
            self.assertEqual(len(list(source.iter_chunks())), 1)
            self.assertEqual(source.completion().total_bytes, 4)
            with self.assertRaisesRegex(streaming.StreamV2Error, "not reusable"):
                list(source.iter_chunks())


class SourceValidationTests(unittest.TestCase):
    def test_source_change_while_generator_is_suspended_invalidates_completion(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "value")
            with open(path, "wb") as handle:
                handle.write(b"abcdef")
            entries, manifest = captured_manifest([path])
            source = streaming.SequentialFileStream(
                transfer_id(), manifest, entries, chunk_size=3)
            iterator = source.iter_chunks()
            self.assertEqual(next(iterator).payload, b"abc")
            with open(path, "r+b") as handle:
                handle.seek(3)
                handle.write(b"XYZ")
                handle.flush()
                os.fsync(handle.fileno())
            os.utime(path, ns=(os.stat(path).st_atime_ns, os.stat(path).st_mtime_ns + 1_000_000))
            with self.assertRaisesRegex(streaming.StreamV2Error, "changed"):
                list(iterator)
            with self.assertRaisesRegex(streaming.StreamV2Error, "not complete"):
                source.completion()

    def test_replaced_source_is_rejected_before_streaming(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "value")
            with open(path, "wb") as handle:
                handle.write(b"old")
            entries, manifest = captured_manifest([path])
            os.unlink(path)
            with open(path, "wb") as handle:
                handle.write(b"new")
            with self.assertRaisesRegex(streaming.StreamV2Error, "changed"):
                streaming.SequentialFileStream(transfer_id(), manifest, entries)

    def test_hash_failure_is_path_free_and_terminal(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "private-name")
            with open(path, "wb") as handle:
                handle.write(b"data")
            entries, manifest = captured_manifest([path])
            source = streaming.SequentialFileStream(
                transfer_id(), manifest, entries,
                hash_factory=mock.Mock(side_effect=RuntimeError("boom")))
            with self.assertRaises(streaming.StreamV2Error) as caught:
                list(source.iter_chunks())
            self.assertEqual(caught.exception.code, "source_hash_failed")
            self.assertNotIn(path, str(caught.exception))

    def test_source_read_failure_is_mapped_and_prevents_completion(self):
        class ReadProxy:
            def __init__(self, handle):
                self.handle = handle

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def read(self, _amount):
                raise OSError(errno.EIO, "read")

        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "source")
            with open(path, "wb") as handle:
                handle.write(b"data")
            entries, manifest = captured_manifest([path])
            source = streaming.SequentialFileStream(transfer_id(), manifest, entries)
            real_open = builtins.open

            def failing_open(candidate, mode="r", *args, **kwargs):
                handle = real_open(candidate, mode, *args, **kwargs)
                return ReadProxy(handle) if mode == "rb" else handle

            with mock.patch.object(streaming, "open", side_effect=failing_open, create=True):
                with self.assertRaises(streaming.StreamV2Error) as caught:
                    list(source.iter_chunks())
            self.assertEqual(caught.exception.code, "source_read_failed")
            with self.assertRaises(streaming.StreamV2Error):
                source.completion()


class ResumeLifecycleTests(unittest.TestCase):
    def test_terminal_journals_cannot_reopen(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "value")
            with open(path, "wb") as handle:
                handle.write(b"abc")
            _entries, manifest = captured_manifest([path])
            identifier = transfer_id()
            stage, store, ids = resumable_stage(root, identifier, manifest)
            stage.cancel()
            with self.assertRaises(streaming.StreamV2Error) as caught:
                streaming.IncomingTransferStage.reopen(
                    os.path.join(root, "incoming"), identifier, manifest,
                    journal_store=store, **ids)
            self.assertEqual(caught.exception.code, "journal_load_failed")

    def test_generation_zero_recreates_stage_and_advances_zero_file(self):
        with tempfile.TemporaryDirectory() as root:
            source_root = os.path.join(root, "sources")
            os.mkdir(source_root)
            zero = os.path.join(source_root, "a-zero")
            payload = os.path.join(source_root, "z-data")
            open(zero, "wb").close()
            with open(payload, "wb") as handle:
                handle.write(b"x")
            entries, manifest = captured_manifest([zero, payload])
            identifier = transfer_id()
            store = resume.ResumeJournalStore(os.path.join(root, "journals"))
            ids = {"peer_id": "peer", "profile_id": "profile", "provider_id": "provider"}
            store.create_incoming(transfer_id=identifier, manifest=manifest, **ids)
            incoming = os.path.join(root, "incoming")
            stage = streaming.IncomingTransferStage.reopen(
                incoming, identifier, manifest, journal_store=store, **ids)
            source = streaming.SequentialFileStream(
                identifier, manifest, entries, chunk_size=1)
            chunks = list(source.iter_chunks())
            self.assertEqual(len(chunks), 1)
            stage.accept(chunks[0])
            result = stage.finalize(source.completion())
            self.assertEqual(store.load("incoming", identifier).state, "completed")
            self.assertEqual([file.size for file in result.files], [0, 1])

    def make_source(self, root, payload=b"0123456789" * 10):
        path = os.path.join(root, "private-source")
        with open(path, "wb") as handle:
            handle.write(payload)
        entries, manifest = captured_manifest([path])
        return path, entries, manifest

    def test_sender_and_receiver_restart_from_prefix_and_finish_exact_bytes(self):
        for checkpoint_chunks in (1, 50):
            with self.subTest(checkpoint_chunks=checkpoint_chunks), \
                    tempfile.TemporaryDirectory() as root:
                payload = b"0123456789" * 100
                _path, entries, manifest = self.make_source(root, payload)
                identifier = transfer_id()
                source = streaming.SequentialFileStream(
                    identifier, manifest, entries, chunk_size=10)
                stage, store, ids = resumable_stage(
                    root, identifier, manifest,
                    policy=resume.CheckpointPolicy(byte_interval=10, time_interval=10))
                iterator = source.iter_chunks()
                for _ in range(checkpoint_chunks):
                    receipt = stage.accept(next(iterator))
                self.assertTrue(receipt.checkpointed)
                durable = checkpoint_chunks * 10
                iterator.close()
                source.close()
                stage.pause(disconnected=True)
                incoming = store.load("incoming", identifier)
                outgoing = outgoing_for_plan(store, incoming, manifest)
                plan = resume.validate_resume_pair(outgoing, incoming, manifest)

                reopened = streaming.IncomingTransferStage.reopen(
                    os.path.join(root, "incoming"), identifier, manifest,
                    journal_store=store, **ids)
                restarted = streaming.SequentialFileStream(
                    identifier, manifest, entries, chunk_size=10, resume_plan=plan)
                receipts, result = stream_to_stage(restarted, reopened)

                self.assertEqual(receipts[0].offset if receipts else len(payload), durable)
                self.assertEqual(restarted.resume_prefix_bytes_read, durable)
                self.assertEqual(restarted.payload_bytes_read, len(payload) - durable)
                self.assertEqual(restarted.payload_bytes_emitted, len(payload) - durable)
                self.assertEqual(restarted.completion().total_bytes, len(payload))
                with open(result.files[0].path, "rb") as handle:
                    self.assertEqual(handle.read(), payload)
                self.assertEqual(result.files[0].sha256, hashlib.sha256(payload).hexdigest())

    def test_fully_durable_file_is_rehashed_without_retransmission(self):
        with tempfile.TemporaryDirectory() as root:
            payload = b"complete"
            _path, entries, manifest = self.make_source(root, payload)
            identifier = transfer_id()
            first = streaming.SequentialFileStream(identifier, manifest, entries)
            stage, store, ids = resumable_stage(root, identifier, manifest)
            chunks = list(first.iter_chunks())
            receipt = stage.accept(chunks[0])
            self.assertTrue(receipt.file_complete)
            stage.pause()
            incoming = store.load("incoming", identifier)
            plan = resume.validate_resume_pair(
                outgoing_for_plan(store, incoming, manifest), incoming, manifest)

            restarted = streaming.SequentialFileStream(
                identifier, manifest, entries, resume_plan=plan)
            self.assertEqual(list(restarted.iter_chunks()), [])
            self.assertEqual(restarted.resume_prefix_bytes_read, len(payload))
            self.assertEqual(restarted.payload_bytes_emitted, 0)
            reopened = streaming.IncomingTransferStage.reopen(
                os.path.join(root, "incoming"), identifier, manifest,
                journal_store=store, **ids)
            result = reopened.finalize(restarted.completion())
            with open(result.files[0].path, "rb") as handle:
                self.assertEqual(handle.read(), payload)

    def test_reopen_truncates_tail_and_rejects_short_or_corrupt_prefix(self):
        for damage, code in (("tail", None), ("short", "corrupt_partial"),
                             ("corrupt", "resume_prefix_mismatch")):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as root:
                _path, entries, manifest = self.make_source(root, b"abcdefghij")
                identifier = transfer_id()
                source = streaming.SequentialFileStream(
                    identifier, manifest, entries, chunk_size=5)
                stage, store, ids = resumable_stage(
                    root, identifier, manifest,
                    policy=resume.CheckpointPolicy(byte_interval=5, time_interval=10))
                stage.accept(next(source.iter_chunks()))
                stage.pause()
                part = os.path.join(stage.stage_directory, "0.part")
                if damage == "tail":
                    with open(part, "ab") as handle:
                        handle.write(b"TAIL")
                elif damage == "short":
                    with open(part, "r+b") as handle:
                        handle.truncate(4)
                else:
                    with open(part, "r+b") as handle:
                        handle.write(b"X")
                if code is None:
                    reopened = streaming.IncomingTransferStage.reopen(
                        os.path.join(root, "incoming"), identifier, manifest,
                        journal_store=store, **ids)
                    self.assertEqual(os.path.getsize(part), 5)
                    reopened.pause()
                else:
                    with self.assertRaises(streaming.StreamV2Error) as caught:
                        streaming.IncomingTransferStage.reopen(
                            os.path.join(root, "incoming"), identifier, manifest,
                            journal_store=store, **ids)
                    self.assertEqual(caught.exception.code, code)
                    self.assertNotIn(root, str(caught.exception))

    def test_source_prefix_change_is_terminal_path_free(self):
        with tempfile.TemporaryDirectory() as root:
            path, entries, manifest = self.make_source(root, b"abcdefghij")
            identifier = transfer_id()
            source = streaming.SequentialFileStream(identifier, manifest, entries, chunk_size=5)
            stage, store, _ids = resumable_stage(
                root, identifier, manifest,
                policy=resume.CheckpointPolicy(byte_interval=5, time_interval=10))
            stage.accept(next(source.iter_chunks()))
            stage.pause()
            incoming = store.load("incoming", identifier)
            plan = resume.validate_resume_pair(
                outgoing_for_plan(store, incoming, manifest), incoming, manifest)
            with open(path, "r+b") as handle:
                handle.write(b"X")
            changed_entries = [dict(entry) for entry in entries]
            with self.assertRaises(streaming.StreamV2Error) as caught:
                restarted = streaming.SequentialFileStream(
                    identifier, manifest, changed_entries, chunk_size=5, resume_plan=plan)
                list(restarted.iter_chunks())
            self.assertIn(caught.exception.code, {"source_changed", "resume_prefix_mismatch"})
            self.assertNotIn(path, str(caught.exception))

    def test_byte_time_file_checkpoints_and_no_per_small_chunk_fsync(self):
        with tempfile.TemporaryDirectory() as root:
            _path, entries, manifest = self.make_source(root, b"abcdef")
            identifier = transfer_id()
            source = streaming.SequentialFileStream(identifier, manifest, entries, chunk_size=2)
            chunks = list(source.iter_chunks())
            stage, store, _ids = resumable_stage(
                root, identifier, manifest,
                policy=resume.CheckpointPolicy(byte_interval=4, time_interval=1))
            with mock.patch.object(streaming.os, "fsync", wraps=os.fsync) as fsync:
                first = stage.accept(chunks[0], now=0)
                self.assertEqual(fsync.call_count, 0)
                second = stage.accept(chunks[1], now=.5)
                self.assertEqual(fsync.call_count, 2)
                third = stage.accept(chunks[2], now=.6)
            self.assertFalse(first.checkpointed)
            self.assertTrue(second.checkpointed)
            self.assertTrue(third.checkpointed)
            self.assertEqual(fsync.call_count, 4)
            self.assertEqual(store.load("incoming", identifier).entries[0]["durable_offset"], 6)

        with tempfile.TemporaryDirectory() as root:
            _path, entries, manifest = self.make_source(root, b"abcd")
            identifier = transfer_id()
            chunks = list(streaming.SequentialFileStream(
                identifier, manifest, entries, chunk_size=2).iter_chunks())
            stage, _store, _ids = resumable_stage(
                root, identifier, manifest,
                policy=resume.CheckpointPolicy(byte_interval=100, time_interval=1))
            self.assertFalse(stage.accept(chunks[0], now=0).checkpointed)
            self.assertTrue(stage.accept(chunks[1], now=2).checkpointed)

    def test_pause_cancel_preserve_and_purge_isolated(self):
        with tempfile.TemporaryDirectory() as root:
            _path, entries, manifest = self.make_source(root, b"abcdef")
            identifier = transfer_id()
            chunk = next(streaming.SequentialFileStream(
                identifier, manifest, entries, chunk_size=3).iter_chunks())
            stage, store, _ids = resumable_stage(root, identifier, manifest)
            stage.accept(chunk)
            stage.pause(disconnected=True)
            self.assertEqual(store.load("incoming", identifier).state, "waiting_reconnect")
            self.assertTrue(os.path.isdir(stage.stage_directory))
            stage = streaming.IncomingTransferStage.reopen(
                os.path.join(root, "incoming"), identifier, manifest,
                journal_store=store, peer_id=stage.journal.peer_id,
                profile_id=stage.journal.profile_id, provider_id=stage.journal.provider_id)
            stage.cancel()
            self.assertEqual(store.load("incoming", identifier).state, "cancelled")
            unrelated = os.path.join(root, "incoming", "keep")
            with open(unrelated, "wb") as handle:
                handle.write(b"keep")
            stage.purge()
            self.assertFalse(os.path.exists(stage.stage_directory))
            with open(unrelated, "rb") as handle:
                self.assertEqual(handle.read(), b"keep")
            with self.assertRaises(resume.ResumeJournalError):
                store.load("incoming", identifier)

    def test_reopen_reconciles_rename_before_journal_commit(self):
        with tempfile.TemporaryDirectory() as root:
            payload = b"abc"
            _path, entries, manifest = self.make_source(root, payload)
            identifier = transfer_id()
            source = streaming.SequentialFileStream(identifier, manifest, entries)
            stage, store, ids = resumable_stage(root, identifier, manifest)
            chunks = list(source.iter_chunks())
            stage.accept(chunks[0])
            real_commit = store.commit
            calls = 0

            def fail_verified(candidate, generation):
                nonlocal calls
                calls += 1
                if candidate.entries[0]["storage_state"] == "verified":
                    raise resume.ResumeJournalError("store_io", "commit")
                return real_commit(candidate, generation)

            with mock.patch.object(store, "commit", side_effect=fail_verified):
                with self.assertRaises(streaming.StreamV2Error) as caught:
                    stage.finalize(source.completion())
            self.assertEqual(caught.exception.code, "journal_commit_failed")
            self.assertTrue(os.path.exists(os.path.join(stage.stage_directory, "0.verified")))
            reopened = streaming.IncomingTransferStage.reopen(
                os.path.join(root, "incoming"), identifier, manifest,
                journal_store=store, **ids)
            self.assertEqual(reopened.journal.entries[0]["storage_state"], "verified")

    def test_reopen_rejects_peer_manifest_and_unexpected_stage_files_without_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            _path, entries, manifest = self.make_source(root, b"abc")
            identifier = transfer_id()
            stage, store, ids = resumable_stage(root, identifier, manifest)
            with self.assertRaises(streaming.StreamV2Error) as caught:
                streaming.IncomingTransferStage.reopen(
                    os.path.join(root, "incoming"), identifier, manifest,
                    journal_store=store, peer_id=transfer_id(),
                    profile_id=ids["profile_id"], provider_id=ids["provider_id"])
            self.assertEqual(caught.exception.code, "resume_mismatch")
            self.assertTrue(os.path.isdir(stage.stage_directory))

            other_manifest = manifest_v2.build_manifest(
                transfer_id(), manifest["item_revision"],
                [clipboard_files._manifest_entry(entry) for entry in entries])
            with self.assertRaises(streaming.StreamV2Error) as caught:
                streaming.IncomingTransferStage.reopen(
                    os.path.join(root, "incoming"), identifier, other_manifest,
                    journal_store=store, **ids)
            self.assertEqual(caught.exception.code, "resume_mismatch")

            unexpected = os.path.join(stage.stage_directory, "private.txt")
            with open(unexpected, "wb") as handle:
                handle.write(b"x")
            with self.assertRaises(streaming.StreamV2Error) as caught:
                streaming.IncomingTransferStage.reopen(
                    os.path.join(root, "incoming"), identifier, manifest,
                    journal_store=store, **ids)
            self.assertEqual(caught.exception.code, "corrupt_partial")
            self.assertTrue(os.path.exists(unexpected))
            self.assertIsNotNone(store.load("incoming", identifier))


class ReceiverFailureTests(unittest.TestCase):
    def make_transfer(self, root, payload=b"abcdef"):
        path = os.path.join(root, "source")
        with open(path, "wb") as handle:
            handle.write(payload)
        entries, manifest = captured_manifest([path])
        identifier = transfer_id()
        source = streaming.SequentialFileStream(
            identifier, manifest, entries, chunk_size=3)
        chunks = list(source.iter_chunks())
        stage = streaming.IncomingTransferStage(
            os.path.join(root, "incoming"), identifier, manifest)
        return source, chunks, stage

    def assert_terminal_cleanup(self, stage):
        self.assertFalse(os.path.exists(stage.stage_directory))

    def test_wrong_offset_or_transfer_id_rejects_and_removes_partials(self):
        for mutate in (
                lambda chunk: streaming.PayloadChunk(
                    chunk.transfer_id, chunk.entry_index, 1, chunk.payload, chunk.checksum),
                lambda chunk: streaming.PayloadChunk(
                    transfer_id(), chunk.entry_index, chunk.offset,
                    chunk.payload, chunk.checksum)):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as root:
                _source, chunks, stage = self.make_transfer(root)
                with self.assertRaises(streaming.StreamV2Error) as caught:
                    stage.accept(mutate(chunks[0]))
                self.assertEqual(caught.exception.code, "invalid_chunk")
                self.assert_terminal_cleanup(stage)

    def test_hash_mismatch_never_exposes_verified_files(self):
        with tempfile.TemporaryDirectory() as root:
            source, chunks, stage = self.make_transfer(root)
            for chunk in chunks:
                stage.accept(chunk)
            completion = source.completion()
            wrong = streaming.SourceStreamCompletion(
                completion.transfer_id, completion.manifest_digest,
                completion.total_bytes,
                {index: "0" * 64 for index in completion.file_hashes},
                completion.source_fingerprints)
            with self.assertRaises(streaming.StreamV2Error) as caught:
                stage.finalize(wrong)
            self.assertEqual(caught.exception.code, "hash_mismatch")
            self.assert_terminal_cleanup(stage)

    def test_short_write_disk_full_flush_and_rename_failures_cleanup(self):
        class WriteProxy:
            def __init__(self, handle, failure):
                self.handle = handle
                self.failure = failure

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def write(self, payload):
                if self.failure == "short":
                    self.handle.write(payload[:-1])
                    return len(payload) - 1
                if self.failure == "disk":
                    raise OSError(errno.ENOSPC, "full")
                return self.handle.write(payload)

            def flush(self):
                if self.failure == "flush":
                    raise OSError(errno.EIO, "flush")
                return self.handle.flush()

        for failure, expected_code in (
                ("short", "short_write"), ("disk", "disk_full"),
                ("flush", "target_flush_failed")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                _source, chunks, stage = self.make_transfer(root, b"abc")
                real_open = builtins.open

                def failing_open(path, mode="r", *args, **kwargs):
                    handle = real_open(path, mode, *args, **kwargs)
                    return WriteProxy(handle, failure) if mode == "xb" else handle

                with mock.patch.object(streaming, "open", side_effect=failing_open, create=True):
                    with self.assertRaises(streaming.StreamV2Error) as caught:
                        stage.accept(chunks[0])
                self.assertEqual(caught.exception.code, expected_code)
                self.assert_terminal_cleanup(stage)

        with tempfile.TemporaryDirectory() as root:
            source, chunks, stage = self.make_transfer(root, b"abc")
            stage.accept(chunks[0])
            with mock.patch.object(resume, "durable_replace", side_effect=OSError("rename")):
                with self.assertRaises(streaming.StreamV2Error) as caught:
                    stage.finalize(source.completion())
            self.assertEqual(caught.exception.code, "finalize_rename_failed")
            self.assert_terminal_cleanup(stage)

    def test_fsync_receiver_hash_and_staging_disk_full_are_terminal(self):
        with tempfile.TemporaryDirectory() as root:
            _source, chunks, stage = self.make_transfer(root, b"abc")
            with mock.patch.object(streaming.os, "fsync", side_effect=OSError(errno.EIO, "sync")):
                with self.assertRaises(streaming.StreamV2Error) as caught:
                    stage.accept(chunks[0])
            self.assertEqual(caught.exception.code, "target_flush_failed")
            self.assert_terminal_cleanup(stage)

        class FailingHash:
            def update(self, _payload):
                raise RuntimeError("hash")

            def hexdigest(self):
                return "0" * 64

        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "source")
            with open(path, "wb") as handle:
                handle.write(b"abc")
            entries, manifest = captured_manifest([path])
            identifier = transfer_id()
            source = streaming.SequentialFileStream(identifier, manifest, entries, chunk_size=3)
            chunks = list(source.iter_chunks())
            stage = streaming.IncomingTransferStage(
                os.path.join(root, "incoming"), identifier, manifest,
                hash_factory=FailingHash)
            with self.assertRaises(streaming.StreamV2Error) as caught:
                stage.accept(chunks[0])
            self.assertEqual(caught.exception.code, "receiver_hash_failed")
            self.assert_terminal_cleanup(stage)

        with tempfile.TemporaryDirectory() as root:
            incoming = os.path.join(root, "incoming")
            os.mkdir(incoming)
            with mock.patch.object(streaming.os, "mkdir",
                                   side_effect=OSError(errno.ENOSPC, "full")):
                with self.assertRaises(streaming.StreamV2Error) as caught:
                    streaming.IncomingTransferStage(
                        incoming, transfer_id(),
                        manifest_v2.build_manifest("empty", 0, []))
            self.assertEqual(caught.exception.code, "disk_full")

    def test_abort_removes_partial_stage(self):
        with tempfile.TemporaryDirectory() as root:
            _source, chunks, stage = self.make_transfer(root)
            stage.accept(chunks[0])
            stage.abort()
            self.assert_terminal_cleanup(stage)

    def test_second_file_rename_failure_removes_all_staged_results(self):
        with tempfile.TemporaryDirectory() as root:
            first = os.path.join(root, "a")
            second = os.path.join(root, "b")
            for path, payload in ((first, b"a"), (second, b"b")):
                with open(path, "wb") as handle:
                    handle.write(payload)
            entries, manifest = captured_manifest([first, second])
            identifier = transfer_id()
            source = streaming.SequentialFileStream(
                identifier, manifest, entries, chunk_size=1)
            stage = streaming.IncomingTransferStage(
                os.path.join(root, "incoming"), identifier, manifest)
            for chunk in source.iter_chunks():
                stage.accept(chunk)
            real_replace = os.replace
            calls = 0

            def fail_second(source_path, target_path):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("rename")
                return real_replace(source_path, target_path)

            with mock.patch.object(resume, "durable_replace", side_effect=fail_second):
                with self.assertRaises(streaming.StreamV2Error) as caught:
                    stage.finalize(source.completion())
            self.assertEqual(caught.exception.code, "finalize_rename_failed")
            self.assert_terminal_cleanup(stage)


if __name__ == "__main__":
    unittest.main()
