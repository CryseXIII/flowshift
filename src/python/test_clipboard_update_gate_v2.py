"""Runtime preflight V2 and update idle gate tests (Phase 3, sections 18 and 23)."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_files as files
import clipboard_model as model
import clipboard_preflight_v2 as preflight
import clipboard_resume_v2 as resume
import clipboard_streaming_v2 as streaming
import clipboard_transfer as ctt
import clipboard_runtime
from clipboard_runtime import ClipboardManager
from update_manager import RESULT_QUEUED, UpdateManager
from update_model import STATUS_UPDATE_AVAILABLE, UpdateResult
from update_safety import is_safe_to_install_update
from update_state import DOWNLOADED, INSTALLING, WAITING_FOR_IDLE
from test_update_manager import FakeTransport, descriptor, response


LOCAL = "local-device"
PEER = "peer-a"
HUGE_FREE = 10 ** 13


def _settings(**overrides):
    raw = {"enabled": True}
    raw.update(overrides)
    return lambda: model.clipboard_settings({"clipboard": raw})


def _disk(free_bytes):
    return mock.patch.object(ctt, "check_disk_space", side_effect=lambda *_a, **_k: {
        "ok": True, "free_bytes": free_bytes, "required_bytes": 0,
        "margin_bytes": 0, "missing_bytes": 0, "path": "x"})


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flowshift-gate-v2-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.logs = []
        self.manager = ClipboardManager(
            str(self.root / "runtime"), LOCAL, lambda _identity, _msg: None,
            _settings(), log_fn=lambda level, msg: self.logs.append((level, msg)))
        self.addCleanup(self.manager.shutdown)
        self.identity = f"device:{PEER}"
        self.store = self.manager.store(self.identity)
        self.source = self.root / "source"
        self.source.mkdir()

    def make_transfer(self, payload=b"0123456789" * 20, chunk_size=64):
        path = self.source / f"{uuid.uuid4().hex}.bin"
        path.write_bytes(payload)
        provisional = files.make_file_item([str(path)])
        local = provisional["files"]
        remote = copy.deepcopy(provisional)
        remote.pop("files")
        remote["source_available"] = False
        remote["providers"] = [
            {"device_id": PEER, "state": "available", "last_seen_at": time.time()}]
        remote = model.version_item(remote, payload_state="receiving")
        provisional, _ = self.store.add_item(remote, make_current=True)
        manifest = provisional["batch_manifest"]
        identifier = uuid.uuid4().hex
        acceptance = preflight.accept_preflight(
            identifier, manifest, preflight.estimate_stream_v2(manifest, free_bytes=HUGE_FREE),
            now=0, expires_at=10 ** 12)
        source = streaming.SequentialFileStream(
            identifier, manifest, local, accepted_preflight=acceptance, chunk_size=chunk_size)
        self.addCleanup(source.close)
        return identifier, manifest, provisional, source

    def prepare(self, identifier, manifest, **kwargs):
        kwargs.setdefault("allow_manual", True)
        return self.manager.prepare_stream_v2_receive(
            self.identity, manifest, transfer_id=identifier, peer_id=PEER,
            provider_id=f"device:{PEER}", **kwargs)


class StreamV2ReceivePreflightRuntimeTests(_Fixture):
    def test_preflight_ok_returns_acceptance_and_exact_accounting(self):
        identifier, manifest, _item, _source = self.make_transfer()
        with _disk(HUGE_FREE):
            result = self.manager.preflight_stream_v2_receive(
                self.identity, manifest, transfer_id=identifier)
        self.assertTrue(result["ok"])
        self.assertEqual(result["strategy"], "stream_v2")
        self.assertEqual(result["resume_bytes"], 0)
        self.assertEqual(result["remaining_payload_bytes"], manifest["total_size"])
        self.assertEqual(result["free_bytes"], HUGE_FREE)
        self.assertEqual(result["required_bytes"],
                         result["peak_required_bytes"] + result["margin"])
        self.assertIsInstance(result["acceptance"], preflight.AcceptedPreflight)
        estimate = result["acceptance"].estimate
        self.assertEqual(result["metadata_overhead_bytes"],
                         estimate.journal_overhead_bytes + estimate.manifest_overhead_bytes)
        self.assertEqual(result["peak_required_bytes"], estimate.peak_required_bytes)
        self.assertEqual(self.manager.last_stream_v2_preflight["reason"], None)
        self.assertNotIn("acceptance", self.manager.last_stream_v2_preflight)

    def test_durable_resume_bytes_reduce_required_bytes_exactly(self):
        identifier, manifest, _item, source = self.make_transfer(chunk_size=50)
        chunks = list(source.iter_chunks())
        with _disk(HUGE_FREE):
            base, stage = self.prepare(identifier, manifest)
        self.assertIsNotNone(stage)
        stage.accept(chunks[0])
        stage.checkpoint()
        stage.pause(disconnected=True)
        journal = self.manager.stream_v2_journal_store().load("incoming", identifier)
        self.assertEqual(journal.entries[0]["durable_offset"], 50)
        with _disk(HUGE_FREE):
            resumed = self.manager.preflight_stream_v2_receive(
                self.identity, manifest, transfer_id=identifier, incoming_journal=journal)
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["resume_bytes"], 50)
        self.assertTrue(resumed["journal_present"])
        self.assertEqual(base["remaining_payload_bytes"] - resumed["remaining_payload_bytes"], 50)
        self.assertEqual(base["peak_required_bytes"] - resumed["peak_required_bytes"], 50)
        self.assertEqual(resumed["acceptance"].estimate.durable_resume_bytes, 50)

    def test_insufficient_space_rejects_and_creates_no_stage_or_journal(self):
        identifier, manifest, _item, _source = self.make_transfer()
        with _disk(HUGE_FREE):
            needed = self.manager.preflight_stream_v2_receive(
                self.identity, manifest, transfer_id=identifier)["required_bytes"]
        with _disk(needed - 1):
            result, stage = self.prepare(identifier, manifest)
        self.assertIsNone(stage)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "disk_full")
        self.assertIsNone(result["acceptance"])
        self.assertEqual(result["missing_bytes"], 1)
        self.assertEqual(self.manager.last_stream_v2_preflight["reason"], "disk_full")
        self.assertFalse(os.path.lexists(
            os.path.join(self.store.object_store_v2.incoming_root, identifier)))
        with self.assertRaises(resume.ResumeJournalError):
            self.manager.stream_v2_journal_store().load("incoming", identifier)
        self.assertFalse(self.manager.transfer_activity_state()["busy"])
        self.assertTrue(any("stream_v2 preflight rejected reason=disk_full" in msg
                            for _level, msg in self.logs))

    def test_cache_disabled_counts_full_payload_and_still_allows_resume_with_journal(self):
        self.manager.settings_fn = _settings(cache_received_payloads=False)
        identifier, manifest, _item, source = self.make_transfer(chunk_size=50)
        with _disk(HUGE_FREE):
            result = self.manager.preflight_stream_v2_receive(
                self.identity, manifest, transfer_id=identifier)
        self.assertFalse(result["cache_enabled"])
        self.assertEqual(result["remaining_payload_bytes"], manifest["total_size"])
        self.assertEqual(result["resume_bytes"], 0)
        # Without a journal there is no credit, even for identical content.
        with _disk(HUGE_FREE):
            _result, stage = self.prepare(identifier, manifest)
        stage.accept(next(source.iter_chunks()))
        stage.pause()
        with _disk(HUGE_FREE):
            resumed, reopened = self.prepare(identifier, manifest, resume=True)
        self.assertIsNotNone(reopened)
        self.addCleanup(reopened.close)
        self.assertEqual(resumed["resume_bytes"], 50)
        self.assertEqual(resumed["remaining_payload_bytes"], manifest["total_size"] - 50)

    def test_limits_apply_before_disk_space(self):
        identifier, manifest, _item, _source = self.make_transfer(
            payload=b"x" * (1024 * 1024 + 1), chunk_size=1024 * 1024)
        self.manager.settings_fn = _settings(max_auto_transfer_mb=1)
        with _disk(HUGE_FREE):
            policy = self.manager.preflight_stream_v2_receive(
                self.identity, manifest, transfer_id=identifier, allow_manual=False)
            manual = self.manager.preflight_stream_v2_receive(
                self.identity, manifest, transfer_id=identifier, allow_manual=True)
        self.assertEqual(policy["reason"], "policy")
        self.assertIsNone(policy["acceptance"])
        self.assertTrue(manual["ok"])
        with _disk(HUGE_FREE):
            invalid = self.manager.preflight_stream_v2_receive(
                self.identity, {"not": "a manifest"}, transfer_id=identifier)
        self.assertEqual(invalid["reason"], "invalid_size_metadata")


class UpdateIdleGateTests(_Fixture):
    def test_active_v2_session_blocks_until_published(self):
        identifier, manifest, item, source = self.make_transfer()
        with _disk(HUGE_FREE):
            _result, stage = self.prepare(identifier, manifest)
        state = self.manager.transfer_activity_state()
        self.assertTrue(state["busy"])
        self.assertEqual(state["states"], ["stream_v2:receiving"])
        self.assertEqual(state["sessions"], 1)
        self.assertEqual(state["policy"], "paused_and_waiting_reconnect_allow_update")
        snapshot = self.manager.activity_snapshot()
        self.assertTrue(snapshot["blocking"])
        self.assertEqual(snapshot["transfer_activity"], state)
        self.assertEqual(is_safe_to_install_update({"clipboard_activity": snapshot})["reason"],
                         "clipboard_transfer_active")
        for chunk in source.iter_chunks():
            stage.accept(chunk)
        stage.finalize(source.completion())
        self.assertEqual(self.manager.transfer_activity_state()["states"],
                         ["stream_v2:finalizing"])
        stage.publish(self.store, item)
        state = self.manager.transfer_activity_state()
        self.assertFalse(state["busy"])
        self.assertEqual(state["sessions"], 0)
        self.assertFalse(self.manager.activity_snapshot()["blocking"])
        self.assertTrue(is_safe_to_install_update(
            {"clipboard_activity": self.manager.activity_snapshot()})["safe"])

    def test_paused_and_waiting_reconnect_allow_update_only_when_durable(self):
        identifier, manifest, _item, source = self.make_transfer(chunk_size=50)
        with _disk(HUGE_FREE):
            _result, stage = self.prepare(identifier, manifest)
        stage.accept(next(source.iter_chunks()))
        stage.pause(disconnected=True)
        self.assertTrue(stage.durably_checkpointed)
        state = self.manager.transfer_activity_state()
        self.assertFalse(state["busy"])
        self.assertEqual(state["states"], ["stream_v2:waiting_reconnect:durable"])
        self.assertEqual(state["sessions"], 1)
        self.assertFalse(self.manager.activity_snapshot()["blocking"])
        journal = self.manager.stream_v2_journal_store().load("incoming", identifier)
        self.assertEqual(journal.state, "waiting_reconnect")
        self.assertTrue(os.path.isfile(os.path.join(stage.stage_directory, "0.part")))
        # A pause that is not durable (journal disagrees) must still block.
        with mock.patch.object(type(stage), "durably_checkpointed",
                               new_callable=mock.PropertyMock, return_value=False):
            blocked = self.manager.transfer_activity_state()
        self.assertTrue(blocked["busy"])
        self.assertEqual(blocked["states"], ["stream_v2:waiting_reconnect"])
        with _disk(HUGE_FREE):
            _resumed, reopened = self.prepare(identifier, manifest, resume=True)
        self.assertEqual(reopened.state, "receiving")
        self.assertTrue(self.manager.transfer_activity_state()["busy"])
        reopened.pause()
        self.assertEqual(self.manager.transfer_activity_state()["states"],
                         ["stream_v2:paused:durable"])
        self.assertFalse(self.manager.transfer_activity_state()["busy"])

    def test_legacy_job_in_progress_is_busy(self):
        job = ctt.TransferJob(uuid.uuid4().hex, self.store.profile_id, "item-1",
                              "receive", "files", "x", 10, status=ctt.TransferStatus.running)
        with mock.patch.object(ctt, "_persist_job_session"):
            self.manager._register_job(job)
        state = self.manager.transfer_activity_state()
        self.assertTrue(state["busy"])
        self.assertEqual(state["states"], ["legacy:running"])
        self.assertTrue(self.manager.activity_snapshot()["blocking"])
        job.status = ctt.TransferStatus.completed
        self.assertFalse(self.manager.transfer_activity_state()["busy"])

    def test_disk_full_failure_is_not_busy_and_journal_survives(self):
        identifier, manifest, _item, source = self.make_transfer(chunk_size=50)
        with _disk(HUGE_FREE):
            _result, stage = self.prepare(identifier, manifest)
        chunks = list(source.iter_chunks())
        stage.accept(chunks[0])
        stage.checkpoint()
        real_file = stage._file

        class FullDisk:
            def __getattr__(self, name):
                return getattr(real_file, name)

            def write(self, _payload):
                raise OSError(28, "no space left")

        stage._file = FullDisk()
        with self.assertRaises(streaming.StreamV2Error) as caught:
            stage.accept(chunks[1])
        self.assertEqual(caught.exception.code, "disk_full")
        self.assertEqual(stage.failure_code, "disk_full")
        self.assertFalse(self.manager.transfer_activity_state()["busy"])
        journal = self.manager.stream_v2_journal_store().load("incoming", identifier)
        self.assertEqual(journal.state, "failed")
        self.assertEqual(journal.entries[0]["durable_offset"], 50)

    def test_deferral_log_is_rate_limited_and_only_during_maintenance(self):
        identifier, manifest, _item, _source = self.make_transfer()
        with _disk(HUGE_FREE):
            self.prepare(identifier, manifest)
        self.manager.activity_snapshot()
        deferrals = [msg for level, msg in self.logs
                     if level == "INFO" and "update install deferred" in msg]
        self.assertEqual(deferrals, [])
        self.manager.set_update_maintenance(True)
        self.manager.activity_snapshot()
        self.manager.activity_snapshot()
        deferrals = [msg for level, msg in self.logs
                     if level == "INFO" and "update install deferred" in msg]
        self.assertEqual(len(deferrals), 1)
        self.assertIn("stream_v2:receiving", deferrals[0])
        with mock.patch.object(clipboard_runtime, "_UPDATE_DEFER_LOG_INTERVAL_SECONDS", 0.0):
            self.manager.activity_snapshot()
        deferrals = [msg for level, msg in self.logs
                     if level == "INFO" and "update install deferred" in msg]
        self.assertEqual(len(deferrals), 2)

    def test_update_manager_defers_install_while_v2_session_active(self):
        identifier, manifest, item, source = self.make_transfer()
        with _disk(HUGE_FREE):
            _result, stage = self.prepare(identifier, manifest)
        content = b"gate installer"
        release = descriptor(content)
        handed_off = threading.Event()
        shutdown_requested = threading.Event()
        official = self.root / "Program Files" / "FlowShift"
        official.mkdir(parents=True)
        clip = self.manager

        def safety():
            return is_safe_to_install_update({"clipboard_activity": clip.activity_snapshot()})

        handoff = mock.Mock(side_effect=lambda _desc, _asset: handed_off.set())
        updater = UpdateManager(
            data_dir=self.root / "update", current_version="0.4.0", updater_version="0.4.0",
            discovery=lambda **kwargs: UpdateResult(STATUS_UPDATE_AVAILABLE, release),
            runtime_root=official, official_install_dir=official,
            program_files=self.root / "other",
            download_transport=FakeTransport([response(200, content)]),
            safety_query=safety, reserve=lambda automatic: clip.set_update_maintenance(True),
            release=lambda: clip.set_update_maintenance(False),
            handoff=handoff, shutdown_callback=shutdown_requested.set,
            install_retry_interval=0.05)
        self.addCleanup(updater.shutdown)
        self.assertEqual(updater.check_for_updates().status, RESULT_QUEUED)
        self.assertTrue(updater.wait_for_quiescence())
        self.assertEqual(updater.download_update().status, RESULT_QUEUED)
        self.assertTrue(updater.wait_for_quiescence())
        self.assertEqual(updater.snapshot()["state"], DOWNLOADED)

        result = updater.install_update(automatic=True)
        self.assertEqual(result.status, RESULT_QUEUED)
        self.assertEqual(result.blocker["reason"], "clipboard_transfer_active")
        self.assertEqual(updater.snapshot()["state"], WAITING_FOR_IDLE)
        self.assertFalse(handed_off.wait(0.3))
        handoff.assert_not_called()
        # The reserved maintenance must not cancel the running transfer.
        for chunk in source.iter_chunks():
            stage.accept(chunk)
        stage.finalize(source.completion())
        self.assertFalse(handed_off.wait(0.2))
        stage.publish(self.store, item)
        self.assertTrue(handed_off.wait(2))
        handoff.assert_called_once()
        self.assertTrue(shutdown_requested.wait(1))
        self.assertTrue(updater.wait_for_quiescence())
        self.assertEqual(updater.snapshot()["state"], INSTALLING)


if __name__ == "__main__":
    unittest.main()
