"""Fallback backend for unsupported operating systems."""
from __future__ import annotations

from .base import InputBackend, BackendUnavailable


class UnsupportedBackend(InputBackend):
    """No capture/injection. Every input operation raises a clear error."""

    input_backend = "unsupported"

    def __init__(self, os_name="unknown", desktop="unknown"):
        self.os_name = os_name
        self.desktop = desktop

    def get_screen_info(self) -> dict:
        raise BackendUnavailable(
            f"unsupported platform {self.os_name!r}: no screen info"
        )

    def capture_start(self, on_event):
        raise BackendUnavailable(
            f"unsupported platform {self.os_name!r}: input capture unavailable"
        )

    def inject_event(self, event: dict):
        raise BackendUnavailable(
            f"unsupported platform {self.os_name!r}: input injection unavailable"
        )
