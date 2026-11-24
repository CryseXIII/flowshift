"""Hold-and-swipe gesture of the Command Wheel (pure, testable).

The wheel opens on the press of one of its triggers (the main hotkey, default
Ctrl+right click, and up to three more; each a key or a mouse button with
modifiers, see ``input_trigger``). What happens on release depends on how long the
trigger was held (``trigger.hold_ms`` from ``config["command_wheel"]``):

* short tap: the wheel stays open for clicking (``tap_open``), or closes when
  tapping is switched off;
* held: the sector in the direction of the cursor runs (``hold_swipe``). Only
  the direction counts, not the distance, so a cursor beyond the ring still
  selects. Releasing inside the small dead zone around the hub closes the
  wheel without an action.

The overlay host owns the geometry (``aim_slot``/``release_outcome``), the
runtime owns the trigger timing (:class:`HoldTracker`).
"""
from __future__ import annotations

import math
import threading
import time

# How the wheel reacts to its open trigger, stored as
# ``config["command_wheel"]["trigger"]`` (validated by overlay_actions). Which
# key or mouse button opens the wheel is ``command_wheel.hotkey`` (exactly one,
# or none when cleared).
HOLD_MS_MIN = 100
HOLD_MS_MAX = 2000
DEFAULT_TRIGGER = {"tap_open": True, "hold_swipe": True, "hold_ms": 250}

# Dead zone radius around the hub in CSS pixels (the hub itself is 60 px).
DEAD_ZONE_CSS = 30

OUTCOME_KEEP_OPEN = "keep_open"
OUTCOME_CLOSE = "close"
OUTCOME_EXECUTE = "execute"
OUTCOME_IGNORED = "ignored"
OUTCOMES = frozenset({OUTCOME_KEEP_OPEN, OUTCOME_CLOSE, OUTCOME_EXECUTE, OUTCOME_IGNORED})

# A trigger whose release never arrives (lost up event) ends after this time.
MAX_HOLD_SECONDS = 60.0


def trigger_from_payload(data):
    """Trigger settings from an overlay show payload; invalid fields use defaults."""
    raw = data.get("trigger") if isinstance(data, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    trigger = dict(DEFAULT_TRIGGER)
    for name in ("tap_open", "hold_swipe"):
        if isinstance(raw.get(name), bool):
            trigger[name] = raw[name]
    hold_ms = raw.get("hold_ms")
    if not isinstance(hold_ms, bool) and isinstance(hold_ms, int) \
            and HOLD_MS_MIN <= hold_ms <= HOLD_MS_MAX:
        trigger["hold_ms"] = hold_ms
    if not trigger["tap_open"] and not trigger["hold_swipe"]:
        trigger["tap_open"] = True
    return trigger


def aim_slot(dx, dy, count, dead_zone):
    """Sector index in the direction ``(dx, dy)`` from the hub, or ``None``.

    Screen coordinates (y grows downwards). Sector 0 is centered at the top and
    sectors run clockwise, exactly like the SVG wheel and the window shape.
    """
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        return None
    if math.hypot(dx, dy) < dead_zone:
        return None
    if count == 1:
        return 0
    step = 360.0 / count
    angle = math.degrees(math.atan2(dy, dx))
    relative = (angle + 90.0 + step / 2.0) % 360.0
    return int(relative // step) % count


def release_outcome(held_ms, trigger, slot):
    """Decide what a trigger release does; see the module docstring."""
    if held_ms < trigger["hold_ms"]:
        return OUTCOME_KEEP_OPEN if trigger["tap_open"] else OUTCOME_CLOSE
    if not trigger["hold_swipe"]:
        return OUTCOME_KEEP_OPEN
    return OUTCOME_EXECUTE if slot is not None else OUTCOME_CLOSE


class HoldTracker:
    """Press/release bookkeeping for the wheel trigger in the runtime.

    ``press`` returns ``False`` while a hold is already running (keyboard
    autorepeat, a second trigger), so the wheel is not reopened mid-gesture.
    ``release`` returns the held milliseconds for the matching source, else
    ``None``.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._source = None
        self._started = 0.0

    @property
    def active_source(self):
        with self._lock:
            return self._source

    def press(self, source):
        now = self._clock()
        with self._lock:
            if self._source is not None and now - self._started < MAX_HOLD_SECONDS:
                return False
            self._source = source
            self._started = now
            return True

    def release(self, source):
        now = self._clock()
        with self._lock:
            if self._source is None or self._source != source:
                return None
            self._source = None
            return max(0, int(round((now - self._started) * 1000)))

    def cancel(self):
        with self._lock:
            self._source = None
