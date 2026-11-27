"""Recent log lines for the WebGUI Event Log page.

``tray.log`` writes every line to ``flowshift.log``. Lines from INFO upwards
(INFO, WARN, ERROR) are also kept here, the last ``CAPACITY`` of them, and
published as SSE event ``{"type": "log", ...}``. The page loads the buffer
through ``GET /api/logs`` when it opens, so it also shows what happened before
(a refused edge switch, a failed connection). DEBUG lines and the access lines
of the web API (``web_api: ...``) stay in the file only.

Viktor 26.11.: "der event log sollte auch einen gescheiten filter haben". The
page filters by level, kind (connection, edge, clipboard, update, Web API), by
device, free text and time range. Every entry gets ``kind`` and ``device``
once, when it is recorded. ``record`` runs on any thread, also while the caller
holds ``istate.lock``: it never takes a runtime lock and never logs itself.
The device names come from ``set_device_source`` (read at most every few
seconds; a failing source only leaves the device empty).
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque

CAPACITY = 500
# A traceback of a crashed worker can be long; the file keeps all of it.
MAX_TEXT_CHARS = 4000
DEVICE_CACHE_SECONDS = 5.0

_LEVELS = {
    "INFO": "INFO",
    "OK": "INFO",
    "WARN": "WARN",
    "WARNING": "WARN",
    "ERROR": "ERROR",
    "CRITICAL": "ERROR",
}
_QUIET_PREFIXES = ("web_api: ",)

KINDS = ("connection", "edge", "clipboard", "update", "web_api", "other")

# First match wins: a clipboard transfer to a peer is "clipboard", a peer
# update is "update", an edge switch over a link is "edge".
_KIND_RULES = (
    # Remote preview steps belong to the link to the other PC, also when the
    # line names a switch or the Web API self-test (Viktor 27.11.).
    ("connection", re.compile(r"\bremote_preview\b", re.I)),
    ("web_api", re.compile(r"\bweb_?api\b|\bweb ?gui\b|/api/|\bsse\b", re.I)),
    ("update", re.compile(r"\bupdat|\binstaller\b|\brelease\b|\bpeer_update\b|\bupgrade", re.I)),
    ("clipboard", re.compile(r"clipboard|\bclip[_ ]|\bpaste|\boverlay\b|\bwheel\b", re.I)),
    ("edge", re.compile(r"\bedge|\bswitch|\bforward|\bhotkey|\breturn to\b|\bcursor\b", re.I)),
    ("connection", re.compile(r"\bpeer|connect|\blink\b|\bhello\b|\bping|discover|\bscan\b|"
                              r"\bsocket\b|\btcp\b|\budp\b|\bheartbeat\b|\bunreachable\b", re.I)),
)

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# AGENTS.md 12: no private absolute paths in API responses; the user folder
# (account name) becomes %USERPROFILE%, the rest of the path stays readable.
_USER_PROFILE = re.compile(r"\b[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s'\"]+", re.I)


def normalize_level(level):
    """``INFO``/``WARN``/``ERROR`` for a published level, else ``None``."""
    return _LEVELS.get(str(level or "").strip().upper())


def should_publish(level, text):
    if normalize_level(level) is None:
        return False
    return not str(text or "").startswith(_QUIET_PREFIXES)


def redact(text):
    return _USER_PROFILE.sub("%USERPROFILE%", text)


def classify_kind(text):
    # The first line decides: a traceback below mentions all kinds of modules.
    first = str(text or "").split("\n", 1)[0]
    for kind, pattern in _KIND_RULES:
        if pattern.search(first):
            return kind
    return "other"


def match_device(text, devices):
    """Name of the first device whose name, address or ID occurs in ``text``.

    ``devices`` is a list of ``(name, [alias, ...])``; names are matched as
    whole words without case, addresses and IDs literally.
    """
    if not text:
        return ""
    lowered = text.lower()
    addresses = set(_IPV4.findall(text))
    for name, aliases in devices:
        if not name:
            continue
        if re.search(r"(?<![\w-])" + re.escape(name.lower()) + r"(?![\w-])", lowered):
            return name
        for alias in aliases:
            if not alias:
                continue
            if alias in addresses or (len(alias) >= 8 and alias in text):
                return name
    return ""


class LogBuffer:
    """Thread-safe ring of the last ``capacity`` published log lines."""

    def __init__(self, capacity=CAPACITY):
        self.capacity = int(capacity)
        self._lock = threading.Lock()
        self._entries = deque(maxlen=self.capacity)
        self._seq = 0
        self._device_source = None
        self._devices = []
        self._devices_at = None

    def set_device_source(self, source):
        """``source()`` returns ``[(name, [address, device_id, ...]), ...]``."""
        with self._lock:
            self._device_source = source
            self._devices_at = None

    def _current_devices(self, now):
        # Called outside self._lock: the source may be slow or fail.
        source = self._device_source
        if source is None:
            return []
        if self._devices_at is not None and 0 <= now - self._devices_at < DEVICE_CACHE_SECONDS:
            return self._devices
        try:
            devices = [(str(name or ""), [str(a) for a in (aliases or []) if a])
                       for name, aliases in (source() or [])]
        except Exception:
            devices = self._devices
        self._devices, self._devices_at = devices, now
        return devices

    def record(self, level, text, at=None):
        """Store one line; the entry, or ``None`` when it is not published."""
        if not should_publish(level, text):
            return None
        text = redact(str(text))
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + " [...]"
        kind = classify_kind(text)
        device = match_device(text, self._current_devices(time.monotonic()))
        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "time": time.time() if at is None else float(at),
                "level": normalize_level(level),
                "kind": kind,
                "device": device,
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


def set_device_source(source):
    BUFFER.set_device_source(source)


def log_event(entry):
    """SSE payload for a recorded entry."""
    return {"type": "log", **entry}
