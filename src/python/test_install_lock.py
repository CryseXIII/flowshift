"""Offline tests for the shared install lock and the manual-install hold.

21.11.: two update runs and a manual install_flowshift.ps1 ran at the same
time and FlowShift ended up without a program folder. These tests cover the
Python side: the runtime never hands an update over while another
installation holds the lock, a busy runner ack ends the handoff without
stopping FlowShift, and the first start after a manual install waits for the
next regular check instead of updating at once.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import install_lock
import update_handoff as handoff
from test_update_manager import FakeTransport, descriptor, response
from update_download import DownloadedAsset
from update_manager import ERROR_INSTALL_BUSY, RESULT_BLOCKED, RESULT_QUEUED, UpdateManager
from update_model import STATUS_UPDATE_AVAILABLE, UpdateResult
from update_state import DOWNLOADED, ERROR, INSTALLING, UPDATE_AVAILABLE


def _utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def held_opener(path, mode):
    # What Windows reports while update_flowshift.ps1 or install_flowshift.ps1
    # holds install.lock (shared for reading only) and someone opens it for writing.
    raise PermissionError(13, "The process cannot access the file", str(path))


class InstallLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "FlowShift"

    def tearDown(self):
        self.temporary.cleanup()

    def write_lock(self, record):
        path = install_lock.lock_path(self.data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_missing_or_unheld_lock_is_free(self):
        self.assertFalse(install_lock.is_locked(self.data_dir))
        self.write_lock({"owner": "update", "pid": 5})
        # A crashed run leaves only the file; nobody holds it open.
        self.assertFalse(install_lock.is_locked(self.data_dir))

    def test_sharing_violation_means_held_and_owner_is_readable(self):
        self.write_lock({"schema_version": 1, "owner": "install", "version": "0.7.0-dev.31",
                         "pid": 4242, "since": "2026-11-21T21:09:57Z"})
        self.assertTrue(install_lock.is_locked(self.data_dir, opener=held_opener))
        owner = install_lock.read_owner(self.data_dir)
        self.assertEqual(install_lock.describe_owner(owner),
                         "manual installation of 0.7.0-dev.31 since 2026-11-21T21:09:57Z")
        self.assertEqual(install_lock.describe_owner(None), "unknown owner")

    def test_other_errors_do_not_block_updates(self):
        self.write_lock({"owner": "update"})

        def broken(path, mode):
            raise OSError(5, "I/O error")

        self.assertFalse(install_lock.is_locked(self.data_dir, opener=broken))

    def write_marker(self, version, finished_at):
        path = install_lock.manual_install_path(self.data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "version": version,
                                    "finished_at": finished_at}), encoding="utf-8")
        return path

    def test_manual_install_marker_is_consumed_once_for_its_version(self):
        now = datetime(2026, 11, 21, 21, 10, 55, tzinfo=timezone.utc)
        path = self.write_marker("0.7.0-dev.31", _utc(now - timedelta(seconds=3)))
        record = install_lock.consume_manual_install(self.data_dir, "0.7.0-dev.31", now=now)
        self.assertEqual(record["version"], "0.7.0-dev.31")
        self.assertFalse(path.exists())
        self.assertIsNone(install_lock.consume_manual_install(self.data_dir, "0.7.0-dev.31", now=now))

    def test_marker_of_another_version_or_old_marker_is_dropped(self):
        now = datetime(2026, 11, 21, 21, 10, 55, tzinfo=timezone.utc)
        path = self.write_marker("0.7.0-dev.33", _utc(now))
        self.assertIsNone(install_lock.consume_manual_install(self.data_dir, "0.7.0-dev.31", now=now))
        self.assertFalse(path.exists())
        path = self.write_marker("0.7.0-dev.31", _utc(now - timedelta(days=2)))
        self.assertIsNone(install_lock.consume_manual_install(self.data_dir, "0.7.0-dev.31", now=now))
        self.assertFalse(path.exists())


class HandoffLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.program_files = self.root / "Program Files"
        self.program_data = self.root / "ProgramData"
        self.install_dir = self.program_files / "FlowShift"
        self.data_dir = self.program_data / "FlowShift"
        self.install_dir.mkdir(parents=True)
        (self.install_dir / handoff.RUNNER_NAME).write_text("# runner\n", encoding="utf-8")
        content = b"verified setup bytes"
        self.release = descriptor(content)
        installer = self.data_dir / "updates" / "downloads" / f"FlowShift-Setup-{self.release.version}.exe"
        installer.parent.mkdir(parents=True)
        installer.write_bytes(content)
        self.asset = DownloadedAsset(str(installer), installer.name, self.release.version,
                                     len(content), self.release.installer_sha256)

    def tearDown(self):
        self.temporary.cleanup()

    def call(self, launcher, **changes):
        values = {
            "release": self.release, "asset": self.asset, "runtime_root": self.install_dir,
            "runtime_pid": 1234, "launcher": launcher, "ack_timeout": 0,
            "_program_files": self.program_files, "_program_data": self.program_data,
            "_request_id": "b" * 32, "_created_at": "2026-11-21T21:11:07Z",
            "lock_check": lambda data_dir: False,
        }
        values.update(changes)
        return handoff.handoff_update(**values)

    def test_held_lock_stops_the_handoff_before_anything_is_written(self):
        launched = []
        with self.assertRaises(handoff.UpdateHandoffError) as raised:
            self.call(lambda arguments, **kwargs: launched.append(arguments),
                      lock_check=lambda data_dir: True)
        self.assertEqual(raised.exception.code, handoff.ERROR_INSTALL_BUSY)
        self.assertIn("Nothing was changed", str(raised.exception))
        self.assertEqual(launched, [])
        self.assertFalse((self.data_dir / "updates" / "update_plan.json").exists())

    def test_busy_ack_from_the_runner_is_reported_as_install_busy(self):
        def busy_runner(arguments, **kwargs):
            plan = json.loads(Path(arguments[arguments.index("-PlanPath") + 1]).read_text(encoding="utf-8"))
            ack = self.data_dir / "updates" / "acks" / f"update_ack-{plan['request_id']}.json"
            ack.parent.mkdir(parents=True, exist_ok=True)
            ack.write_text(json.dumps({"schema_version": 1, "request_id": plan["request_id"],
                                       "status": "busy", "runner_pid": 77}), encoding="utf-8")
            return object()

        with self.assertRaises(handoff.UpdateHandoffError) as raised:
            self.call(busy_runner)
        self.assertEqual(raised.exception.code, handoff.ERROR_INSTALL_BUSY)

    def test_unknown_ack_status_is_not_accepted(self):
        def odd_runner(arguments, **kwargs):
            plan = json.loads(Path(arguments[arguments.index("-PlanPath") + 1]).read_text(encoding="utf-8"))
            ack = self.data_dir / "updates" / "acks" / f"update_ack-{plan['request_id']}.json"
            ack.parent.mkdir(parents=True, exist_ok=True)
            ack.write_text(json.dumps({"schema_version": 1, "request_id": plan["request_id"],
                                       "status": "maybe", "runner_pid": 77}), encoding="utf-8")
            return object()

        with self.assertRaises(handoff.UpdateHandoffError) as raised:
            self.call(odd_runner)
        self.assertEqual(raised.exception.code, "ack_timeout")


class ManagerLockTests(unittest.TestCase):
    def make_manager(self, root, discovery, **kwargs):
        official = Path(root) / "Program Files" / "FlowShift"
        official.mkdir(parents=True, exist_ok=True)
        return UpdateManager(
            data_dir=root, current_version="0.4.0", updater_version="0.4.0",
            discovery=discovery, runtime_root=official, official_install_dir=official,
            program_files=Path(root) / "other", **kwargs)

    def wait_for_state(self, manager, expected, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and manager.snapshot()["state"] != expected:
            time.sleep(0.01)
        return manager.snapshot()["state"]

    def test_install_is_blocked_while_another_installation_holds_the_lock(self):
        content = b"locked install"
        release = descriptor(content)
        locked = threading.Event()
        locked.set()
        handed_off = []
        with tempfile.TemporaryDirectory() as temporary:
            manager = self.make_manager(
                temporary, lambda **kwargs: UpdateResult(STATUS_UPDATE_AVAILABLE, release),
                download_transport=FakeTransport([response(200, content)]),
                handoff=lambda desc, asset: handed_off.append(desc.version),
                install_lock_check=locked.is_set)
            try:
                self.assertEqual(manager.check_for_updates().status, RESULT_QUEUED)
                self.assertTrue(manager.wait_for_quiescence())
                self.assertEqual(manager.download_update().status, RESULT_QUEUED)
                self.assertTrue(manager.wait_for_quiescence())
                result = manager.install_update(automatic=True)
                self.assertEqual(result.status, RESULT_BLOCKED)
                self.assertEqual(result.error_code, ERROR_INSTALL_BUSY)
                self.assertEqual(manager.snapshot()["state"], DOWNLOADED)
                self.assertEqual(handed_off, [])
                locked.clear()
                self.assertEqual(manager.install_update(automatic=True).status, RESULT_QUEUED)
                self.assertEqual(self.wait_for_state(manager, INSTALLING), INSTALLING)
                self.assertEqual(handed_off, [release.version])
            finally:
                manager.shutdown()

    def test_busy_handoff_keeps_flowshift_running_and_reports_install_busy(self):
        content = b"busy runner"
        release = descriptor(content)
        stopped = []

        def busy(desc, asset):
            raise handoff.UpdateHandoffError(handoff.ERROR_INSTALL_BUSY, "Another FlowShift installation is running")

        with tempfile.TemporaryDirectory() as temporary:
            manager = self.make_manager(
                temporary, lambda **kwargs: UpdateResult(STATUS_UPDATE_AVAILABLE, release),
                download_transport=FakeTransport([response(200, content)]),
                handoff=busy, shutdown_callback=lambda: stopped.append(True),
                install_lock_check=lambda: False)
            try:
                manager.check_for_updates()
                self.assertTrue(manager.wait_for_quiescence())
                manager.download_update()
                self.assertTrue(manager.wait_for_quiescence())
                self.assertEqual(manager.install_update().status, RESULT_QUEUED)
                self.assertEqual(self.wait_for_state(manager, ERROR), ERROR)
                self.assertEqual(manager.snapshot()["last_error"]["code"], ERROR_INSTALL_BUSY)
                self.assertEqual(stopped, [])
            finally:
                manager.shutdown()

    def write_marker(self, root, version="0.4.0"):
        path = install_lock.manual_install_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": 1, "version": version,
            "finished_at": _utc(datetime.now(timezone.utc))}), encoding="utf-8")
        return path

    def test_first_start_after_manual_install_waits_for_the_next_regular_check(self):
        content = b"newer after manual install"
        release = descriptor(content)
        checks = []
        with tempfile.TemporaryDirectory() as temporary:
            marker = self.write_marker(temporary)
            manager = self.make_manager(
                temporary,
                lambda **kwargs: checks.append(True) or UpdateResult(STATUS_UPDATE_AVAILABLE, release),
                download_transport=FakeTransport([response(200, content)]),
                manual_install_hold=0.4, install_lock_check=lambda: False)
            try:
                self.assertFalse(marker.exists())
                self.assertGreater(manager.snapshot()["automatic_hold_seconds"], -1)
                self.assertGreater(manager.automatic_hold_remaining(), 0.2)
                manager.start_automatic_updates(
                    lambda: {"enabled": True, "check_on_start": True, "policy": "download"},
                    lambda: True, startup_delay=2, poll_interval=0.05,
                    startup_wait=lambda event, delay: False)
                time.sleep(0.2)
                # No check (and so no download or install) during the hold.
                self.assertEqual(checks, [])
                self.assertEqual(self.wait_for_state(manager, DOWNLOADED), DOWNLOADED)
                self.assertEqual(checks, [True])
            finally:
                manager.shutdown()

    def test_start_without_marker_checks_at_once(self):
        release = descriptor(b"no marker")
        checks = []
        with tempfile.TemporaryDirectory() as temporary:
            self.write_marker(temporary, version="0.3.9")
            manager = self.make_manager(
                temporary,
                lambda **kwargs: checks.append(True) or UpdateResult(STATUS_UPDATE_AVAILABLE, release),
                manual_install_hold=30, install_lock_check=lambda: False)
            try:
                self.assertEqual(manager.automatic_hold_remaining(), 0.0)
                manager.start_automatic_updates(
                    lambda: {"enabled": True, "check_on_start": True, "policy": "notify"},
                    lambda: True, startup_delay=2, poll_interval=0.05,
                    startup_wait=lambda event, delay: False)
                self.assertEqual(self.wait_for_state(manager, UPDATE_AVAILABLE), UPDATE_AVAILABLE)
                self.assertEqual(checks, [True])
            finally:
                manager.shutdown()


if __name__ == "__main__":
    unittest.main()
