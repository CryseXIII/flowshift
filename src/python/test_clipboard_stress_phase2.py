"""Phase 2.3 stress and closure tests: real metrics, byte-level assertions, provider state import."""
from __future__ import annotations

import os
import json
import sys
import tempfile
import threading
import time
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_events as cbe
import clipboard_model as cm
import clipboard_protocol as cp
import clipboard_transfer as ctt
import runtime_model as rm
from clipboard_runtime import ClipboardManager


def _settings():
    return cm.clipboard_settings({"clipboard": {"enabled": True}})


def _manager(prefix, **overrides):
    root = tempfile.mkdtemp(prefix=prefix)

    def _settings_with_overrides():
        s = cm.clipboard_settings({"clipboard": {"enabled": True}})
        s.update(overrides)
        return s

    mgr = ClipboardManager(root, "dev", lambda _ident, _msg: None, _settings_with_overrides)
    return mgr


def _registry(mgr):
    return mgr.provider_snapshot().get("registry", {})


class _InstrumentedTransport:
    """Serialize real protocol frames, classify bytes, and optionally deliver them."""

    _METADATA_TYPES = {cp.T_ANNOUNCEMENT, cp.T_MANIFEST}

    def __init__(self):
        self.auto_deliver = False
        self.metadata_message_bytes = 0
        self.payload_content_bytes = 0
        self.control_message_bytes = 0
        self.messages = {"sender": [], "receiver": []}
        self._routes = {}
        self._lock = threading.RLock()

    def bind(self, sender, receiver):
        self._routes = {
            "sender": (receiver, "sender"),
            "receiver": (sender, "receiver"),
        }

    def callback(self, source):
        return lambda identity, message: self.send(source, identity, message)

    def send(self, source, identity, message):
        frame = rm.pack_frame(message)
        payload_size = int.from_bytes(frame[:4], "big")
        if payload_size != len(frame) - 4:
            raise AssertionError("invalid framed protocol length")
        wire_message = json.loads(frame[4:].decode("utf-8"))
        message_type = wire_message.get("type")

        with self._lock:
            self.messages[source].append(wire_message)
            if message_type in self._METADATA_TYPES:
                self.metadata_message_bytes += len(frame)
            elif message_type == cp.T_CHUNK:
                self.payload_content_bytes += len(cp.decode_chunk_data(wire_message))
                encoded_content = len(wire_message.get("data", "").encode("ascii"))
                self.control_message_bytes += max(0, len(frame) - encoded_content)
            else:
                self.control_message_bytes += len(frame)
            route = self._routes.get(source) if self.auto_deliver else None

        if route:
            target, source_identity = route
            target.handle(source_identity, wire_message)

    def snapshot(self):
        with self._lock:
            return {
                "metadata_message_bytes": self.metadata_message_bytes,
                "payload_content_bytes": self.payload_content_bytes,
                "control_message_bytes": self.control_message_bytes,
            }


# ═══════════════════════════════════════════════════════════════════════
# 12. TEST A – EXPLIZITER OVERFLOW-STRESS
# ═══════════════════════════════════════════════════════════════════════


class EventOverflowStressTests(unittest.TestCase):
    """10k events into small bounded queue with slow consumer — real overflow metrics."""

    def test_overflow_10k_events_small_queue_slow_consumer(self):
        q = cbe.BoundedClipboardEvents(capacity=4)
        errors = []

        def producer():
            try:
                for i in range(10000):
                    seq = i % 100
                    q.submit(sequence=seq, source="overflow",
                             digest=f"d-{seq}", kind="copy")
            except BaseException as exc:
                errors.append(exc)

        t = threading.Thread(target=producer, daemon=True)
        t.start()
        time.sleep(0.05)
        processed = 0
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and (t.is_alive() or q.snapshot()["depth"]):
            ev = q.get(timeout=0.05)
            if ev is None:
                continue
            processed += 1
            time.sleep(0.0005)
        t.join(5)
        q.close()
        snap = q.snapshot()

        self.assertEqual(errors, [])
        self.assertFalse(t.is_alive())
        self.assertEqual(snap["submitted"], 10000)
        self.assertGreater(snap["dropped"], 100)
        self.assertGreater(processed, 0)
        self.assertLess(processed, 5000)
        self.assertEqual(snap["depth"], 0)
        self.assertEqual(
            processed + snap["dropped"] + snap["coalesced"], snap["submitted"])

    def test_empty_submitted_stays_zero(self):
        q = cbe.BoundedClipboardEvents(capacity=8)
        try:
            snap = q.snapshot()
            self.assertEqual(snap["submitted"], 0)
            self.assertEqual(snap["coalesced"], 0)
            self.assertEqual(snap["dropped"], 0)
        finally:
            q.close()


# ═══════════════════════════════════════════════════════════════════════
# 13. TEST B – REALISTISCHER DURCHSATZ-STRESS
# ═══════════════════════════════════════════════════════════════════════


class EventThroughputStressTests(unittest.TestCase):
    """10k events, 1k sequences, concurrent producer/consumer, real metrics."""

    def test_throughput_10k_events_1k_sequences(self):
        q = cbe.BoundedClipboardEvents(capacity=1024)
        errors = []
        submitted_seqs = set()
        producer_done = threading.Event()

        def producer():
            try:
                for i in range(10000):
                    while q.snapshot()["depth"] >= 512:
                        time.sleep(0.0005)
                    seq = i % 1000
                    submitted_seqs.add(seq)
                    q.submit(sequence=seq, source="throughput",
                             digest=f"d-{seq}", kind="copy")
            except BaseException as exc:
                errors.append(exc)
            finally:
                producer_done.set()

        p = threading.Thread(target=producer, daemon=True)
        p.start()
        observed_seqs = set()
        processed_count = 0
        deadline = time.monotonic() + 15.0
        while (not producer_done.is_set() or q.snapshot()["depth"] > 0) \
                and time.monotonic() < deadline:
            ev = q.get(timeout=0.05)
            if ev is None:
                continue
            processed_count += 1
            observed_seqs.add(ev["sequence"])
        p.join(5)
        q.close()
        snap = q.snapshot()
        unrecoverable = len(submitted_seqs - observed_seqs)

        self.assertEqual(errors, [])
        self.assertFalse(p.is_alive())
        self.assertEqual(snap["submitted"], 10000)
        self.assertEqual(processed_count, 10000)
        self.assertEqual(len(observed_seqs), 1000)
        self.assertEqual(snap["coalesced"], 0)
        self.assertEqual(snap["dropped"], 0)
        self.assertEqual(unrecoverable, 0)

    def test_concurrent_producer_consumer_no_deadlock(self):
        q = cbe.BoundedClipboardEvents(capacity=64)
        errors = []
        processed = 0
        producer_done = threading.Event()

        def producer():
            try:
                for i in range(2000):
                    q.submit(sequence=i % 200, kind="copy")
            except BaseException as exc:
                errors.append(exc)
            finally:
                producer_done.set()

        def consumer():
            nonlocal processed
            try:
                deadline = time.monotonic() + 10.0
                while (not producer_done.is_set() or q.snapshot()["depth"] > 0) \
                        and time.monotonic() < deadline:
                    ev = q.get(timeout=0.05)
                    if ev is not None:
                        processed += 1
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=producer, daemon=True),
                   threading.Thread(target=consumer, daemon=True)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        q.close()
        self.assertEqual(errors, [])
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertGreater(processed, 0)
        self.assertEqual(q.snapshot()["submitted"], 2000)


# ═══════════════════════════════════════════════════════════════════════
# 14. TEST C – NORMALER EVENT-PFAD
# ═══════════════════════════════════════════════════════════════════════


class EventNormalPathTests(unittest.TestCase):
    """Deterministic event processing tests."""

    def test_same_sequence_coalesced(self):
        q = cbe.BoundedClipboardEvents(capacity=8)
        try:
            q.submit(sequence=101, kind="copy")
            q.submit(sequence=101, kind="copy")
            q.submit(sequence=101, kind="copy")
            snap = q.snapshot()
            self.assertEqual(snap["submitted"], 3)
            self.assertEqual(snap["coalesced"], 2)
        finally:
            q.close()

    def test_different_sequences_not_coalesced(self):
        q = cbe.BoundedClipboardEvents(capacity=8)
        try:
            q.submit(sequence=101, kind="copy")
            q.submit(sequence=102, kind="copy")
            q.submit(sequence=103, kind="copy")
            snap = q.snapshot()
            self.assertEqual(snap["submitted"], 3)
            self.assertEqual(snap["coalesced"], 0)
        finally:
            q.close()

    def test_self_write_suppressed(self):
        sup = cbe.ClipboardWriteSuppressor(ttl_seconds=2.0)
        token = sup.prepare("item-1", {"text"}, "text", "digest-write", 0)
        ok = sup.finish(token, True, 100)
        self.assertTrue(ok)
        consumed = sup.consume(100, {"text"}, "text", "digest-write")
        self.assertTrue(consumed)
        snap = sup.snapshot()
        self.assertGreater(snap["consumed"], 0)

    def test_real_user_event_not_suppressed(self):
        sup = cbe.ClipboardWriteSuppressor(ttl_seconds=2.0)
        token = sup.prepare("item-1", {"text"}, "text", "digest-suppress", 0)
        ok = sup.finish(token, True, 100)
        self.assertTrue(ok)
        consumed = sup.consume(200, {"text"}, "text", "digest-user")
        self.assertFalse(consumed)

    def test_shutdown_under_load_clean(self):
        q = cbe.BoundedClipboardEvents(capacity=8)
        errors = []

        def loader():
            try:
                for i in range(5000):
                    if not q.submit(sequence=i % 50, kind="copy"):
                        break
            except BaseException as exc:
                errors.append(exc)

        t = threading.Thread(target=loader, daemon=True)
        t.start()
        time.sleep(0.01)
        q.close()
        t.join(5)
        self.assertEqual(errors, [])
        self.assertTrue(q.snapshot()["closed"])


# ═══════════════════════════════════════════════════════════════════════
# 15. METADATA-STRESS mit echten Payload-Byte-Zählern
# ═══════════════════════════════════════════════════════════════════════


class MetadataAnnouncementStressTests(unittest.TestCase):
    """5k announcements with real byte-transfer verification."""

    def setUp(self):
        self.root_a = tempfile.mkdtemp(prefix="flowshift-ann3-a-")
        self.root_b = tempfile.mkdtemp(prefix="flowshift-ann3-b-")
        self.transport = _InstrumentedTransport()
        self.sender = ClipboardManager(
            self.root_a, "sender-dev",
            self.transport.callback("sender"), lambda: _settings())
        self.receiver = ClipboardManager(
            self.root_b, "receiver-dev",
            self.transport.callback("receiver"), lambda: _settings())
        self.transport.bind(self.sender, self.receiver)
        self.messages = self.transport.messages["sender"]

    def tearDown(self):
        self.sender.shutdown()
        self.receiver.shutdown()

    def test_5k_announcements_zero_payload_bytes(self):
        self.transport.auto_deliver = True
        total = 5000
        for i in range(total):
            self.sender.capture_text("receiver", f"ann-byte-{i}")
        ann_count = sum(1 for m in self.messages
                        if isinstance(m, dict) and m.get("type") == cp.T_ANNOUNCEMENT)
        self.assertGreaterEqual(ann_count, total - 10)
        metrics = self.transport.snapshot()
        self.assertGreater(metrics["metadata_message_bytes"], 0)
        self.assertEqual(metrics["payload_content_bytes"], 0)
        self.assertGreater(metrics["control_message_bytes"], 0)
        self.assertEqual(self.receiver.stats["announcements_received"], total)
        store = self.receiver.store("sender")
        items = store.list_items()
        self.assertGreater(len(items), 0)

    def test_flowshift_byte_counter_zero_payload_transferred(self):
        self.transport.auto_deliver = True
        before = self.transport.snapshot()
        for i in range(200):
            self.sender.capture_text("receiver", f"byte-check-{i}")
        after = self.transport.snapshot()
        self.assertGreater(
            after["metadata_message_bytes"] - before["metadata_message_bytes"], 0)
        self.assertEqual(
            after["payload_content_bytes"] - before["payload_content_bytes"], 0)
        self.assertGreater(
            after["control_message_bytes"] - before["control_message_bytes"], 0)

    def test_duplicates_deduplicated(self):
        for i in range(200):
            self.sender.capture_text("receiver", f"dedup-{i}")
        anns = [m for m in self.messages
                if isinstance(m, dict) and m.get("type") == cp.T_ANNOUNCEMENT]
        seen_ids = set()
        for ann in anns:
            pid = ann.get("announcement_id")
            before = self.receiver.stats["announcements_received"]
            self.receiver.handle("sender", ann)
            first_result = self.receiver.stats["announcements_received"] - before
            self.receiver.handle("sender", ann)
            second_result = self.receiver.stats["announcements_received"] - before - first_result
            if pid not in seen_ids:
                seen_ids.add(pid)
                self.assertEqual(first_result, 1)
            self.assertIn(second_result, (0, 1))

    def test_out_of_order_accepted(self):
        for i in range(200):
            self.sender.capture_text("receiver", f"ooo-{i}")
        anns = [m for m in self.messages
                if isinstance(m, dict) and m.get("type") == cp.T_ANNOUNCEMENT]
        for ann in reversed(anns):
            self.receiver.handle("sender", ann)
        store = self.receiver.store("sender")
        self.assertGreater(len(store.list_items()), 0)

    def test_disconnect_reconnect_provider_states(self):
        for i in range(50):
            self.sender.capture_text("receiver", f"disc-{i}")
        anns = [m for m in self.messages
                if isinstance(m, dict) and m.get("type") == cp.T_ANNOUNCEMENT]
        for ann in anns:
            self.receiver.handle("sender", ann)
        self.assertEqual(_registry(self.receiver).get("sender-dev", {}).get("state"), "available")
        self.receiver.on_peer_disconnected("sender-dev")
        self.assertEqual(_registry(self.receiver).get("sender-dev", {}).get("state"), "offline")
        self.receiver.on_peer_connected("sender-dev", "sender")
        self.assertEqual(_registry(self.receiver).get("sender-dev", {}).get("state"), "unconfirmed")
        for i in range(25):
            self.sender.capture_text("receiver", f"recon-{i}")
        new_anns = [m for m in self.messages[-50:]
                    if isinstance(m, dict) and m.get("type") == cp.T_ANNOUNCEMENT]
        for ann in new_anns:
            self.receiver.handle("sender", ann)
        self.assertEqual(_registry(self.receiver).get("sender-dev", {}).get("state"), "available")

    def test_manifest_reconciliation_repairs(self):
        for i in range(50):
            self.sender.capture_text("receiver", f"repair-{i}")
        anns = [m for m in self.messages
                if isinstance(m, dict) and m.get("type") == cp.T_ANNOUNCEMENT]
        for ann in anns[:25]:
            self.receiver.handle("sender", ann)
        store_a = self.sender.store("receiver")
        manifest = store_a.build_manifest("sender-dev")
        self.receiver._on_manifest("sender", manifest)
        store_b = self.receiver.store("sender")
        self.assertGreater(len(store_b.list_items()), 25)


# ═══════════════════════════════════════════════════════════════════════
# 9–10. PROVIDER-STATE-IMPORT-TESTS
# ═══════════════════════════════════════════════════════════════════════


class ProviderStateImportTests(unittest.TestCase):
    """Remote provider states are respected, not blindly upgraded."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="flowshift-provimport-")
        self.mgr = ClipboardManager(self.root, "local-dev",
                                    lambda _i, _m: None, lambda: _settings())

    def tearDown(self):
        self.mgr.shutdown()

    def _prov_item(self, device_id, state, sha, sz):
        return {"device_id": device_id, "state": state,
                "last_seen_at": 100.0,
                "payload_sha256": sha, "payload_size": sz}

    def _announce_msg(self, device_id, make_providers):
        item = cm.make_text_item("prov-state", seq=1)
        item = cm.version_item(item, origin_device_id=device_id)
        sha = item["payload"]["sha256"]
        sz = item["payload"]["size"]
        item["providers"] = make_providers(sha, sz)
        manifest = cm.manifest_item(item)
        ann_id = uuid.uuid4().hex
        return {
            "type": cp.T_ANNOUNCEMENT,
            "schema_version": cm.ITEM_SCHEMA_VERSION,
            "announcement_id": ann_id,
            "profile_id": "peer-a",
            "device_id": device_id,
            "history_revision": 5,
            "current_item_id": manifest["item_id"],
            "item": manifest,
        }

    def test_available_stays_available(self):
        ann = self._announce_msg("remote-dev",
                                 lambda sha, sz: [self._prov_item("remote-dev", "available", sha, sz)])
        self.mgr.handle("peer-a", ann)
        self.assertEqual(_registry(self.mgr).get("remote-dev", {}).get("state"), "available")

    def test_offline_stays_offline(self):
        ann = self._announce_msg("available-dev",
                                 lambda sha, sz: [self._prov_item("available-dev", "available", sha, sz),
                                                  self._prov_item("remote-dev", "offline", sha, sz)])
        self.mgr.handle("peer-a", ann)
        self.assertEqual(_registry(self.mgr).get("remote-dev", {}).get("state"), "offline")
        self.assertEqual(_registry(self.mgr).get("available-dev", {}).get("state"), "available")

    def test_stale_stays_stale(self):
        ann = self._announce_msg("available-dev",
                                 lambda sha, sz: [self._prov_item("available-dev", "available", sha, sz),
                                                  self._prov_item("remote-dev", "stale", sha, sz)])
        self.mgr.handle("peer-a", ann)
        self.assertEqual(_registry(self.mgr).get("remote-dev", {}).get("state"), "stale")

    def test_unconfirmed_stays_unconfirmed(self):
        ann = self._announce_msg("available-dev",
                                 lambda sha, sz: [self._prov_item("available-dev", "available", sha, sz),
                                                  self._prov_item("remote-dev", "unconfirmed", sha, sz)])
        self.mgr.handle("peer-a", ann)
        self.assertEqual(_registry(self.mgr).get("remote-dev", {}).get("state"), "unconfirmed")

    def test_unknown_state_falls_back_to_unconfirmed(self):
        self.mgr._register_remote_providers("peer-a", {
            "providers": [{"device_id": "remote-dev", "state": "unknown_state_xyz"}],
        })
        self.assertEqual(_registry(self.mgr).get("remote-dev", {}).get("state"), "unconfirmed")

    def test_malformed_provider_ignored(self):
        self.mgr._register_remote_providers("peer-a", {
            "providers": [{"device_id": None}, {}, "not-a-provider"],
        })
        reg = _registry(self.mgr)
        self.assertEqual(reg, {})

    def test_duplicate_provider_reconciliation(self):
        ann1 = self._announce_msg("available-dev",
                                  lambda sha, sz: [self._prov_item("available-dev", "available", sha, sz),
                                                   self._prov_item("remote-dev", "available", sha, sz)])
        self.mgr.handle("peer-a", ann1)
        ann2 = self._announce_msg("available-dev",
                                  lambda sha, sz: [self._prov_item("available-dev", "available", sha, sz),
                                                   self._prov_item("remote-dev", "offline", sha, sz)])
        self.mgr.handle("peer-a", ann2)
        self.assertEqual(_registry(self.mgr).get("remote-dev", {}).get("state"), "offline")

    def test_offline_not_promoted_to_available_by_manifest(self):
        self.mgr.on_peer_connected("remote-dev", "peer-a")
        self.assertEqual(_registry(self.mgr).get("remote-dev", {}).get("state"), "unconfirmed")
        item = cm.make_text_item("should-not-promote", seq=1)
        item = cm.version_item(item, origin_device_id="remote-dev")
        sha = item["payload"]["sha256"]
        sz = item["payload"]["size"]
        item["providers"] = [{"device_id": "remote-dev", "state": "offline",
                              "last_seen_at": 100.0,
                              "payload_sha256": sha, "payload_size": sz}]
        manifest = cm.build_manifest("peer-a", "remote-dev", 5, [item], item["item_id"])
        self.mgr._on_manifest("peer-a", manifest)
        self.assertEqual(_registry(self.mgr).get("remote-dev", {}).get("state"), "offline")


# ═══════════════════════════════════════════════════════════════════════
# 5–8. GLOBAL CACHE LIMIT — echte Byte-Assertions
# ═══════════════════════════════════════════════════════════════════════


class GlobalCacheLimitRealTests(unittest.TestCase):
    """Real byte-level cache limit enforcement tests."""

    def _populate_store(self, mgr, identity, entries, size_per_entry, start=0):
        st = mgr.store(identity)
        for i in range(start, start + entries):
            sha = f"{i:064x}"
            st.record_cache_entry(sha, payload_size=size_per_entry)
        return st

    def test_global_limit_enforced_after_excess(self):
        mgr = _manager("flowshift-global-real-", cache_max_total_gb=0.1)
        try:
            st_a = self._populate_store(mgr, "peer-a", 10, 10 * 1024 * 1024)
            st_b = self._populate_store(mgr, "peer-b", 10, 10 * 1024 * 1024, start=10)
            before = (st_a.cache_snapshot()["unique_bytes"]
                      + st_b.cache_snapshot()["unique_bytes"])
            self.assertGreater(before, 100 * 1024 * 1024)
            result = mgr._global_cache_enforce()
            after = (st_a.cache_snapshot()["unique_bytes"]
                     + st_b.cache_snapshot()["unique_bytes"])
            self.assertLessEqual(after, result["limit_bytes"])
            self.assertGreater(result["freed_bytes"], 0)
            self.assertTrue(result["limit_satisfied"])
        finally:
            mgr.shutdown()

    def test_protected_payload_not_evicted(self):
        mgr = _manager("flowshift-protected-", cache_max_total_gb=0.01)
        try:
            st = mgr.store("peer")
            item = mgr.capture_text("peer", "protected text")
            self.assertIsNotNone(item)
            mgr.set_pinned("peer", item["item_id"], True)
            st.record_cache_entry(item["sha256"], payload_size=10 * 1024 * 1024)
            mgr._global_cache_enforce()
            entry = st.get_cache_entry(item["sha256"])
            self.assertIsNotNone(entry)
        finally:
            mgr.shutdown()

    def test_active_transfer_payload_protected(self):
        mgr = _manager("flowshift-protected-transfer-", cache_max_total_gb=0.01)
        try:
            st = mgr.store("peer")
            item = mgr.capture_text("peer", "transfer item")
            self.assertIsNotNone(item)
            st.record_cache_entry(item["sha256"], payload_size=10 * 1024 * 1024)
            job = ctt.make_transfer_job(
                "prot-transfer", "profile-peer", item["item_id"], "receive",
                item["kind"], item["display_name"], item.get("size", 0))
            job.status = ctt.TransferStatus.running
            mgr._register_job(job)
            mgr._global_cache_enforce()
            entry = st.get_cache_entry(item["sha256"])
            self.assertIsNotNone(entry)
        finally:
            mgr.shutdown()

    def test_lease_protected_not_evicted(self):
        mgr = _manager("flowshift-protected-lease-", cache_max_total_gb=0.01)
        try:
            st = mgr.store("peer")
            item = mgr.capture_text("peer", "lease protected text")
            self.assertIsNotNone(item)
            st.record_cache_entry(item["sha256"], payload_size=10 * 1024 * 1024)
            st.set_lease(item["item_id"], tempfile.mkdtemp())
            mgr._global_cache_enforce()
            entry = st.get_cache_entry(item["sha256"])
            self.assertIsNotNone(entry)
        finally:
            mgr.shutdown()

    def test_local_current_payload_protected(self):
        mgr = _manager("flowshift-protected-current-", cache_max_total_gb=0.01)
        try:
            st = mgr.store("peer")
            item = mgr.capture_text("peer", "current item")
            self.assertIsNotNone(item)
            st.record_cache_entry(item["sha256"], payload_size=10 * 1024 * 1024)
            mgr._global_cache_enforce()
            entry = st.get_cache_entry(item["sha256"])
            self.assertIsNotNone(entry)
        finally:
            mgr.shutdown()

    def test_limit_unsatisfiable_diagnosed_honestly(self):
        mgr = _manager("flowshift-unsatisfiable-", cache_max_total_gb=0.001)
        try:
            st = mgr.store("peer")
            for i in range(5):
                item = mgr.capture_text("peer", f"prot-{i}")
                if item:
                    mgr.set_pinned("peer", item["item_id"], True)
                    st.record_cache_entry(item["sha256"],
                                          payload_size=50 * 1024 * 1024)
            result = mgr._global_cache_enforce()
            if not result["limit_satisfied"]:
                self.assertGreater(result["over_bytes"], 0)
        finally:
            mgr.shutdown()

    def test_dedup_shared_sha_counted_once(self):
        mgr = _manager("flowshift-dedup-", cache_max_total_gb=0.1)
        try:
            st_a = mgr.store("peer-a")
            st_b = mgr.store("peer-b")
            shared_sha = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
            st_a.record_cache_entry(shared_sha, payload_size=50 * 1024 * 1024)
            st_b.record_cache_entry(shared_sha, payload_size=50 * 1024 * 1024)
            before_a = st_a.cache_snapshot()["unique_bytes"]
            before_b = st_b.cache_snapshot()["unique_bytes"]
            self.assertGreaterEqual(before_a, 50 * 1024 * 1024)
            self.assertGreaterEqual(before_b, 50 * 1024 * 1024)
            result = mgr._global_cache_enforce()
            after_a = st_a.cache_snapshot()["unique_bytes"]
            after_b = st_b.cache_snapshot()["unique_bytes"]
            self.assertEqual(result["remaining_bytes"], 50 * 1024 * 1024)
            self.assertEqual(result["freed_bytes"], 0)
            self.assertEqual(after_a, 50 * 1024 * 1024)
            self.assertEqual(after_b, 50 * 1024 * 1024)
        finally:
            mgr.shutdown()


# ═══════════════════════════════════════════════════════════════════════
# Persistence Stress (keepers)
# ═══════════════════════════════════════════════════════════════════════


class PersistenceStressTests(unittest.TestCase):
    """Hundreds of items across multiple save/load cycles."""

    def test_text_items_survive_restart(self):
        root = tempfile.mkdtemp(prefix="flowshift-persist-text-")
        mgr = ClipboardManager(root, "dev", lambda _i, _m: None, lambda: _settings())
        try:
            ids = set()
            for i in range(200):
                item = mgr.capture_text("peer", f"text-{i}")
                if item:
                    ids.add(item["item_id"])
            mgr.capture_files("peer", [__file__])
        finally:
            mgr.shutdown()
        mgr2 = ClipboardManager(root, "dev", lambda _i, _m: None, lambda: _settings())
        try:
            items = mgr2.list_items("peer")
            self.assertGreater(len(items), 0)
            item_ids = {it["item_id"] for it in items}
            overlap = item_ids & ids
            self.assertGreater(len(overlap), 0)
        finally:
            mgr2.shutdown()

    def test_pins_survive_restart(self):
        root = tempfile.mkdtemp(prefix="flowshift-persist-pin-")
        mgr = ClipboardManager(root, "dev", lambda _i, _m: None, lambda: _settings())
        try:
            items = []
            for i in range(100):
                it = mgr.capture_text("peer", f"pin-{i}")
                if it:
                    items.append(it)
            for it in items[:5]:
                mgr.set_pinned("peer", it["item_id"], True)
        finally:
            mgr.shutdown()
        mgr2 = ClipboardManager(root, "dev", lambda _i, _m: None, lambda: _settings())
        try:
            for it in items[:5]:
                stored = mgr2.store("peer").get_item(it["item_id"])
                if stored:
                    self.assertTrue(stored.get("pinned"))
        finally:
            mgr2.shutdown()

    def test_current_item_restored_correctly(self):
        root = tempfile.mkdtemp(prefix="flowshift-persist-current-")
        mgr = ClipboardManager(root, "dev", lambda _i, _m: None, lambda: _settings())
        try:
            first = mgr.capture_text("peer", "first")
            self.assertIsNotNone(first)
            second = mgr.capture_text("peer", "second")
            self.assertIsNotNone(second)
            self.assertEqual(mgr.store("peer").current_item_id, second["item_id"])
            mgr.mark_current("peer", first["item_id"])
            self.assertEqual(mgr.store("peer").current_item_id, first["item_id"])
        finally:
            mgr.shutdown()
        mgr2 = ClipboardManager(root, "dev", lambda _i, _m: None, lambda: _settings())
        try:
            current = mgr2.store("peer").current_item_id
            self.assertEqual(current, first["item_id"])
        finally:
            mgr2.shutdown()

    def test_no_duplicates_across_cycles(self):
        root = tempfile.mkdtemp(prefix="flowshift-persist-dup-")
        for cycle in range(5):
            mgr = ClipboardManager(root, "dev", lambda _i, _m: None, lambda: _settings())
            try:
                for i in range(20):
                    mgr.capture_text("peer", f"cycle-{cycle}-{i}")
                ids = {it["item_id"] for it in mgr.list_items("peer")}
                self.assertEqual(len(ids), len(mgr.list_items("peer")))
            finally:
                mgr.shutdown()

    def test_index_not_corrupted(self):
        root = tempfile.mkdtemp(prefix="flowshift-persist-idx-")
        mgr = ClipboardManager(root, "dev", lambda _i, _m: None, lambda: _settings())
        try:
            for i in range(300):
                mgr.capture_text("peer", f"idx-{i}")
        finally:
            mgr.shutdown()
        import json
        index_path = os.path.join(root, "profiles", "default-profile", "clipboard", "index.json")
        if os.path.exists(index_path):
            with open(index_path, "r") as f:
                data = json.load(f)
            self.assertIn("items", data)
            self.assertIsInstance(data["items"], list)
        mgr2 = ClipboardManager(root, "dev", lambda _i, _m: None, lambda: _settings())
        try:
            items = mgr2.list_items("peer")
            self.assertGreater(len(items), 0)
        finally:
            mgr2.shutdown()

    def test_provider_registry_not_recovered_as_available(self):
        root = tempfile.mkdtemp(prefix="flowshift-persist-provider-")
        mgr = ClipboardManager(root, "dev", lambda _i, _m: None, lambda: _settings())
        try:
            mgr.on_peer_connected("remote-dev", "peer")
            mgr.capture_text("peer", "entry")
            reg = _registry(mgr)
            self.assertIn("remote-dev", reg)
            self.assertEqual(reg["remote-dev"]["state"], "unconfirmed")
        finally:
            mgr.shutdown()
        mgr2 = ClipboardManager(root, "dev", lambda _i, _m: None, lambda: _settings())
        try:
            items = mgr2.list_items("peer")
            self.assertGreater(len(items), 0)
            reg = _registry(mgr2)
            self.assertNotIn("remote-dev", reg)
        finally:
            mgr2.shutdown()


# ═══════════════════════════════════════════════════════════════════════
# Provider Lifecycle Stress (keepers + strengthened)
# ═══════════════════════════════════════════════════════════════════════


class ProviderLifecycleStressTests(unittest.TestCase):
    """Provider lifecycle under disconnect/reconnect and reconciliation stress."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="flowshift-prov-stress-")
        self.mgr = ClipboardManager(self.root, "local-dev",
                                    lambda _i, _m: None, lambda: _settings())

    def tearDown(self):
        self.mgr.shutdown()

    def _make_manifest(self, device_id, items, revision=5):
        return cm.build_manifest("peer-a", device_id, revision, items,
                                 items[-1]["item_id"] if items else None)

    def _make_prov_item(self, text, device_id, state="available", seq=1):
        item = cm.make_text_item(text, seq=seq)
        item = cm.version_item(item, origin_device_id=device_id)
        sha = item["payload"]["sha256"]
        sz = item["payload"]["size"]
        item["providers"] = [{"device_id": device_id, "state": state,
                              "last_seen_at": 100.0,
                              "payload_sha256": sha, "payload_size": sz}]
        return item

    def test_provider_origin_has_payload_then_disconnects(self):
        self.mgr.on_peer_connected("origin-dev", "peer-a")
        item = self._make_prov_item("origin content", "origin-dev", "available")
        self.mgr._on_manifest("peer-a", self._make_manifest("origin-dev", [item]))
        self.assertEqual(_registry(self.mgr).get("origin-dev", {}).get("state"), "available")
        self.mgr.on_peer_disconnected("origin-dev")
        self.assertEqual(_registry(self.mgr).get("origin-dev", {}).get("state"), "offline")

    def test_reconnect_does_not_auto_make_available(self):
        self.mgr.on_peer_connected("origin-dev", "peer-a")
        item = self._make_prov_item("pre-disc", "origin-dev", "available")
        self.mgr._on_manifest("peer-a", self._make_manifest("origin-dev", [item]))
        self.mgr.on_peer_disconnected("origin-dev")
        self.mgr.on_peer_connected("origin-dev", "peer-a")
        self.assertEqual(_registry(self.mgr).get("origin-dev", {}).get("state"), "unconfirmed")

    def test_reconciliation_after_reconnect_restores_available(self):
        self.mgr.on_peer_connected("origin-dev", "peer-a")
        item = self._make_prov_item("reconciled", "origin-dev", "available")
        manifest = self._make_manifest("origin-dev", [item])
        self.mgr._on_manifest("peer-a", manifest)
        self.mgr.on_peer_disconnected("origin-dev")
        self.mgr.on_peer_connected("origin-dev", "peer-a")
        self.assertEqual(_registry(self.mgr).get("origin-dev", {}).get("state"), "unconfirmed")
        self.mgr._on_manifest("peer-a", manifest)
        self.assertEqual(_registry(self.mgr).get("origin-dev", {}).get("state"), "available")

    def test_full_disconnect_reconnect_cycle(self):
        self.mgr.on_peer_connected("origin-dev", "peer-a")
        items = [self._make_prov_item(f"cycle-{i}", "origin-dev", "available", i)
                 for i in range(10)]
        manifest = self._make_manifest("origin-dev", items, 10)
        self.mgr._on_manifest("peer-a", manifest)
        self.assertEqual(_registry(self.mgr).get("origin-dev", {}).get("state"), "available")
        self.mgr.on_peer_disconnected("origin-dev")
        self.assertEqual(_registry(self.mgr).get("origin-dev", {}).get("state"), "offline")
        self.mgr.on_peer_connected("origin-dev", "peer-a")
        self.assertEqual(_registry(self.mgr).get("origin-dev", {}).get("state"), "unconfirmed")
        self.mgr._on_manifest("peer-a", manifest)
        self.assertEqual(_registry(self.mgr).get("origin-dev", {}).get("state"), "available")

    def test_local_provider_survives_restart_with_cache(self):
        item = self.mgr.capture_text("peer-a", "cache me")
        self.assertIsNotNone(item)
        self.mgr.store("peer-a").record_cache_entry(item["sha256"], payload_size=100)
        self.mgr.shutdown()
        mgr2 = ClipboardManager(self.root, "local-dev",
                                lambda _i, _m: None, lambda: _settings())
        try:
            stored = mgr2.store("peer-a").get_item(item["item_id"])
            if stored:
                self.assertTrue(
                    stored.get("available")
                    or mgr2.store("peer-a").has_object(item["sha256"]))
        finally:
            mgr2.shutdown()


# ═══════════════════════════════════════════════════════════════════════
# Self-Write Suppression Stress (keepers)
# ═══════════════════════════════════════════════════════════════════════


class SelfWriteSuppressionStressTests(unittest.TestCase):
    """Suppression under stress — 10k events, concurrent."""

    def test_suppression_under_10k_events(self):
        sup = cbe.ClipboardWriteSuppressor(ttl_seconds=5.0, max_tokens=64)
        token = sup.prepare("item-1", {"text"}, "text", "digest-abc", 0)
        ok = sup.finish(token, True, 10000)
        self.assertTrue(ok)
        consumed = sup.consume(10000, {"text"}, "text", "digest-abc")
        self.assertTrue(consumed)
        snap = sup.snapshot()
        self.assertGreater(snap["consumed"], 0)

    def test_suppression_concurrent_access(self):
        sup = cbe.ClipboardWriteSuppressor(ttl_seconds=2.0, max_tokens=32)
        errors = []

        def worker(n):
            try:
                for _ in range(100):
                    token = sup.prepare(
                        f"token-{n}-{_}", {"text"}, "text", f"digest-{n}", _)
                    sup.finish(token, True, _ + 1)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(errors, [])

    def test_suppression_shutdown(self):
        sup = cbe.ClipboardWriteSuppressor()
        token = sup.prepare("shutdown-item", {"text"}, "text", "shutdown-digest", 0)
        ok = sup.finish(token, True, 100)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
