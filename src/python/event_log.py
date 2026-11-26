"""Recent log lines for the WebGUI Event Log page.

``tray.log`` writes every line to ``flowshift.log``. Lines from INFO upwards
(INFO, WARN, ERROR) are also kept here, the last ``CAPACITY`` of them, and
published as SSE event ``{"type": "log", ...}``. The page loads the buffer
through ``GET /api/logs`` when it opens, so it also shows what happened before
(a refused edge switch, a failed connection). DEBUG lines and the access lines
of the web API (``web_api: ...``) stay in the file only.
"""
from __future__ import annotations

import threading
import time
from collections import deque

CAPACITY = 500
# A traceback of a crashed worker can be long; the file keeps all of it.
MAX_TEXT_CHARS = 4000

_LEVELS = {
    "INFO": "INFO",
    "OK": "INFO",
    "WARN": "WARN",
    "WARNING": "WARN",
    "ERROR": "ERROR",
    "CRITICAL": "ERROR",
}
_QUIET_PREFIXES = ("web_api: ",)


def normalize_level(level):
    """``INFO``/``WARN``/``ERROR`` for a published level, else ``None``."""
    return _LEVELS.get(str(level or "").strip().upper())


def should_publish(level, text):
    if normalize_level(level) is None:
        return False
    return not str(text or "").startswith(_QUIET_PREFIXES)


class LogBuffer:
    """Thread-safe ring of the last ``capacity`` published log lines."""

    def __init__(self, capacity=CAPACITY):
        self.capacity = int(capacity)
        self._lock = threading.Lock()
        self._entries = deque(maxlen=self.capacity)
        self._seq = 0

    def record(self, level, text, at=None):
        """Store one line; the entry, or ``None`` when it is not published."""
        if not should_publish(level, text):
            return None
        text = str(text)
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + " [...]"
        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "time": time.time() if at is None else float(at),
                "level": normalize_level(level),
                "text": text,
            }
            self._entries.append(entry)
        return dict(entry)

    def entries(self, after=0):
        """Oldest first; only entries with ``seq`` greater than ``after``."""
        with self._lock:
            return [dict(e) for e in self._entries if e["seq"] > after]

    def clear(self):
        with self._lock:
            self._entries.clear()


BUFFER = LogBuffer()


def log_event(entry):
    """SSE payload for a recorded entry."""
    return {"type": "log", **entry}
