"""Finite timeouts, deadline tracking and smoothed progress for transfer V2.

Phase 3, sections 21 and 22. Transport-neutral helpers used by
:class:`clipboard_streaming_v2.IncomingTransferStage` and the runtime:

- :class:`TransferTimeouts` - the six mandatory finite timeouts, loaded from
  the clipboard settings block;
- :class:`DeadlineTracker` - one absolute deadline per lifecycle phase; a
  fired deadline is reported exactly once and only when ``now`` reached it;
- :class:`TransferRateTracker` - EWMA transfer rate and ETA that freeze while
  a transfer is paused so pauses never distort them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, fields

TIMEOUT_NAMES = ("preflight", "manifest_ack", "window_ack", "no_progress",
                 "reconnect_wait", "final_complete_ack")

# Settings keys (clipboard block) -> TransferTimeouts field, in seconds.
TIMEOUT_SETTINGS_KEYS = {
    name: f"clipboard_transfer_v2_{name}_timeout_s" for name in TIMEOUT_NAMES
}

# Lifecycle phase -> timeout that bounds the wait in that phase.
PHASE_TIMEOUTS = {
    "created": "preflight",
    "preflight": "preflight",
    "accepted": "preflight",
    "sending_manifest": "manifest_ack",
    "transferring": "no_progress",
    "receiving": "no_progress",
    "awaiting_ack": "window_ack",
    "paused": "reconnect_wait",
    "waiting_reconnect": "reconnect_wait",
    "verifying": "final_complete_ack",
    "finalizing": "final_complete_ack",
    "cancelled": "final_complete_ack",
}
# Phases without any pending wait.
UNBOUNDED_PHASES = frozenset(("completed", "failed", "purged"))

MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 86400.0


def _finite_seconds(value, name):
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number of seconds") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be a finite positive number of seconds")
    return seconds


@dataclass(frozen=True)
class TransferTimeouts:
    """Finite waits (seconds) for every V2 lifecycle phase. No indefinite waits."""

    preflight: float = 60.0
    manifest_ack: float = 30.0
    window_ack: float = 30.0
    no_progress: float = 60.0
    reconnect_wait: float = 300.0
    final_complete_ack: float = 30.0

    def __post_init__(self):
        for field in fields(self):
            object.__setattr__(self, field.name,
                               _finite_seconds(getattr(self, field.name), field.name))

    @classmethod
    def from_settings(cls, settings):
        """Build from a normalised clipboard settings dict; invalid values fall back."""
        values = {}
        defaults = cls()
        for name, key in TIMEOUT_SETTINGS_KEYS.items():
            raw = settings.get(key) if isinstance(settings, dict) else None
            try:
                seconds = _finite_seconds(raw, key)
            except ValueError:
                seconds = getattr(defaults, name)
            values[name] = max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, seconds))
        return cls(**values)

    def for_phase(self, phase):
        """Timeout (seconds) bounding ``phase`` or None when the phase does not wait."""
        name = PHASE_TIMEOUTS.get(phase)
        return None if name is None else getattr(self, name)


class DeadlineTracker:
    """Absolute deadline for the current phase, fired once when ``now`` reaches it."""

    def __init__(self, timeouts, phase, now):
        if not isinstance(timeouts, TransferTimeouts):
            raise ValueError("timeouts must be a TransferTimeouts")
        self._timeouts = timeouts
        self._phase = None
        self._anchor = None
        self._deadline = None
        self._fired = None
        self.enter(phase, now)

    @property
    def phase(self):
        return self._phase

    @property
    def deadline(self):
        return self._deadline

    @property
    def timeout_name(self):
        return PHASE_TIMEOUTS.get(self._phase)

    def enter(self, phase, now):
        """Switch to ``phase`` and restart its wait from ``now``."""
        if phase not in PHASE_TIMEOUTS and phase not in UNBOUNDED_PHASES:
            raise ValueError(f"unknown transfer phase {phase!r}")
        self._phase = phase
        self._fired = None
        self.progress(now)

    def progress(self, now):
        """Record activity in the current phase; the wait restarts from ``now``."""
        now = float(now)
        self._anchor = now
        seconds = self._timeouts.for_phase(self._phase)
        self._deadline = None if seconds is None else now + seconds

    def remaining(self, now):
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - float(now))

    def check(self, now):
        """Return the fired timeout name when ``now >= deadline``; else None.

        A deadline fires exactly once. ``enter`` or ``progress`` arms a new one.
        """
        if self._deadline is None or self._fired is not None:
            return None
        if float(now) >= self._deadline:
            self._fired = PHASE_TIMEOUTS[self._phase]
            return self._fired
        return None


class TransferRateTracker:
    """EWMA of bytes per second; paused intervals are excluded from the estimate."""

    def __init__(self, alpha=0.3):
        alpha = float(alpha)
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be within (0, 1]")
        self._alpha = alpha
        self._rate = None
        self._last = None
        self._pending = 0
        self._paused = False

    @property
    def paused(self):
        return self._paused

    @property
    def rate_bytes_per_s(self):
        return 0.0 if self._rate is None else float(self._rate)

    def record(self, byte_count, now):
        """Account ``byte_count`` transferred at ``now`` (monotonic seconds)."""
        byte_count = int(byte_count)
        if byte_count < 0:
            raise ValueError("byte_count must not be negative")
        now = float(now)
        if self._paused or self._last is None:
            # The first sample after start or after a pause anchors the clock;
            # its bytes have no measurable interval and are not rated.
            self._paused = False
            self._last = now
            self._pending = 0
            return
        delta = now - self._last
        if delta <= 0:
            self._pending += byte_count
            return
        instantaneous = (self._pending + byte_count) / delta
        if self._rate is None:
            self._rate = instantaneous
        else:
            self._rate = self._alpha * instantaneous + (1.0 - self._alpha) * self._rate
        self._last = now
        self._pending = 0

    def pause(self):
        """Freeze the estimate; time until the next ``record`` is not counted."""
        self._paused = True
        self._pending = 0

    def eta_seconds(self, remaining_bytes):
        remaining = int(remaining_bytes)
        if remaining <= 0:
            return 0.0
        if self._rate is None or self._rate <= 0:
            return None
        return remaining / self._rate
