"""Only one FlowShift runtime per PC (cause of the doubled log lines, Viktor 27.11.).

On BOOK-Viktor every line of ``flowshift.log`` stood twice in a row: two
runtimes ran at the same time and both wrote every step (connector, edge
watcher, scan). The old guard could not stop the second one:

- ``CreateMutexW`` was followed by ``ctypes.windll.kernel32.GetLastError()``.
  ``windll`` does not keep the error of the call before; ctypes and Python may
  have reset it in between, so ``ERROR_ALREADY_EXISTS`` (183) could be missed.
  ``WinDLL(..., use_last_error=True)`` with ``ctypes.get_last_error()`` reads
  the error ctypes saved right after the call.
- A plain mutex name lives in the session namespace (``Local\\``): a runtime
  in another Windows session (the optional NSSM helper in session 0, a second
  logged-on user, a Remote Desktop session) did not see it.
- The listeners used ``SO_REUSEADDR``; on Windows that lets a second process
  bind the same port, so nothing failed there either.

The guard now takes, in this order, a lock on ``flowshift_runtime.lock`` in the
data folder (``%ProgramData%\\FlowShift``, shared by every session; released by
Windows when the process ends, also after a crash) and the session mutex read
with the saved last error. Either one held by another runtime stops this one
with a log line that names it.

Standard library only; the Windows calls are injected in tests.
"""
from __future__ import annotations

import ctypes
import os

LOCK_FILE_NAME = "flowshift_runtime.lock"
ERROR_ALREADY_EXISTS = 183


def lock_path(data_dir):
    return os.path.join(str(data_dir), LOCK_FILE_NAME)


class RuntimeLock:
    """Exclusive, non-blocking lock in the lock file (all sessions)."""

    def __init__(self, path, locker=None):
        self.path = path
        self._locker = locker
        self._handle = None
        self.owner = ""

    def acquire(self, pid=None):
        """True when this process holds the lock now; else ``owner`` says who does."""
        try:
            handle = open(self.path, "a+b")
        except OSError as exc:
            # No data folder access: do not block the start, the mutex still runs.
            self.owner = f"lock file not usable ({exc.__class__.__name__})"
            return True
        try:
            (self._locker or _lock_byte)(handle)
        except OSError:
            try:
                handle.seek(0)
                self.owner = handle.read(64).decode("ascii", "replace").strip()
            except OSError:
                self.owner = ""
            handle.close()
            return False
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid() if pid is None else pid}".encode("ascii"))
            handle.flush()
        except OSError:
            pass
        self._handle = handle
        return True

    def release(self):
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None


# Windows locks a byte range also against reading: the range lies behind the
# ``pid=N`` text, so a second runtime can still read who holds the lock.
LOCK_OFFSET = 4096


def _lock_byte(handle):
    """Lock one byte without waiting; raises OSError when another process holds it."""
    if os.name == "nt":
        import msvcrt
        handle.seek(LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        handle.seek(0)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def create_mutex(name, kernel32=None, get_last_error=None):
    """``(handle, already_exists)`` of the named mutex, with the saved last error."""
    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_last_error = ctypes.get_last_error
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, 0, name)
    error = get_last_error() if get_last_error else 0
    return handle, (error == ERROR_ALREADY_EXISTS)


def acquire(data_dir, mutex_name, kernel32=None, get_last_error=None, locker=None):
    """``(ok, guard, reason)``: ``guard`` must stay referenced while the runtime runs."""
    lock = RuntimeLock(lock_path(data_dir), locker=locker)
    if not lock.acquire():
        owner = f" ({lock.owner})" if lock.owner else ""
        return False, None, f"another FlowShift runtime holds {LOCK_FILE_NAME}{owner}"
    try:
        handle, exists = create_mutex(mutex_name, kernel32, get_last_error)
    except (AttributeError, OSError):
        handle, exists = None, False
    if exists:
        lock.release()
        return False, None, f"another FlowShift runtime holds the mutex {mutex_name}"
    return True, (lock, handle), ""
