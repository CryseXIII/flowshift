"""Pure tests for atomic runtime update-maintenance admission."""
from __future__ import annotations

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from update_runtime import RuntimeMaintenanceGate


class RuntimeMaintenanceGateTests(unittest.TestCase):
    def test_reservation_closes_admission_race_and_existing_work_drains(self):
        gate = RuntimeMaintenanceGate()
        admitted = threading.Event()
        release_work = threading.Event()

        def existing_work():
            self.assertTrue(gate.begin("profile_activation"))
            admitted.set()
            release_work.wait(1)
            gate.end("profile_activation")

        worker = threading.Thread(target=existing_work)
        worker.start()
        self.assertTrue(admitted.wait(1))
        reserved = gate.reserve()
        self.assertTrue(reserved["reserved"])
        self.assertEqual(reserved["active_admissions"], {"profile_activation": 1})
        self.assertFalse(gate.begin("overlay_show"))
        release_work.set()
        worker.join(1)
        self.assertEqual(gate.snapshot()["active_admissions"], {})
        gate.release()
        self.assertTrue(gate.begin("overlay_show"))
        gate.end("overlay_show")

    def test_shutdown_release_never_reopens_admission(self):
        gate = RuntimeMaintenanceGate()
        gate.reserve()
        gate.release(shutting_down=True)
        self.assertTrue(gate.snapshot()["reserved"])
        self.assertFalse(gate.begin("synthetic_input"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
