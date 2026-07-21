"""Phase 2 clipboard listener, queue, suppression, and capture-bound tests."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

import clipboard_events as events
import clipboard_files as files
import clipboard_model as model
from clipboard_runtime import ClipboardManager
import clipboard_win


class BoundedClipboardEventTests(unittest.TestCase):
    def test_queue_coalesces_toward_the_newest_sequence(self):
        queue = events.BoundedClipboardEvents(capacity=2)
        self.assertTrue(queue.submit(1))
        self.assertTrue(queue.submit(1))
        self.assertTrue(queue.submit(2))
        self.assertTrue(queue.submit(3))

        self.assertEqual(queue.get(0)["sequence"], 3)
        snapshot = queue.snapshot()
        self.assertEqual(snapshot["coalesced"], 3)
        self.assertEqual(snapshot["dropped"], 0)

    def test_closed_queue_rejects_and_wakes(self):
        queue = events.BoundedClipboardEvents(capacity=1)
        queue.close()
        self.assertFalse(queue.submit(1))
        self.assertIsNone(queue.get(0))
        self.assertTrue(queue.snapshot()["closed"])


class ClipboardWriteSuppressorTests(unittest.TestCase):
    def test_successful_write_is_suppressed_once_for_exact_sequence(self):
        suppressor = events.ClipboardWriteSuppressor()
        digest = events.text_digest("same")
        token = suppressor.prepare("item", {"text"}, "text", digest, 10)
        self.assertTrue(suppressor.finish(token, True, 11))

        self.assertTrue(suppressor.consume(11, {"text"}, "text", digest))
        self.assertFalse(suppressor.consume(11, {"text"}, "text", digest))
        self.assertFalse(suppressor.consume(12, {"text"}, "text", digest))

    def test_failed_write_and_later_equal_copy_are_not_suppressed(self):
        suppressor = events.ClipboardWriteSuppressor()
        digest = events.text_digest("same")
        token = suppressor.prepare("item", {"text"}, "text", digest, 10)
        self.assertFalse(suppressor.finish(token, False, 10))
        self.assertFalse(suppressor.consume(10, {"text"}, "text", digest))

    def test_multiformat_write_requires_observed_formats(self):
        suppressor = events.ClipboardWriteSuppressor()
        digest = events.bytes_digest(b"html")
        token = suppressor.prepare("item", {"html", "text"}, "html", digest, 1)
        suppressor.finish(token, True, 2)
        self.assertFalse(suppressor.consume(2, {"html"}, "html", digest))
        self.assertTrue(suppressor.consume(2, {"html", "text"}, "html", digest))

    def test_capture_waits_briefly_for_matching_pending_write(self):
        suppressor = events.ClipboardWriteSuppressor()
        digest = events.text_digest("same")
        token = suppressor.prepare("item", {"text"}, "text", digest, 10)
        result = []
        thread = threading.Thread(
            target=lambda: result.append(suppressor.consume(11, {"text"}, "text", digest)))
        thread.start()
        time.sleep(0.02)
        suppressor.finish(token, True, 11)
        thread.join(1)
        self.assertEqual(result, [True])


class ClipboardCaptureIntegrationTests(unittest.TestCase):
    def test_manager_write_marks_current_and_commits_suppression_only_on_success(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-write-suppression-") as root:
            settings = model.clipboard_settings({"clipboard": {"enabled": True}})
            manager = ClipboardManager(root, "device", lambda _identity, _msg: None,
                                       lambda: settings)
            sequence = [20]
            try:
                item = manager.capture_text("peer", "hello")

                def successful_write():
                    sequence[0] = 21
                    return True

                self.assertTrue(manager.perform_windows_write(
                    "peer", item["item_id"], {"text"}, "text",
                    manager.text_digest("hello"), successful_write, lambda: sequence[0]))
                self.assertEqual(manager.store("peer").current_item_id, item["item_id"])
                self.assertTrue(manager.consume_write_suppression(
                    21, {"text"}, "text", manager.text_digest("hello")))

                self.assertFalse(manager.perform_windows_write(
                    "peer", item["item_id"], {"text"}, "text",
                    manager.text_digest("hello"), lambda: False, lambda: sequence[0]))
                self.assertEqual(manager.write_suppression_snapshot()["failed"], 1)
            finally:
                manager.shutdown()

    def test_hard_item_cap_rejects_before_store_mutation(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-capture-cap-") as root:
            settings = model.clipboard_settings({"clipboard": {"enabled": True}})
            settings["max_item_gb"] = 0.000000001
            manager = ClipboardManager(root, "device", lambda _identity, _msg: None,
                                       lambda: settings)
            try:
                self.assertIsNone(manager.capture_text("peer", "too large"))
                self.assertEqual(manager.list_items("peer"), [])
            finally:
                manager.shutdown()

    def test_write_is_rejected_after_shutdown_without_calling_writer(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-write-shutdown-") as root:
            settings = model.clipboard_settings({"clipboard": {"enabled": True}})
            manager = ClipboardManager(root, "device", lambda _identity, _msg: None,
                                       lambda: settings)
            item = manager.capture_text("peer", "hello")
            manager.shutdown()
            called = []
            self.assertFalse(manager.perform_windows_write(
                "peer", item["item_id"], {"text"}, "text", manager.text_digest("hello"),
                lambda: called.append(True) or True, lambda: 1))
            self.assertEqual(called, [])

    def test_locked_write_sequence_cannot_suppress_a_later_user_copy(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-write-attribution-") as root:
            settings = model.clipboard_settings({"clipboard": {"enabled": True}})
            manager = ClipboardManager(root, "device", lambda _identity, _msg: None,
                                       lambda: settings)
            item = manager.capture_text("peer", "same")
            try:
                sequence_samples = iter((10, 12))
                self.assertTrue(manager.perform_windows_write(
                    "peer", item["item_id"], {"text"}, "text", manager.text_digest("same"),
                    lambda: (True, 11), lambda: next(sequence_samples)))
                self.assertFalse(manager.consume_write_suppression(
                    12, {"text"}, "text", manager.text_digest("same")))
                self.assertTrue(manager.consume_write_suppression(
                    11, {"text"}, "text", manager.text_digest("same")))
            finally:
                manager.shutdown()

    def test_file_scan_stops_before_hashing_over_limit(self):
        with tempfile.TemporaryDirectory(prefix="flowshift-file-cap-") as root:
            path = os.path.join(root, "payload.bin")
            with open(path, "wb") as handle:
                handle.write(b"1234")
            with self.assertRaises(files.CaptureLimitError):
                files.scan_paths([path], max_total_bytes=3)


class WindowsClipboardListenerTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows clipboard listener")
    def test_listener_starts_and_stops_cleanly(self):
        delivered = threading.Event()
        listener = clipboard_win.ClipboardListener(lambda _sequence: delivered.set())
        try:
            self.assertTrue(listener.start())
            self.assertTrue(listener.snapshot()["running"])
            import ctypes
            ctypes.windll.user32.PostMessageW(
                listener._hwnd, listener.WM_CLIPBOARDUPDATE, 0, 0)
            self.assertTrue(delivered.wait(1.0))
        finally:
            self.assertTrue(listener.stop())
        self.assertFalse(listener.snapshot()["running"])


if __name__ == "__main__":
    unittest.main()
