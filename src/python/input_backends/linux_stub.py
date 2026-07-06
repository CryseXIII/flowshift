"""Linux input backend – STUB (not implemented yet).

The real Linux backend will capture via ``evdev`` (``/dev/input/event*``) and
inject via ``uinput`` (``/dev/uinput``). See ``docs/linux_backend_plan.md``.

Until that exists, this stub reports honestly that it cannot capture or inject
and which privileges it will require. It never pretends to work and never
crashes the import or the cleanup paths.
"""
from __future__ import annotations

import platform_capabilities as _caps

from .base import InputBackend, BackendUnavailable


class LinuxStubBackend(InputBackend):
    os_name = "linux"
    input_backend = "evdev_uinput"

    # Not implemented -> cannot capture/inject yet, but declare the requirements.
    keyboard_capture = False
    mouse_capture = False
    keyboard_inject = False
    mouse_inject = False
    screen_info = False
    requires_privileged_helper = True
    requires_uinput = True
    requires_evdev = True

    def __init__(self):
        self.desktop = _caps.detect_desktop("linux")

    def get_screen_info(self) -> dict:
        raise BackendUnavailable(
            "linux evdev/uinput backend not implemented: no screen info yet"
        )

    def capture_start(self, on_event):
        raise BackendUnavailable(
            "linux evdev/uinput backend not implemented: capture requires read "
            "access to /dev/input/event* (evdev) and a privileged helper"
        )

    def inject_event(self, event: dict):
        # Controlled, non-crashing failure: clearly unsupported for now.
        raise BackendUnavailable(
            "linux evdev/uinput backend not implemented: injection requires "
            "/dev/uinput access (see docs/linux_backend_plan.md)"
        )

    def release_all(self):
        # Nothing was ever injected; cleanup must never crash.
        return None
