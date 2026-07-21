"""Bounded-concurrency stress coverage for the Phase 1.5 update manager."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from update_manager import RESULT_ALREADY_RUNNING, RESULT_QUEUED, UpdateManager
from update_model import ReleaseDescriptor, STATUS_ERROR, STATUS_UPDATE_AVAILABLE, UpdateResult
from update_state import ERROR, UPDATE_AVAILABLE


def release_descriptor():
    content = b"stress"
    return ReleaseDescriptor(
        current_version="0.4.0", version="0.5.0", tag="v0.5.0", relation="newer",
        release_url="https://github.com/CryseXIII/flowshift/releases/tag/v0.5.0",
        release_notes="stress",
        installer_url=("https://github.com/CryseXIII/flowshift/releases/download/"
                       "v0.5.0/FlowShift-Setup.exe"),
        installer_size=len(content), installer_sha256=hashlib.sha256(content).hexdigest(),
        manifest_url="https://github.com/CryseXIII/flowshift/manifest",
        checksums_url="https://github.com/CryseXIII/flowshift/checksums",
        minimum_updater_version="0.4.0",
    )


class UpdateStressTests(unittest.TestCase):
    def test_snapshots_success_failure_rapid_admission_and_worker_bound(self):
        release = release_descriptor()
        calls = 0
        calls_lock = threading.Lock()

        def alternating(**kwargs):
            nonlocal calls
            with calls_lock:
                index = calls
                calls += 1
            if index % 2 == 0:
                return UpdateResult(STATUS_UPDATE_AVAILABLE, release)
            return UpdateResult(STATUS_ERROR, error_code="stress_failure", message="expected")

        with tempfile.TemporaryDirectory() as temporary:
            manager = UpdateManager(
                data_dir=temporary, current_version="0.4.0", updater_version="0.4.0",
                discovery=alternating, runtime_root=Path(temporary) / "checkout")
            try:
                with ThreadPoolExecutor(max_workers=32) as executor:
                    snapshots = list(executor.map(lambda _: manager.snapshot(), range(500)))
                self.assertEqual(len(snapshots), 500)
                self.assertTrue(all(value["schema_version"] == 1 for value in snapshots))

                successes = failures = 0
                for _ in range(200):
                    self.assertEqual(manager.check_for_updates().status, RESULT_QUEUED)
                    self.assertTrue(manager.wait_for_quiescence(timeout=2))
                    if manager.snapshot()["state"] == UPDATE_AVAILABLE:
                        successes += 1
                    elif manager.snapshot()["state"] == ERROR:
                        failures += 1
                self.assertEqual((successes, failures), (100, 100))

                gate = threading.Event()
                started = threading.Event()

                def blocked(**kwargs):
                    started.set()
                    gate.wait(2)
                    return UpdateResult(STATUS_UPDATE_AVAILABLE, release)

                manager._discovery = blocked
                with ThreadPoolExecutor(max_workers=32) as executor:
                    results = list(executor.map(lambda _: manager.check_for_updates(), range(100)))
                self.assertTrue(started.wait(1))
                self.assertEqual(sum(item.status == RESULT_QUEUED for item in results), 1)
                self.assertEqual(sum(item.status == RESULT_ALREADY_RUNNING for item in results), 99)
                workers = [thread for thread in threading.enumerate()
                           if thread.name == "FlowShiftUpdateWorker"]
                self.assertEqual(workers, [manager.worker_thread])
                gate.set()
                self.assertTrue(manager.wait_for_quiescence(timeout=2))

                persisted = json.loads(
                    (Path(temporary) / "update_state.json").read_text(encoding="utf-8"))
                self.assertEqual(persisted["schema_version"], 1)
                self.assertIsInstance(persisted["release"], dict)
                self.assertEqual(persisted["release"], asdict(release))
            finally:
                manager.shutdown()
            self.assertFalse(manager.worker_thread.is_alive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
