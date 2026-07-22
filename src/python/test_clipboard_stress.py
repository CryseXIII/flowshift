"""Phase 2.1 stress tests: burst captures, concurrent transfers, race conditions."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_model as cm
import clipboard_transfer as ctt
from clipboard_runtime import ClipboardManager


def _manager(prefix):
    root = tempfile.mkdtemp(prefix=prefix)
    settings = cm.clipboard_settings({"clipboard": {"enabled": True}})
    mgr = ClipboardManager(root, "dev", lambda _ident, _msg: None, lambda: settings)
    return mgr


class ClipboardStressTests(unittest.TestCase):
    def test_burst_of_100_captures_maintains_item_count(self):
        mgr = _manager("flowshift-burst-")
        try:
            for i in range(100):
                mgr.capture_text("peer", f"item-{i}")
            items = mgr.list_items("peer")
            self.assertLessEqual(len(items), 200)
            self.assertGreater(len(items), 0)
        finally:
            mgr.shutdown()

    def test_rapid_alternating_captures_no_crash(self):
        mgr = _manager("flowshift-alternate-")
        try:
            for i in range(50):
                mgr.capture_text("peer-a", f"text-{i}")
                mgr.capture_files("peer-b", [__file__])
            items_a = mgr.list_items("peer-a")
            items_b = mgr.list_items("peer-b")
            self.assertGreater(len(items_a), 0)
            self.assertGreater(len(items_b), 0)
        finally:
            mgr.shutdown()

    def test_concurrent_captures_from_multiple_threads(self):
        mgr = _manager("flowshift-concurrent-")
        errors = []
        lock = threading.Lock()

        def capture_text(n):
            try:
                for _ in range(20):
                    mgr.capture_text("peer", f"thread-{n}")
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=capture_text, args=(i,), daemon=True)
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        mgr.shutdown()

    def test_shutdown_during_active_capture(self):
        mgr = _manager("flowshift-shutdown-race-")
        try:
            def keeper():
                for i in range(50):
                    mgr.capture_text("peer", f"keep-{i}")
                    time.sleep(0.001)
            t = threading.Thread(target=keeper, daemon=True)
            t.start()
            time.sleep(0.01)
            mgr.shutdown()
            t.join(5)
        except BaseException:
            mgr.shutdown()
            raise

    def test_many_peers_each_with_items(self):
        mgr = _manager("flowshift-multi-peer-")
        try:
            for p in range(20):
                ident = f"peer-{p}"
                for i in range(5):
                    mgr.capture_text(ident, f"item-{i}")
            for p in range(20):
                items = mgr.list_items(f"peer-{p}")
                self.assertGreater(len(items), 0, f"peer-{p} has no items")
        finally:
            mgr.shutdown()

    def test_capture_and_delete_alternating(self):
        mgr = _manager("flowshift-alt-del-")
        try:
            ids = []
            for i in range(30):
                item = mgr.capture_text("peer", f"item-{i}")
                if item:
                    ids.append(item["item_id"])
            for iid in ids:
                mgr.delete_item("peer", iid)
            self.assertEqual(mgr.list_items("peer"), [])
        finally:
            mgr.shutdown()

    def test_transfer_job_under_stress(self):
        sent = []

        def send_fn(ident, msg):
            sent.append((ident, msg))

        root_a = tempfile.mkdtemp(prefix="flowshift-stress-src-")
        root_b = tempfile.mkdtemp(prefix="flowshift-stress-dst-")
        settings = cm.clipboard_settings({"clipboard": {"enabled": True}})
        src = ClipboardManager(root_a, "src-dev", send_fn, lambda: settings)
        dst = ClipboardManager(root_b, "dst-dev", send_fn, lambda: settings)
        try:
            for i in range(30):
                src.capture_text("peer", f"stress-{i}")
            items = src.list_items("peer")
            for it in items[:10]:
                job = ctt.make_transfer_job(
                    f"job-{it['item_id']}", "peer", it["item_id"], "send",
                    it["kind"], it["display_name"], it.get("size", 0))
                src._register_job(job)
                job2 = ctt.make_transfer_job(
                    f"recv-{it['item_id']}", "src-dev", it["item_id"], "receive",
                    it["kind"], it["display_name"], it.get("size", 0))
                dst._register_job(job2)
            diag_src = src.diagnostics("peer")
            diag_dst = dst.diagnostics("src-dev")
            self.assertIn("store", diag_src)
            self.assertIn("store", diag_dst)
        finally:
            src.shutdown()
            dst.shutdown()
            import shutil
            shutil.rmtree(root_a, ignore_errors=True)
            shutil.rmtree(root_b, ignore_errors=True)

    def test_cache_under_repeated_receive(self):
        mgr = _manager("flowshift-cache-stress-")
        try:
            st = mgr.store("peer")
            for i in range(100):
                sha = f"{i:064x}"
                st.record_cache_entry(sha, payload_size=100)
            snap = mgr.store("peer").cache_snapshot()
            self.assertGreater(snap["entry_count"], 0)
            self.assertLessEqual(snap["entry_count"], 100)
        finally:
            mgr.shutdown()

    def test_enforce_limits_under_concurrent_access(self):
        mgr = _manager("flowshift-enforce-")
        errors = []
        lock = threading.Lock()

        def adder():
            try:
                for i in range(50):
                    mgr.capture_text("peer", f"add-{i}")
                    mgr.store("peer").enforce_limits(30, 10**9)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=adder, daemon=True) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        mgr.shutdown()


if __name__ == "__main__":
    unittest.main()
