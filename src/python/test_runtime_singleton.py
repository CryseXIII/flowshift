"""Only one FlowShift runtime per PC (cause of BOOK-Viktor's doubled log lines).

The lock file is taken for real (``fcntl``/``msvcrt`` on a temporary folder);
the mutex is a stand-in that reports the last error the way ctypes saves it.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime_singleton as rs


class _Kernel32:
    """``CreateMutexW`` stand-in: the second call reports ERROR_ALREADY_EXISTS."""

    def __init__(self):
        self.names = []
        self.last_error = 0

    def CreateMutexW(self, _attrs, _owner, name):  # noqa: N802 - Windows name
        self.last_error = rs.ERROR_ALREADY_EXISTS if name in self.names else 0
        self.names.append(name)
        return len(self.names)


class RuntimeSingletonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fs-single-", ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.kernel = _Kernel32()

    def acquire(self, kernel=None):
        kernel = kernel or self.kernel
        return rs.acquire(self.tmp.name, "FlowShift_Runtime_Mutex", kernel32=kernel,
                          get_last_error=lambda: kernel.last_error)

    def test_second_runtime_is_refused_by_the_lock_file(self):
        ok, guard, reason = self.acquire()
        self.assertTrue(ok, reason)
        self.addCleanup(guard[0].release)
        # Another session: its own mutex namespace (fresh stand-in), same data folder.
        ok2, guard2, reason2 = self.acquire(_Kernel32())
        self.assertFalse(ok2)
        self.assertIsNone(guard2)
        self.assertIn("flowshift_runtime.lock", reason2)
        self.assertIn(f"pid={os.getpid()}", reason2)

    def test_lock_is_free_again_after_the_first_runtime_ended(self):
        ok, guard, _ = self.acquire()
        self.assertTrue(ok)
        guard[0].release()
        ok2, guard2, reason2 = self.acquire(_Kernel32())
        self.assertTrue(ok2, reason2)
        guard2[0].release()

    def test_mutex_error_is_read_from_the_saved_last_error(self):
        self.kernel.names.append("FlowShift_Runtime_Mutex")  # held in this session
        ok, guard, reason = self.acquire()
        self.assertFalse(ok)
        self.assertIn("mutex", reason)
        # The lock file was given back, so the real owner is not blocked by us.
        ok2, guard2, _ = self.acquire(_Kernel32())
        self.assertTrue(ok2)
        guard2[0].release()

    def test_unusable_data_folder_does_not_block_the_start(self):
        missing = os.path.join(self.tmp.name, "missing", "deeper")
        ok, guard, reason = rs.acquire(missing, "M", kernel32=self.kernel,
                                       get_last_error=lambda: self.kernel.last_error)
        self.assertTrue(ok, reason)

    def test_tray_listener_does_not_share_the_port_on_windows(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tray.py"),
                  encoding="utf-8") as f:
            source = f.read()
        self.assertIn("runtime_singleton.acquire(DATA_DIR, RUNTIME_MUTEX_NAME)", source)
        self.assertNotIn("kernel32.GetLastError() == 183", source)
        self.assertIn("SO_EXCLUSIVEADDRUSE", source)


if __name__ == "__main__":
    unittest.main()
