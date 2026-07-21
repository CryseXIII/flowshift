"""Pure atomic admission gate for runtime update maintenance."""
from __future__ import annotations

import threading


class RuntimeMaintenanceGate:
    """Reject new work after reservation while truthfully tracking admitted work."""

    def __init__(self):
        self._lock = threading.Lock()
        self._reserved = False
        self._active = {}

    def begin(self, kind, shutting_down=False):
        with self._lock:
            if self._reserved or shutting_down:
                return False
            self._active[kind] = self._active.get(kind, 0) + 1
            return True

    def end(self, kind):
        with self._lock:
            remaining = self._active.get(kind, 0) - 1
            if remaining > 0:
                self._active[kind] = remaining
            else:
                self._active.pop(kind, None)

    def reserve(self):
        with self._lock:
            self._reserved = True
            return self._snapshot_locked()

    def release(self, shutting_down=False):
        with self._lock:
            if not shutting_down:
                self._reserved = False
            return self._snapshot_locked()

    def snapshot(self):
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self):
        return {
            "reserved": self._reserved,
            "active_admissions": dict(self._active),
        }
