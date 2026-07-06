"""Input backend interface + shared helpers."""
from __future__ import annotations

import platform_capabilities as _caps


class BackendUnavailable(RuntimeError):
    """Raised when an input operation is not available on this backend."""


class InputBackend:
    """Interface every FlowShift input backend implements.

    Subclasses set ``os_name`` / ``desktop`` / ``input_backend`` and override the
    capability flags plus the operations they support. The default operations
    raise :class:`BackendUnavailable` so an unimplemented backend fails in a
    controlled, non-crashing way instead of blowing up with a random error.
    """

    os_name = "unknown"
    desktop = "unknown"
    input_backend = "unsupported"

    # Capability flags (see platform_capabilities.CAPABILITY_KEYS).
    keyboard_capture = False
    mouse_capture = False
    keyboard_inject = False
    mouse_inject = False
    screen_info = False
    requires_privileged_helper = False
    requires_uinput = False
    requires_evdev = False

    def get_capabilities(self) -> dict:
        return {
            "keyboard_capture": self.keyboard_capture,
            "mouse_capture": self.mouse_capture,
            "keyboard_inject": self.keyboard_inject,
            "mouse_inject": self.mouse_inject,
            "screen_info": self.screen_info,
            "requires_privileged_helper": self.requires_privileged_helper,
            "requires_uinput": self.requires_uinput,
            "requires_evdev": self.requires_evdev,
        }

    def describe(self) -> dict:
        return {
            "os": self.os_name,
            "desktop": self.desktop,
            "input_backend": self.input_backend,
            "capabilities": self.get_capabilities(),
        }

    def build_hello(self, device_id, display_name, screen, port=None, msg_type="hello") -> dict:
        return _caps.build_hello_from_backend(
            device_id, display_name, screen, self, port=port, msg_type=msg_type
        )

    # ── Operations (override in real backends) ──────────────────────
    def get_screen_info(self) -> dict:
        raise BackendUnavailable(f"{self.input_backend}: screen info not available")

    def capture_start(self, on_event):
        raise BackendUnavailable(f"{self.input_backend}: input capture not implemented")

    def capture_stop(self):
        # Safe no-op by default so cleanup paths never crash.
        return None

    def inject_event(self, event: dict):
        raise BackendUnavailable(f"{self.input_backend}: input injection not implemented")

    def release_all(self):
        # Safe no-op by default (nothing was injected).
        return None
