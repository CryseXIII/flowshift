"""WebGUI/program consistency checks and overlay start give-up behavior.

These tests run without Windows: the WebGUI check only inspects files, and the
overlay controller is driven with a ``Popen`` replacement that fails like a
host that can never become ready.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import overlay_controller
import web_api
import webgui_health as wh


def _write(root, name, text="x"):
    path = Path(root) / name
    path.write_text(text, encoding="utf-8")
    return path


class WebGuiHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fs-webgui-health-")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_root_reports_missing_and_blocks_overlay(self):
        report = wh.check_webgui(None, "0.7.0-dev.23")
        self.assertFalse(report["ok"])
        self.assertEqual(report["state"], wh.MISSING)
        self.assertFalse(report["overlay_ready"])
        self.assertEqual(report["missing_files"], ["index.html", "overlay.html"])
        self.assertIn("install_flowshift.bat", report["action"])

    def test_july_build_without_overlay_html_is_incomplete(self):
        # Viktor's laptop: dashboard from 17.07., no overlay.html, no VERSION.
        _write(self.root, "index.html", "<html><div id=root></div></html>")
        report = wh.check_webgui(self.root, "0.7.0-dev.23")
        self.assertFalse(report["ok"])
        self.assertEqual(report["state"], wh.INCOMPLETE)
        self.assertFalse(report["overlay_ready"])
        self.assertEqual(report["missing_files"], ["overlay.html"])
        self.assertIn("overlay.html", report["message"])
        self.assertIsNone(report["webgui_version"])

    def test_unversioned_complete_build_warns_but_allows_overlay(self):
        _write(self.root, "index.html")
        _write(self.root, "overlay.html")
        report = wh.check_webgui(self.root, "0.7.0-dev.23")
        self.assertFalse(report["ok"])
        self.assertEqual(report["state"], wh.UNVERSIONED)
        self.assertTrue(report["overlay_ready"])
        self.assertEqual(report["missing_files"], [])

    def test_version_mismatch_is_outdated(self):
        _write(self.root, "index.html")
        _write(self.root, "overlay.html")
        _write(self.root, "VERSION", "0.7.0-dev.22\n")
        report = wh.check_webgui(self.root, "0.7.0-dev.23")
        self.assertFalse(report["ok"])
        self.assertEqual(report["state"], wh.OUTDATED)
        self.assertEqual(report["webgui_version"], "0.7.0-dev.22")
        self.assertIn("0.7.0-dev.22", report["message"])
        self.assertIn("0.7.0-dev.23", report["message"])

    def test_matching_version_is_ok(self):
        _write(self.root, "index.html")
        _write(self.root, "overlay.html")
        # The installer and Vite may write a BOM or trailing newline.
        (self.root / "VERSION").write_text("﻿0.7.0-dev.23\r\n", encoding="utf-8")
        report = wh.check_webgui(self.root, "0.7.0-dev.23")
        self.assertTrue(report["ok"])
        self.assertEqual(report["state"], wh.OK)
        self.assertTrue(report["overlay_ready"])
        self.assertIsNone(report["action"])

    def test_oversized_version_file_is_ignored(self):
        _write(self.root, "index.html")
        _write(self.root, "overlay.html")
        _write(self.root, "VERSION", "9" * (wh.MAX_VERSION_BYTES + 1))
        report = wh.check_webgui(self.root, "0.7.0-dev.23")
        self.assertEqual(report["state"], wh.UNVERSIONED)

    def test_web_api_report_uses_served_root_without_paths(self):
        _write(self.root, "index.html")
        with mock.patch.object(web_api, "resolve_webgui_root", return_value=self.root):
            report = web_api.webgui_health_report("0.7.0-dev.23")
        self.assertEqual(report["state"], wh.INCOMPLETE)
        self.assertNotIn(str(self.root), repr(report))


class _FailingPopen:
    calls = 0

    def __init__(self, *args, **kwargs):
        type(self).calls += 1
        raise OSError("overlay UI did not become ready")


class OverlayGiveUpTests(unittest.TestCase):
    def make_controller(self, gave_up, **kwargs):
        return overlay_controller.OverlayController(
            host_script=os.path.join(os.path.dirname(__file__), "overlay_host.py"),
            python_executable=sys.executable,
            headless=True,
            restart_delays=(0.01, 0.02, 0.04),
            max_start_failures=3,
            give_up_cooldown=60.0,
            on_give_up=gave_up.append,
            **kwargs,
        )

    def test_never_ready_host_backs_off_then_gives_up_and_reports(self):
        gave_up = []
        _FailingPopen.calls = 0
        controller = self.make_controller(gave_up)
        try:
            with mock.patch.object(overlay_controller.subprocess, "Popen", _FailingPopen):
                self.assertFalse(controller.ensure_started())
                self.assertEqual(_FailingPopen.calls, 1)
                # Immediately again: the backoff applies before any host was ever ready.
                self.assertFalse(controller.ensure_started())
                self.assertEqual(_FailingPopen.calls, 1)
                deadline = time.monotonic() + 2.0
                while _FailingPopen.calls < 3 and time.monotonic() < deadline:
                    controller.ensure_started()
                    time.sleep(0.01)
                self.assertEqual(_FailingPopen.calls, 3)
                self.assertEqual(len(gave_up), 1)
                self.assertIn("did not become ready", gave_up[0])
                snapshot = controller.snapshot()
                self.assertTrue(snapshot["gave_up"])
                self.assertIn("did not become ready", snapshot["last_error"])
                # After giving up no further host is spawned, however often it is asked.
                for _ in range(20):
                    self.assertFalse(controller.ensure_started())
                    time.sleep(0.005)
                self.assertEqual(_FailingPopen.calls, 3)
                self.assertEqual(len(gave_up), 1)
                controller.reset_failures()
                self.assertFalse(controller.snapshot()["gave_up"])
                self.assertFalse(controller.ensure_started())
                self.assertEqual(_FailingPopen.calls, 4)
        finally:
            controller.shutdown(grace_period=0.5)

    def test_backoff_delays_grow_between_failed_starts(self):
        controller = self.make_controller([])
        try:
            with mock.patch.object(overlay_controller.subprocess, "Popen", _FailingPopen):
                delays = []
                for _ in range(2):
                    before = time.monotonic()
                    controller._restart_at = 0.0
                    controller.ensure_started()
                    delays.append(controller._restart_at - before)
            self.assertLess(delays[0], delays[1])
        finally:
            controller.shutdown(grace_period=0.5)

    def test_invalid_give_up_settings_are_rejected(self):
        with self.assertRaises(ValueError):
            overlay_controller.OverlayController(max_start_failures=0)
        with self.assertRaises(ValueError):
            overlay_controller.OverlayController(give_up_cooldown=0)

    def test_default_restart_delays_grow_beyond_one_second_cadence(self):
        self.assertEqual(overlay_controller.DEFAULT_RESTART_DELAYS[0], 1)
        self.assertGreaterEqual(max(overlay_controller.DEFAULT_RESTART_DELAYS), 30)


if __name__ == "__main__":
    unittest.main()
