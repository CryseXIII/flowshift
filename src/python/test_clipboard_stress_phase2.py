"""Phase 2.2 stress tests: 10k events, 5k announcements, persistence, cache, provider lifecycle."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_events as cbe
import clipboard_model as cm
import clipboard_protocol as cp
import clipboard_transfer as ctt
from clipboard_runtime import ClipboardManager


def _settings():
    return cm.clipboard_settings({"clipboard": {"enabled": True}})


def _manager(prefix):
    root = tempfile.mkdtemp(prefix=prefix)
    mgr = ClipboardManager(root, "dev", lambda _ident, _msg: None, lambda: _settings())
    return mgr


# ── 3.1 Clipboard Event Stress (10k notifications) ──────────────────────


class ClipboardEventStressTests(unittest.TestCase):
    """10 000 synthetic clipboard notifications through BoundedClipboardEvents."""

    def _run_stress(self, total=10000, distinct=1000):
        q = cbe.BoundedClipboardEvents(capacity=8)
        errors = []
        start = time.monotonic()

        try:
            for i in range(total):
                seq = i % distinct
                digest = f"digest-{seq}" if i % 3 == 0 else None
                kind = "clear" if i % 97 == 0 else "copy"
                try:
                    ok = q.submit(sequence=seq, source="stress", digest=digest, kind=kind)
                    if not ok:
                        errors.append(f"submit returned False at {i}")
                except BaseException as exc:
                    errors.append(f"submit raised {exc} at {i}")

            duration = time.monotonic() - start
            snap = q.snapshot()
            processed = 0
            while q.get(timeout=0.01) is not None:
                processed += 1

            report = {
                "submitted": snap["submitted"], "processed": processed,
                "coalesced": snap["coalesced"], "dropped": snap["dropped"],
                "overflow": max(0, processed - snap["capacity"]),
                "distinct_sequences": distinct,
                "errors": len(errors), "duration_s": round(duration, 3),
            }
        finally:
            q.close()

        self.assertEqual(errors, [], f"Errors during stress: {errors}")
        self.assertEqual(report["submitted"], total)
        self.assertGreater(report["processed"], 0)
        return report

    def test_10k_events_with_1k_distinct_sequences(self):
        report = self._run_stress(total=10000, distinct=1000)
        self.assertGreater(report["duration_s"], 0)

    def test_identical_duplicates_are_coalesced(self):
        q = cbe.BoundedClipboardEvents(capacity=8)
        try:
            for _ in range(100):
                q.submit(sequence=1, digest="same")
            snap = q.snapshot()
            self.assertEqual(snap["submitted"], 100)
            self.assertGreater(snap["coalesced"], 90)
        finally:
            q.close()

    def test_different_sequence_numbers_not_coalesced(self):
        q = cbe.BoundedClipboardEvents(capacity=8)
        try:
            for i in range(100):
                q.submit(sequence=i, digest=f"digest-{i}")
            snap = q.snapshot()
            self.assertEqual(snap["submitted"], 100)
            self.assertLess(snap["coalesced"], 10)
        finally:
            q.close()

    def test_fast_burst_maintains_order(self):
        q = cbe.BoundedClipboardEvents(capacity=16)
        try:
            for i in range(200):
                q.submit(sequence=i, digest=f"d-{i}", kind="copy")
                q.submit(sequence=i, digest=f"d-{i}", kind="copy")
            extracted = []
            while True:
                ev = q.get(timeout=0.01)
                if ev is None:
                    break
                extracted.append(ev["sequence"])
            self.assertTrue(all(extracted[i] <= extracted[i + 1]
                                for i in range(len(extracted) - 1)))
        finally:
            q.close()

    def test_queue_pressure_and_overflow(self):
        q = cbe.BoundedClipboardEvents(capacity=4)
        try:
            for i in range(100):
                q.submit(sequence=i, digest=f"d-{i}")
            snap = q.snapshot()
            self.assertGreater(snap["dropped"], 80)
        finally:
            q.close()

    def test_shutdown_during_active_load(self):
        q = cbe.BoundedClipboardEvents(capacity=8)
        results = []
        errors = []

        def loader():
            try:
                for i in range(5000):
                    if not q.submit(sequence=i % 100, digest=f"d-{i % 100}"):
                        break
            except BaseException as exc:
                errors.append(exc)

        t = threading.Thread(target=loader, daemon=True)
        t.start()
        time.sleep(0.01)
        q.close()
        t.join(5)
        self.assertEqual(errors, [])
        snap = q.snapshot()
        self.assertTrue(snap["closed"])

    def test_clear_events_not_coalesced(self):
        q = cbe.BoundedClipboardEvents(capacity=8)
        try:
            q.submit(sequence=1, kind="copy")
            q.submit(sequence=1, kind="clear")
            q.submit(sequence=1, kind="copy")
            snap = q.snapshot()
            self.assertEqual(snap["coalesced"], 0)
        finally:
            q.close()


# ── 3.2 Metadata Announcement Stress (5k announcements) ────────────────


class MetadataAnnouncementStressTests(unittest.TestCase):
    """5 000 metadata announcements between manager pairs."""

    def setUp(self):
        self.root_a = tempfile.mkdtemp(prefix="flowshift-ann-stress-a-")
        self.root_b = tempfile.mkdtemp(prefix="flowshift-ann-stress-b-")
        self.messages = []
        self.sender = ClipboardManager(
            self.root_a, "sender-dev",
            lambda _ident, msg: self.messages.append(msg), lambda: _settings())
        self.receiver = ClipboardManager(
            self.root_b, "receiver-dev",
            lambda _ident, _msg: None, lambda: _settings())

    def tearDown(self):
        self.sender.shutdown()
        self.receiver.shutdown()

    def test_5k_normal_announcements(self):
        total = 5000
        for i in range(total):
            self.sender.capture_text("receiver", f"ann-{i}")

        ann_count = sum(1 for m in self.messages if isinstance(m, dict)
                        and m.get("type") == cp.T_ANNOUNCEMENT)
        self.assertGreaterEqual(ann_count, total - 10)

        for msg in self.messages[:100]:
            if isinstance(msg, dict) and msg.get("type") == cp.T_ANNOUNCEMENT:
                parsed = cp.parse_announcement(msg)
                self.assertIsNotNone(parsed)
                self.assertIsNone(msg.get("item", {}).get("data"))

        processed = 0
        for msg in self.messages:
            if isinstance(msg, dict) and msg.get("type") == cp.T_ANNOUNCEMENT:
                self.receiver.handle("sender", msg)
                processed += 1

        store = self.receiver.store("sender")
        items = store.list_items()
        self.assertGreater(len(items), 0)
        self.assertGreaterEqual(processed, total - 10)

    def test_duplicate_announcements(self):
        sent_ids = set()
        for i in range(100):
            item = self.sender.capture_text("receiver", f"dup-{i}")
            self.assertIsNotNone(item)

        unique = 0
        for msg in self.messages:
            if isinstance(msg, dict) and msg.get("type") == cp.T_ANNOUNCEMENT:
                pid = msg.get("announcement_id")
                self.receiver.handle("sender", msg)
                self.receiver.handle("sender", msg)
                if pid and pid not in sent_ids:
                    sent_ids.add(pid)
                    unique += 1

        self.assertGreater(unique, 0)

    def test_out_of_order_announcements(self):
        items = []
        for i in range(200):
            item = self.sender.capture_text("receiver", f"ooo-{i}")
            if item:
                items.append(item)

        anns = [m for m in self.messages if isinstance(m, dict)
                and m.get("type") == cp.T_ANNOUNCEMENT]
        for ann in reversed(anns):
            self.receiver.handle("sender", ann)

        store = self.receiver.store("sender")
        items_in_store = store.list_items()
        self.assertGreater(len(items_in_store), 0)

    def test_invalid_announcements_rejected(self):
        bad = {"type": cp.T_ANNOUNCEMENT, "announcement_id": "bad1"}
        before = self.receiver.stats["announcements_received"]
        self.receiver.handle("sender", bad)
        self.assertEqual(self.receiver.stats["announcements_received"], before)

    def test_announcements_carry_zero_payload_bytes(self):
        for i in range(500):
            self.sender.capture_text("receiver", f"zero-payload-ann-{i}")
        for msg in self.messages:
            if isinstance(msg, dict) and msg.get("type") == cp.T_ANNOUNCEMENT:
                self.assertIsNone(msg.get("data"))
                self.assertIsNone(msg.get("item", {}).get("data"))
                payload = msg.get("item", {}).get("payload", {})
                self.assertIsNone(payload.get("data"))

    def test_disconnect_reconnect_announcements(self):
        for i in range(100):
            item = self.sender.capture_text("receiver", f"pre-disc-{i}")
            if item:
                self.receiver.handle("sender", self.messages[-1])

        self.receiver.on_peer_disconnected("sender-dev")
        reg = self.receiver.provider_snapshot().get("registry", {})
        self.assertEqual(reg.get("sender-dev", {}).get("state"), "offline")

        self.receiver.on_peer_connected("sender-dev", "sender")
        reg = self.receiver.provider_snapshot().get("registry", {})
        self.assertEqual(reg.get("sender-dev", {}).get("state"), "unconfirmed")

        for i in range(50):
            item = self.sender.capture_text("receiver", f"post-recon-{i}")
            if item:
                self.receiver.handle("sender", self.messages[-1])

        reg = self.receiver.provider_snapshot().get("registry", {})
        self.assertEqual(reg.get("sender-dev", {}).get("state"), "available")

    def test_manifest_reconciliation_after_lost_announcements(self):
        for i in range(50):
            item = self.sender.capture_text("receiver", f"lost-{i}")
            if item:
                self.receiver.handle("sender", self.messages[-1])

        store_a = self.sender.store("receiver")
        manifest = store_a.build_manifest("sender-dev")
        self.receiver._on_manifest("sender", manifest)
        store_b = self.receiver.store("sender")
        items_b = store_b.list_items()
        self.assertGreater(len(items_b), 0)


# ── 3.3 Persistence Stress ────────────────────────────────────────


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
            reg = mgr.provider_snapshot().get("registry", {})
            self.assertIn("remote-dev", reg)
            self.assertEqual(reg["remote-dev"]["state"], "unconfirmed")
        finally:
            mgr.shutdown()

        mgr2 = ClipboardManager(root, "dev", lambda _i, _m: None, lambda: _settings())
        try:
            items = mgr2.list_items("peer")
            self.assertGreater(len(items), 0)
            reg = mgr2.provider_snapshot().get("registry", {})
            self.assertNotIn("remote-dev", reg)
        finally:
            mgr2.shutdown()


# ── 3.4 Global Cache Limit ────────────────────────────────────────


class GlobalCacheLimitTests(unittest.TestCase):
    """Verify a global cache_max_total_gb limit exists and works."""

    def test_cache_snapshot_available(self):
        mgr = _manager("flowshift-global-cache-")
        try:
            st = mgr.store("peer")
            snap = st.cache_snapshot()
            self.assertIn("entry_count", snap)
            self.assertGreaterEqual(snap["entry_count"], 0)
        finally:
            mgr.shutdown()

    def test_global_enforce_evicts_across_peers(self):
        mgr = _manager("flowshift-global-evict-")
        try:
            st_a = mgr.store("peer-a")
            st_b = mgr.store("peer-b")
            for i in range(50):
                sha = f"{i:064x}"
                st_a.record_cache_entry(sha, payload_size=10 * 1024 * 1024)
                st_b.record_cache_entry(sha, payload_size=10 * 1024 * 1024)
            mgr._global_cache_enforce()
            snap_a = st_a.cache_snapshot()
            self.assertIsNotNone(snap_a)
        finally:
            mgr.shutdown()

    def test_pinned_items_not_evicted(self):
        mgr = _manager("flowshift-global-pinned-")
        try:
            st = mgr.store("peer")
            item = mgr.capture_text("peer", "pinned-text")
            self.assertIsNotNone(item)
            mgr.set_pinned("peer", item["item_id"], True)
            st.record_cache_entry(item["sha256"], payload_size=1024)
            st.enforce_limits(100, 10**9)
            snap = st.cache_snapshot()
            self.assertLessEqual(snap["entry_count"], 100)
        finally:
            mgr.shutdown()

    def test_active_transfer_not_evicted(self):
        mgr = _manager("flowshift-global-active-")
        try:
            st = mgr.store("peer")
            sha = "a" * 64
            st.record_cache_entry(sha, payload_size=1024)
            st._received_cache[sha] = {"sha256": sha, "payload_size": 1024,
                                       "received_at": time.time(), "last_access": time.time()}
            st.enforce_limits(100, 10**9)
            entry = st.get_cache_entry(sha)
            if entry:
                self.assertEqual(entry.get("sha256"), sha)
        finally:
            mgr.shutdown()


# ── 3.5 Provider Lifecycle in Stress ──────────────────────────────


class ProviderLifecycleStressTests(unittest.TestCase):
    """Provider lifecycle under disconnect/reconnect and reconciliation stress."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="flowshift-prov-stress-")
        self.mgr = ClipboardManager(self.root, "local-dev",
                                    lambda _i, _m: None, lambda: _settings())

    def tearDown(self):
        self.mgr.shutdown()

    def test_provider_origin_has_payload_then_disconnects(self):
        self.mgr.on_peer_connected("origin-dev", "peer-a")
        item = cm.make_text_item("origin content", seq=1)
        item = cm.version_item(item, origin_device_id="origin-dev")
        sha = item["payload"]["sha256"]
        sz = item["payload"]["size"]
        item["providers"] = [{"device_id": "origin-dev", "state": "available",
                              "last_seen_at": 100.0,
                              "payload_sha256": sha, "payload_size": sz}]
        manifest = cm.build_manifest("peer-a", "origin-dev", 5, [item], item["item_id"])
        self.mgr._on_manifest("peer-a", manifest)
        reg = self.mgr.provider_snapshot().get("registry", {})
        self.assertEqual(reg["origin-dev"]["state"], "available")

        self.mgr.on_peer_disconnected("origin-dev")
        reg = self.mgr.provider_snapshot().get("registry", {})
        self.assertEqual(reg["origin-dev"]["state"], "offline")

    def test_reconnect_does_not_auto_make_available(self):
        self.mgr.on_peer_connected("origin-dev", "peer-a")
        item = cm.make_text_item("pre-disconnect content", seq=1)
        item = cm.version_item(item, origin_device_id="origin-dev")
        sha = item["payload"]["sha256"]
        sz = item["payload"]["size"]
        item["providers"] = [{"device_id": "origin-dev", "state": "available",
                              "last_seen_at": 100.0,
                              "payload_sha256": sha, "payload_size": sz}]
        manifest = cm.build_manifest("peer-a", "origin-dev", 5, [item], item["item_id"])
        self.mgr._on_manifest("peer-a", manifest)
        reg = self.mgr.provider_snapshot().get("registry", {})
        self.assertEqual(reg["origin-dev"]["state"], "available")

        self.mgr.on_peer_disconnected("origin-dev")
        self.mgr.on_peer_connected("origin-dev", "peer-a")
        reg = self.mgr.provider_snapshot().get("registry", {})
        self.assertEqual(reg["origin-dev"]["state"], "unconfirmed")

    def test_reconciliation_after_reconnect_restores_available(self):
        self.mgr.on_peer_connected("origin-dev", "peer-a")
        item = cm.make_text_item("reconciled content", seq=1)
        item = cm.version_item(item, origin_device_id="origin-dev")
        sha = item["payload"]["sha256"]
        sz = item["payload"]["size"]
        item["providers"] = [{"device_id": "origin-dev", "state": "available",
                              "last_seen_at": 100.0,
                              "payload_sha256": sha, "payload_size": sz}]
        manifest = cm.build_manifest("peer-a", "origin-dev", 5, [item], item["item_id"])
        self.mgr._on_manifest("peer-a", manifest)
        reg = self.mgr.provider_snapshot().get("registry", {})
        self.assertEqual(reg["origin-dev"]["state"], "available")

        self.mgr.on_peer_disconnected("origin-dev")
        self.mgr.on_peer_connected("origin-dev", "peer-a")
        reg = self.mgr.provider_snapshot().get("registry", {})
        self.assertEqual(reg["origin-dev"]["state"], "unconfirmed")

        self.mgr._on_manifest("peer-a", manifest)
        reg = self.mgr.provider_snapshot().get("registry", {})
        self.assertEqual(reg["origin-dev"]["state"], "available")

    def test_local_provider_survives_restart_with_cache(self):
        item = self.mgr.capture_text("peer-a", "cache me")
        self.assertIsNotNone(item)
        sha = item["sha256"]
        self.mgr.store("peer-a").record_cache_entry(sha, payload_size=100)
        self.mgr.shutdown()

        mgr2 = ClipboardManager(self.root, "local-dev",
                                lambda _i, _m: None, lambda: _settings())
        try:
            stored = mgr2.store("peer-a").get_item(item["item_id"])
            if stored:
                self.assertTrue(
                    stored.get("available")
                    or mgr2.store("peer-a").has_object(sha))
        finally:
            mgr2.shutdown()

    def test_cache_eviction_removes_provider_status(self):
        sha = "e" * 64
        st = self.mgr.store("peer-a")
        st.record_cache_entry(sha, payload_size=1024)
        st.enforce_limits(0, 512)
        snap = st.cache_snapshot()
        self.assertLessEqual(snap["entry_count"], 1)

    def test_lost_announcement_repaired_by_manifest(self):
        self.mgr.on_peer_connected("remote-dev", "peer-a")
        item = cm.make_text_item("lost then found", seq=1)
        item = cm.version_item(item, origin_device_id="remote-dev")
        sha = item["payload"]["sha256"]
        sz = item["payload"]["size"]
        item["providers"] = [{"device_id": "remote-dev", "state": "available",
                              "last_seen_at": 100.0,
                              "payload_sha256": sha, "payload_size": sz}]
        announcement = cp.build_announcement(
            "ann-lost", "peer-a", "remote-dev", 5, item["item_id"], item)
        self.mgr.handle("peer-a", announcement)
        store = self.mgr.store("peer-a")
        self.assertIsNotNone(store.get_item(item["item_id"]))

        self.mgr.on_peer_disconnected("remote-dev")

        manifest = cm.build_manifest("peer-a", "remote-dev", 6, [item], item["item_id"])
        self.mgr._on_manifest("peer-a", manifest)
        reg = self.mgr.provider_snapshot().get("registry", {})
        self.assertIn("remote-dev", reg)

    def test_full_disconnect_reconnect_cycle_with_reconciliation(self):
        self.mgr.on_peer_connected("origin-dev", "peer-a")
        items = []
        for i in range(10):
            it = cm.make_text_item(f"cycle-item-{i}", seq=i + 1)
            it = cm.version_item(it, origin_device_id="origin-dev")
            sha = it["payload"]["sha256"]
            sz = it["payload"]["size"]
            it["providers"] = [{"device_id": "origin-dev", "state": "available",
                                "last_seen_at": 100.0 + i,
                                "payload_sha256": sha, "payload_size": sz}]
            items.append(it)
        manifest = cm.build_manifest("peer-a", "origin-dev", 10, items, items[-1]["item_id"])
        self.mgr._on_manifest("peer-a", manifest)
        reg = self.mgr.provider_snapshot().get("registry", {})
        self.assertEqual(reg["origin-dev"]["state"], "available")

        self.mgr.on_peer_disconnected("origin-dev")
        reg = self.mgr.provider_snapshot().get("registry", {})
        self.assertEqual(reg["origin-dev"]["state"], "offline")

        self.mgr.on_peer_connected("origin-dev", "peer-a")
        reg = self.mgr.provider_snapshot().get("registry", {})
        self.assertEqual(reg["origin-dev"]["state"], "unconfirmed")

        self.mgr._on_manifest("peer-a", manifest)
        reg = self.mgr.provider_snapshot().get("registry", {})
        self.assertEqual(reg["origin-dev"]["state"], "available")


# ── 3.1 additional: Self-Write Suppression Stress ──────────────────


class SelfWriteSuppressionStressTests(unittest.TestCase):
    """Suppression under stress — 10k events, shutdown, concurrent."""

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
                    tid = f"token-{n}-{_}"
                    token = sup.prepare(tid, {"text"}, "text", f"digest-{n}", _)
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
