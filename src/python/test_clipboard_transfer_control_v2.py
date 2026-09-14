"""Cancellation, finite timeouts, progress/diagnostics and lease-only
materialization for transfer V2 (Phase 3, sections 20-22 and 24)."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
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
import clipboard_transfer_control_v2 as control
from clipboard_runtime import ClipboardManager


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


class TransferTimeoutsTests(unittest.TestCase):
    def test_defaults_are_finite_and_settings_are_read_and_clamped(self):
        defaults = control.TransferTimeouts()
        for name in control.TIMEOUT_NAMES:
            self.assertGreater(getattr(defaults, name), 0)
        settings = model.clipboard_settings({"clipboard": {
            "clipboard_transfer_v2_preflight_timeout_s": 7,
            "clipboard_transfer_v2_manifest_ack_timeout_s": 0,        # clamped to 1
            "clipboard_transfer_v2_window_ack_timeout_s": 10 ** 9,   # clamped to 86400
            "clipboard_transfer_v2_no_progress_timeout_s": "abc",    # default
            "clipboard_transfer_v2_reconnect_wait_timeout_s": 42,
            "clipboard_transfer_v2_final_complete_ack_timeout_s": 3}})
        timeouts = control.TransferTimeouts.from_settings(settings)
        self.assertEqual(timeouts.preflight, 7)
        self.assertEqual(timeouts.manifest_ack, 1)
        self.assertEqual(timeouts.window_ack, 86400)
        self.assertEqual(timeouts.no_progress, defaults.no_progress)
        self.assertEqual(timeouts.reconnect_wait, 42)
        self.assertEqual(timeouts.final_complete_ack, 3)
        with self.assertRaises(ValueError):
            control.TransferTimeouts(no_progress=float("inf"))
        with self.assertRaises(ValueError):
            control.TransferTimeouts(preflight=0)

    def test_every_phase_deadline_fires_exactly_once_at_its_deadline(self):
        timeouts = control.TransferTimeouts(
            preflight=5, manifest_ack=6, window_ack=7, no_progress=8,
            reconnect_wait=9, final_complete_ack=10)
        expected = {
            "preflight": ("preflight", 5), "sending_manifest": ("manifest_ack", 6),
            "awaiting_ack": ("window_ack", 7), "receiving": ("no_progress", 8),
            "transferring": ("no_progress", 8), "paused": ("reconnect_wait", 9),
            "waiting_reconnect": ("reconnect_wait", 9),
            "finalizing": ("final_complete_ack", 10), "cancelled": ("final_complete_ack", 10),
        }
        for phase, (name, seconds) in expected.items():
            tracker = control.DeadlineTracker(timeouts, phase, 100.0)
            self.assertEqual(tracker.timeout_name, name)
            self.assertEqual(tracker.deadline, 100.0 + seconds)
            self.assertIsNone(tracker.check(100.0 + seconds - 1e-6), phase)
            self.assertEqual(tracker.check(100.0 + seconds), name, phase)
            self.assertIsNone(tracker.check(100.0 + seconds + 1000), phase)
        # Progress restarts the wait; terminal phases never wait.
        tracker = control.DeadlineTracker(timeouts, "receiving", 0.0)
        tracker.progress(4.0)
        self.assertIsNone(tracker.check(8.0))
        self.assertEqual(tracker.check(12.0), "no_progress")
        for phase in ("completed", "failed", "purged"):
            tracker.enter(phase, 0.0)
            self.assertIsNone(tracker.deadline)
            self.assertIsNone(tracker.check(10 ** 9))
        with self.assertRaises(ValueError):
            tracker.enter("bogus", 0.0)

    def test_rate_and_eta_ignore_paused_time(self):
        tracker = control.TransferRateTracker(alpha=1.0)
        tracker.record(50, 0.0)
        tracker.record(50, 1.0)
        tracker.record(50, 2.0)
        self.assertEqual(tracker.rate_bytes_per_s, 50.0)
        self.assertEqual(tracker.eta_seconds(500), 10.0)
        tracker.pause()
        self.assertEqual(tracker.rate_bytes_per_s, 50.0)
        self.assertEqual(tracker.eta_seconds(500), 10.0)
        tracker.record(50, 1000.0)  # resume after a long gap: anchors only
        self.assertEqual(tracker.rate_bytes_per_s, 50.0)
        tracker.record(50, 1001.0)
        self.assertEqual(tracker.rate_bytes_per_s, 50.0)
        self.assertEqual(tracker.eta_seconds(0), 0.0)
        self.assertIsNone(control.TransferRateTracker().eta_seconds(10))


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flowshift-control-v2-")
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

    def make_transfer(self, payload=b"0123456789" * 20, chunk_size=50, name=None):
        path = self.source / (name or f"{uuid.uuid4().hex}.bin")
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
        with _disk(HUGE_FREE):
            return self.manager.prepare_stream_v2_receive(
                self.identity, manifest, transfer_id=identifier, peer_id=PEER,
                provider_id=f"device:{PEER}", **kwargs)

    def journal(self, identifier):
        return self.manager.stream_v2_journal_store().load("incoming", identifier)

    def assert_no_journal(self, identifier):
        with self.assertRaises(resume.ResumeJournalError):
            self.journal(identifier)

    def assert_cancelled_end_state(self, identifier, stage, *, from_state):
        self.assertEqual(stage.state, "cancelled")
        self.assertEqual(stage.cancel_reason, "user")
        self.assertIsNone(stage._file)
        self.assertEqual(self.journal(identifier).state, "cancelled")
        activity = self.manager.transfer_activity_state()
        self.assertFalse(activity["busy"])
        self.assertEqual(activity["sessions"], 0)
        self.assertEqual(activity["states"], [])
        self.assertTrue(os.path.isdir(stage.stage_directory), "partials retained until ack")
        status, = self.manager.stream_v2_status()
        self.assertEqual(status["state"], "cancelled")
        self.assertEqual(status["timeout"], "final_complete_ack")
        self.assertTrue(any(f"stream_v2 cancel transfer={identifier} reason=user from={from_state}"
                            in msg for _level, msg in self.logs))


class CancellationTests(_Fixture):
    def test_cancel_in_preflight_phase_then_ack_purges_everything(self):
        identifier, manifest, _item, _source = self.prepare_only()
        stage = self.stage
        result = self.manager.cancel_stream_v2_session(identifier, "user")
        self.assertTrue(result["ok"])
        self.assertEqual(result["journal_state"], "cancelled")
        self.assert_cancelled_end_state(identifier, stage, from_state="receiving")
        # Idempotent: a second cancel does nothing.
        self.assertFalse(self.manager.cancel_stream_v2_session(identifier)["ok"])
        self.assertTrue(self.manager.acknowledge_stream_v2_cancel(identifier))
        self.assertEqual(stage.state, "purged")
        self.assertFalse(os.path.lexists(stage.stage_directory))
        self.assert_no_journal(identifier)
        self.assertEqual(self.manager.stream_v2_status(), [])
        self.assertFalse(self.manager.acknowledge_stream_v2_cancel(identifier))

    def prepare_only(self):
        identifier, manifest, item, source = self.make_transfer()
        _result, stage = self.prepare(identifier, manifest)
        self.assertIsNotNone(stage)
        self.stage = stage
        return identifier, manifest, item, source

    def test_cancel_during_transfer_closes_handle_and_keeps_partial_until_ack(self):
        identifier, _manifest, _item, source = self.prepare_only()
        stage = self.stage
        chunks = list(source.iter_chunks())
        stage.accept(chunks[0], now=0.0)
        self.assertIsNotNone(stage._file)
        result = self.manager.cancel_stream_v2_session(identifier, "user", now=1.0)
        self.assertTrue(result["ok"])
        self.assert_cancelled_end_state(identifier, stage, from_state="receiving")
        part = os.path.join(stage.stage_directory, "0.part")
        self.assertEqual(os.path.getsize(part), 50)
        self.assertEqual(self.journal(identifier).entries[0]["durable_offset"], 50)
        with self.assertRaises(streaming.StreamV2Error):
            stage.accept(chunks[1])
        self.assertTrue(self.manager.acknowledge_stream_v2_cancel(identifier))
        self.assertFalse(os.path.lexists(part))
        self.assert_no_journal(identifier)

    def test_cancel_during_verification_and_finalization_phase(self):
        identifier, _manifest, _item, source = self.prepare_only()
        stage = self.stage
        for chunk in source.iter_chunks():
            stage.accept(chunk, now=0.0)
        stage.finalize(source.completion(), now=1.0)
        self.assertEqual(stage.state, "finalizing")
        verified = os.path.join(stage.stage_directory, "0.verified")
        self.assertTrue(os.path.isfile(verified))
        self.assertTrue(self.manager.cancel_stream_v2_session(identifier, "user", now=2.0)["ok"])
        self.assert_cancelled_end_state(identifier, stage, from_state="finalizing")
        with self.assertRaises(streaming.StreamV2Error):
            stage.publish(self.store, _item)
        self.assertTrue(self.manager.acknowledge_stream_v2_cancel(identifier))
        self.assertFalse(os.path.lexists(verified))
        self.assert_no_journal(identifier)
        self.assertEqual(self.store.get_item(_item["item_id"])["payload_state"], "receiving")

    def test_cancel_from_paused_and_failed_states(self):
        identifier, _manifest, _item, source = self.prepare_only()
        stage = self.stage
        stage.accept(next(source.iter_chunks()), now=0.0)
        stage.pause(disconnected=True, now=1.0)
        self.assertTrue(self.manager.cancel_stream_v2_session(identifier, now=2.0)["ok"])
        self.assert_cancelled_end_state(identifier, stage, from_state="waiting_reconnect")

    def test_cancel_unknown_transfer_is_reported(self):
        result = self.manager.cancel_stream_v2_session("does-not-exist")
        self.assertEqual(result, {"ok": False, "transfer_id": "does-not-exist",
                                  "reason": "unknown_transfer"})

    def test_user_cancel_without_ack_is_purged_exactly_at_final_ack_timeout(self):
        self.manager.settings_fn = _settings(clipboard_transfer_v2_final_complete_ack_timeout_s=30)
        identifier, _manifest, _item, source = self.prepare_only()
        stage = self.stage
        stage.accept(next(source.iter_chunks()), now=0.0)
        self.manager.cancel_stream_v2_session(identifier, now=100.0)
        early = self.manager.run_stream_v2_maintenance(now=129.999)
        self.assertEqual(early["fired"], [])
        self.assertEqual(stage.state, "cancelled")
        fired = self.manager.run_stream_v2_maintenance(now=130.0)
        self.assertEqual(fired["fired"], [(identifier, "final_complete_ack")])
        self.assertEqual(fired["pruned"], [identifier])
        self.assertEqual(stage.state, "purged")
        self.assertFalse(os.path.lexists(stage.stage_directory))
        self.assert_no_journal(identifier)
        self.assertEqual(self.manager.stream_v2_status(), [])
        self.assertTrue(any(level == "WARN" and f"stream_v2 timeout transfer={identifier} "
                            "timeout=final_complete_ack state=purged" in msg
                            for level, msg in self.logs))

    def test_shutdown_style_abort_without_journal_has_no_pending_wait(self):
        identifier, manifest, _item, source = self.make_transfer()
        acceptance = preflight.accept_preflight(
            identifier, manifest, preflight.estimate_stream_v2(manifest, free_bytes=HUGE_FREE),
            now=0, expires_at=10 ** 12)
        stage = streaming.IncomingTransferStage(
            self.store.object_store_v2.incoming_root, identifier, manifest,
            accepted_preflight=acceptance, now=0.0)
        stage.accept(next(source.iter_chunks()), now=1.0)
        self.assertTrue(stage.cancel("user", now=2.0))
        self.assertEqual(stage.state, "cancelled")
        self.assertFalse(os.path.lexists(stage.stage_directory))
        self.assertIsNone(stage.check_timeouts(10 ** 9))
        self.assertFalse(stage.cancel())


class TimeoutTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.manager.settings_fn = _settings(
            clipboard_transfer_v2_preflight_timeout_s=10,
            clipboard_transfer_v2_no_progress_timeout_s=20,
            clipboard_transfer_v2_reconnect_wait_timeout_s=100,
            clipboard_transfer_v2_final_complete_ack_timeout_s=5)

    def test_settings_reach_the_stage(self):
        identifier, manifest, _item, _source = self.make_transfer()
        _result, stage = self.prepare(identifier, manifest)
        self.assertEqual(stage.timeouts, control.TransferTimeouts(
            preflight=10, manifest_ack=30, window_ack=30, no_progress=20,
            reconnect_wait=100, final_complete_ack=5))

    def test_preflight_timeout_without_payload_purges_stage_and_journal(self):
        identifier, manifest, _item, _source = self.make_transfer()
        with mock.patch.object(time, "monotonic", return_value=1000.0):
            _result, stage = self.prepare(identifier, manifest)
        status, = self.manager.stream_v2_status(now=1000.0)
        self.assertEqual((status["timeout"], status["timeout_remaining_seconds"]), ("preflight", 10.0))
        self.assertEqual(self.manager.run_stream_v2_maintenance(now=1009.9)["fired"], [])
        self.assertEqual(stage.state, "receiving")
        result = self.manager.run_stream_v2_maintenance(now=1010.0)
        self.assertEqual(result["fired"], [(identifier, "preflight")])
        self.assertEqual(stage.state, "purged")
        self.assertEqual(stage.error_code, "preflight_timeout")
        self.assertFalse(os.path.lexists(stage.stage_directory))
        self.assert_no_journal(identifier)
        self.assertEqual(self.manager.transfer_activity_state()["sessions"], 0)

    def test_no_progress_then_reconnect_timeout_checkpoint_retain_then_retryable_failure(self):
        identifier, manifest, _item, source = self.make_transfer()
        _result, stage = self.prepare(identifier, manifest)
        chunks = list(source.iter_chunks())
        stage.accept(chunks[0], now=50.0)
        stage.accept(chunks[1], now=60.0)      # progress restarts the wait
        self.assertEqual(self.manager.run_stream_v2_maintenance(now=79.99)["fired"], [])
        self.assertEqual(stage.state, "receiving")
        result = self.manager.run_stream_v2_maintenance(now=80.0)
        self.assertEqual(result["fired"], [(identifier, "no_progress")])
        self.assertEqual(result["transitions"], [(identifier, "receiving", "paused")])
        self.assertEqual(stage.state, "paused")
        self.assertEqual(stage.error_code, "no_progress_timeout")
        self.assertIsNone(stage._file)
        journal = self.journal(identifier)
        self.assertEqual(journal.state, "paused")
        self.assertEqual(journal.entries[0]["durable_offset"], 100)
        self.assertEqual(os.path.getsize(os.path.join(stage.stage_directory, "0.part")), 100)
        self.assertTrue(stage.durably_checkpointed)
        activity = self.manager.transfer_activity_state()
        self.assertFalse(activity["busy"])
        self.assertEqual(activity["states"], ["stream_v2:paused:durable"])
        # Paused is bounded by reconnect_wait (100 s from the pause at 80).
        self.assertEqual(self.manager.run_stream_v2_maintenance(now=179.9)["fired"], [])
        result = self.manager.run_stream_v2_maintenance(now=180.0)
        self.assertEqual(result["fired"], [(identifier, "reconnect_wait")])
        self.assertEqual(stage.state, "failed")
        self.assertEqual(stage.failure_code, "reconnect_timeout")
        self.assertEqual(stage.error_code, "reconnect_timeout")
        self.assertEqual(self.journal(identifier).state, "failed")
        self.assertEqual(self.journal(identifier).entries[0]["durable_offset"], 100)
        self.assertTrue(os.path.isfile(os.path.join(stage.stage_directory, "0.part")))
        self.assertEqual(result["pruned"], [identifier])
        # The retained checkpoint is still resumable through the productive path.
        resumed, reopened = self.prepare(identifier, manifest, resume=True)
        self.assertEqual(resumed["resume_bytes"], 100)
        self.assertEqual(reopened.resume_bytes, 100)
        self.assertEqual(reopened.status()["retry_count"], 1)
        reopened.accept(chunks[2], now=200.0)
        reopened.accept(chunks[3], now=201.0)
        reopened.finalize(source.completion(), now=202.0)
        item, _evicted = self.manager.publish_stream_v2_session(identifier, _item)
        self.assertEqual(item["payload_state"], "cached")

    def test_waiting_reconnect_times_out_exactly_at_reconnect_deadline(self):
        identifier, manifest, _item, source = self.make_transfer()
        _result, stage = self.prepare(identifier, manifest)
        stage.accept(next(source.iter_chunks()), now=0.0)
        stage.pause(disconnected=True, now=10.0)
        self.assertEqual(self.journal(identifier).state, "waiting_reconnect")
        self.assertIsNone(stage.check_timeouts(109.999))
        self.assertEqual(stage.check_timeouts(110.0), "reconnect_wait")
        self.assertEqual(stage.state, "failed")
        self.assertEqual(stage.failure_code, "reconnect_timeout")
        self.assertEqual(self.journal(identifier).state, "failed")
        self.assertIsNone(stage.check_timeouts(10 ** 9))

    def test_final_ack_timeout_keeps_finalizing_journal_and_verified_stage(self):
        identifier, manifest, item, source = self.make_transfer()
        _result, stage = self.prepare(identifier, manifest)
        for chunk in source.iter_chunks():
            stage.accept(chunk, now=0.0)
        stage.finalize(source.completion(), now=10.0)
        self.assertEqual(self.manager.run_stream_v2_maintenance(now=14.9)["fired"], [])
        result = self.manager.run_stream_v2_maintenance(now=15.0)
        self.assertEqual(result["fired"], [(identifier, "final_complete_ack")])
        self.assertEqual(stage.state, "failed")
        self.assertEqual(stage.failure_code, "final_ack_timeout")
        self.assertEqual(self.journal(identifier).state, "finalizing")
        self.assertTrue(os.path.isfile(os.path.join(stage.stage_directory, "0.verified")))
        # Bounded finalization retry from the durable evidence still succeeds.
        with _disk(HUGE_FREE):
            _resumed, reopened = self.prepare(identifier, manifest, resume=True)
        reopened.recover_finalization()
        self.manager.register_stream_v2_session(identifier, reopened, identity=self.identity)
        published, _ = self.manager.publish_stream_v2_session(identifier, item)
        self.assertEqual(published["payload_state"], "cached")
        self.assertEqual(self.journal(identifier).state, "completed")

    def test_maintenance_is_throttled_without_explicit_clock(self):
        identifier, manifest, _item, _source = self.make_transfer()
        self.prepare(identifier, manifest)
        first = self.manager.run_stream_v2_maintenance()
        second = self.manager.run_stream_v2_maintenance()
        self.assertEqual(first["checked"], 1)
        self.assertEqual(second["checked"], 0)


class StatusTests(_Fixture):
    def test_status_percent_rate_eta_and_privacy(self):
        identifier, manifest, item, source = self.make_transfer(name="report.bin")
        _result, stage = self.prepare(identifier, manifest)
        chunks = list(source.iter_chunks())
        stage.accept(chunks[0], now=0.0)
        stage.accept(chunks[1], now=1.0)
        stage.accept(chunks[2], now=2.0)
        status, = self.manager.stream_v2_status(now=2.0)
        self.assertEqual(status["transfer_id"], identifier)
        self.assertEqual(status["item_id"], item["item_id"])
        self.assertEqual(status["strategy"], "stream_v2")
        self.assertEqual(status["state"], "receiving")
        self.assertEqual(status["current_file"], "report.bin")
        self.assertEqual(status["file_index"], 0)
        self.assertEqual(status["file_count"], 1)
        self.assertEqual(status["current_file_bytes"], 150)
        self.assertEqual(status["total_bytes"], 200)
        self.assertEqual(status["bytes_done"], 150)
        self.assertEqual(status["percent"], 75.0)
        self.assertEqual(status["rate_bytes_per_s"], 50.0)
        self.assertEqual(status["eta_seconds"], 1.0)
        self.assertEqual(status["resume_bytes"], 0)
        self.assertEqual(status["retry_count"], 0)
        self.assertEqual(status["provider"], f"device:{PEER}")
        self.assertEqual(status["preflight_state"], "accepted")
        self.assertIsNone(status["error_code"])
        self.assertEqual(status["timeout"], "no_progress")
        self.assertEqual(status["identity"], self.identity)
        serialized = json.dumps(status)
        for private in (str(self.source), str(self.root), stage.stage_directory,
                        self.store.dir, os.sep + "runtime"):
            self.assertNotIn(private.replace("\\", "\\\\"), serialized)
            self.assertNotIn(private, serialized)
        # Pause freezes rate and ETA; the paused time never distorts them.
        stage.pause(now=3.0)
        paused, = self.manager.stream_v2_status(now=5000.0)
        self.assertEqual(paused["rate_bytes_per_s"], 50.0)
        self.assertEqual(paused["eta_seconds"], 1.0)
        self.assertEqual(paused["state"], "paused")
        self.assertEqual(paused["timeout"], "reconnect_wait")
        # A resumed transfer reports resume bytes and does not rate the gap.
        _resumed, reopened = self.prepare(identifier, manifest, resume=True)
        reopened.accept(chunks[3], now=9000.0)
        resumed_status = reopened.status(now=9000.0)
        self.assertEqual(resumed_status["resume_bytes"], 150)
        self.assertEqual(resumed_status["retry_count"], 1)
        self.assertEqual(resumed_status["rate_bytes_per_s"], 0.0)
        self.assertEqual(resumed_status["percent"], 100.0)
        self.assertIsNone(resumed_status["current_file"])
        diagnostics = self.manager.diagnostics(self.identity)
        self.assertEqual([entry["transfer_id"] for entry in diagnostics["stream_v2"]], [identifier])
        self.assertIn("activity", diagnostics)

    def test_logs_never_contain_private_paths_and_keyed_logs_are_rate_limited(self):
        identifier, manifest, _item, source = self.make_transfer()
        _result, stage = self.prepare(identifier, manifest)
        stage.accept(next(source.iter_chunks()), now=0.0)
        self.manager.cancel_stream_v2_session(identifier, now=1.0)
        self.manager.run_stream_v2_maintenance(now=10 ** 6)
        for _level, msg in self.logs:
            self.assertNotIn(str(self.root), msg)
            self.assertNotIn(str(self.source), msg)
        self.assertTrue(self.manager._log_stream_v2("repeat", key="k"))
        self.assertFalse(self.manager._log_stream_v2("repeat", key="k"))
        self.assertEqual(sum(1 for _l, msg in self.logs if msg == "repeat"), 1)


class LeaseOnlyMaterializationTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.manager.settings_fn = _settings(cache_received_payloads=False)

    def _receive(self):
        identifier, manifest, item, source = self.make_transfer(name="lease.bin")
        _result, stage = self.prepare(identifier, manifest)
        for chunk in source.iter_chunks():
            stage.accept(chunk, now=0.0)
        stage.finalize(source.completion(), now=1.0)
        published, _evicted = self.manager.publish_stream_v2_session(identifier, item)
        return published, stage

    def _store_object_files(self):
        objects_root = Path(self.store.object_store_v2.objects_root)
        return sorted(p for p in objects_root.rglob("*") if p.is_file()) if objects_root.exists() else []

    def test_files_live_only_in_the_lease_and_nothing_persists_after_lease_end(self):
        item, stage = self._receive()
        self.assertEqual(item["payload_state"], "cached")
        self.assertIsNone(self.store.get_cache_entry(item["sha256"]))
        self.assertEqual(len(self._store_object_files()), 1)
        self.assertTrue(any("cache=lease_only" in msg for _l, msg in self.logs))
        dest_root = self.root / "dest"
        dest_root.mkdir()
        result = self.manager.materialize_files_result(self.identity, item["item_id"], str(dest_root))
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["lease_only"])
        materialized = Path(result["paths"][0])
        self.assertEqual(materialized.name, "lease.bin")
        self.assertEqual(materialized.read_bytes(), b"0123456789" * 20)
        lease = self.store.get_lease(item["item_id"])
        self.assertEqual(lease["state"], model.LEASE_ACTIVE)
        # The store no longer owns the bytes: item retired, receipt pruned.
        retired = self.store.get_item(item["item_id"])
        self.assertEqual(retired["payload_state"], "missing")
        self.assertFalse(retired["available"])
        self.assertIsNone(self.store.v2_manifest_for_item(item["item_id"]))
        for path in self._store_object_files():
            self.assertGreaterEqual(os.stat(path).st_nlink, 2, "only the lease hardlink keeps it")
        self.assertTrue(materialized.exists())
        # Lease end removes the tree and every remaining object.
        self.store.release_stale_leases(lease["owner_sequence"] + 1 if lease.get("owner_sequence") else 1)
        self.assertTrue(self.store.release_lease(item["item_id"]))
        self.assertFalse(materialized.exists())
        self.assertEqual([p for p in Path(lease["dest_path"]).rglob("*") if p.is_file()], [])
        self.assertEqual(self._store_object_files(), [])
        self.assertFalse(os.path.lexists(stage.stage_directory))
        incoming = Path(self.store.object_store_v2.incoming_root)
        self.assertEqual([p for p in incoming.rglob("*")] if incoming.exists() else [], [])
        self.assertFalse(list(Path(self.manager.store_root).rglob("*.zip")))

    def test_cleanup_leases_after_stale_frees_lease_only_objects(self):
        item, _stage = self._receive()
        dest_root = self.root / "dest"
        dest_root.mkdir()
        result = self.manager.materialize_files_result(self.identity, item["item_id"], str(dest_root))
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.store.bind_lease_sequence(item["item_id"], 7))
        self.assertEqual(self.manager.retire_leases_for_sequence(8), {self.identity: [item["item_id"]]})
        self.assertTrue(Path(result["paths"][0]).exists(), "stale lease tree retained until aged")
        self.assertEqual(self.store.cleanup_leases(0), [item["item_id"]])
        self.assertFalse(Path(result["paths"][0]).exists())
        self.assertEqual(self._store_object_files(), [])

    def test_cache_enabled_materialization_keeps_objects(self):
        self.manager.settings_fn = _settings(cache_received_payloads=True)
        item, _stage = self._receive()
        self.assertIsNotNone(self.store.get_cache_entry(item["sha256"]))
        dest_root = self.root / "dest"
        dest_root.mkdir()
        result = self.manager.materialize_files_result(self.identity, item["item_id"], str(dest_root))
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["lease_only"])
        self.assertEqual(self.store.get_item(item["item_id"])["payload_state"], "cached")
        self.assertTrue(self.store.release_lease(item["item_id"]))
        self.assertEqual(len(self._store_object_files()), 1)


if __name__ == "__main__":
    unittest.main()
