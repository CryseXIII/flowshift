"""Event Log of the WebGUI: recent runtime log lines with level, kind and device.

Viktor 26.11.: "der event log sollte auch einen gescheiten filter haben". The
WebGUI filters by level (Info, Warning, Error), by kind (connection, edge,
clipboard, update, Web API), by device, by free text and by time range. This
module gives every line of ``tray.log`` those fields once, when it is written,
and keeps the last ``MAX_ENTRIES`` in memory for ``GET /api/event-log``.

``record`` is called from ``tray.log`` on any thread, also while the caller
holds ``istate.lock``: it never takes a runtime lock and never logs itself.
The device names come from ``set_device_source`` (read at most every few
seconds, a failing source only leaves the device empty).
"""
from __future__ import annotations

import re
import threading
import time

MAX_ENTRIES = 1000
MAX_TEXT = 1000
MAX_DETAIL = 4000
DEVICE_CACHE_SECONDS = 5.0

LEVELS = ("info", "warning", "error")
KINDS = ("connection", "edge", "clipboard", "update", "web_api", "other")

_LEVEL_MAP = {
    "WARN": "warning", "WARNING": "warning",
    "ERROR": "error", "ERR": "error", "CRITICAL": "error", "FATAL": "error",
}
_SKIPPED_LEVELS = frozenset({"DEBUG", "TRACE"})

# First match wins: a clipboard transfer to a peer is "clipboard", a peer
# update is "update", an edge switch over a link is "edge".
_KIND_RULES = (
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


def redact(text):
    return _USER_PROFILE.sub("%USERPROFILE%", text)


def normalize_level(level):
    """'WARN' -> 'warning', 'ERROR' -> 'error', everything else -> 'info'.

    Returns None for DEBUG/TRACE, which the Event Log does not keep.
    """
    name = str(level or "").strip().upper()
    if name in _SKIPPED_LEVELS:
        return None
    return _LEVEL_MAP.get(name, "info")


def classify_kind(text):
    for kind, pattern in _KIND_RULES:
        if pattern.search(text):
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


class EventLog:
    def __init__(self, max_entries=MAX_ENTRIES, clock=time.time):
        self._lock = threading.Lock()
        self._entries = []
        self._next_id = 1
        self._max = max(1, int(max_entries))
        self._clock = clock
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
        if self._device_source is None:
            return []
        if self._devices_at is not None and now - self._devices_at < DEVICE_CACHE_SECONDS:
            return self._devices
        try:
            devices = [(str(name or ""), [str(a) for a in (aliases or []) if a])
                       for name, aliases in (self._device_source() or [])]
        except Exception:
            devices = self._devices
        self._devices, self._devices_at = devices, now
        return devices

    def record(self, level, message):
        """Keep one log line; returns the stored entry or None (DEBUG)."""
        lvl = normalize_level(level)
        if lvl is None:
            return None
        message = redact(str(message or ""))
        first, _, rest = message.partition("\n")
        text = first.strip()[:MAX_TEXT]
        now = self._clock()
        entry = {
            "level": lvl,
            "kind": classify_kind(text),
            "device": match_device(message[:MAX_TEXT + MAX_DETAIL], self._current_devices(now)),
            "text": text,
            "ts": round(float(now), 3),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        }
        if rest.strip():
            entry["detail"] = rest.strip()[:MAX_DETAIL]
        with self._lock:
            entry["id"] = self._next_id
            self._next_id += 1
            self._entries.append(entry)
            if len(self._entries) > self._max:
                del self._entries[: len(self._entries) - self._max]
        return entry

    def snapshot(self, after=0, limit=MAX_ENTRIES):
        """Entries with ``id > after``, oldest first, at most ``limit`` (newest kept)."""
        try:
            after = max(0, int(after))
        except (TypeError, ValueError):
            after = 0
        try:
            limit = min(MAX_ENTRIES, max(1, int(limit)))
        except (TypeError, ValueError):
            limit = MAX_ENTRIES
        with self._lock:
            entries = [dict(e) for e in self._entries if e["id"] > after]
            last_id = self._next_id - 1
        return {
            "entries": entries[-limit:],
            "last_id": last_id,
            "levels": list(LEVELS),
            "kinds": list(KINDS),
        }


EVENTS = EventLog()


def record(level, message):
    try:
        return EVENTS.record(level, message)
    except Exception:
        # The Event Log must never break the log call that feeds it.
        return None


def snapshot(after=0, limit=MAX_ENTRIES):
    return EVENTS.snapshot(after, limit)


def set_device_source(source):
    EVENTS.set_device_source(source)
