"""Windows Win32 input backend.

Reports full input capabilities and real screen info (virtual desktop rect).
The productive capture + injection path currently lives natively in ``tray.py``
(low-level hooks + ``SendInput``); this backend deliberately does not duplicate
that logic yet, so ``capture_start`` / ``inject_event`` report that the native
runtime owns those operations.

Safe to import on any OS: ``ctypes.windll`` is only touched inside methods.
"""
from __future__ import annotations

import ctypes

from .base import InputBackend, BackendUnavailable


class WindowsWin32Backend(InputBackend):
    os_name = "windows"
    desktop = "win32"
    input_backend = "win32"

    keyboard_capture = True
    mouse_capture = True
    keyboard_inject = True
    mouse_inject = True
    screen_info = True
    requires_privileged_helper = False
    requires_uinput = False
    requires_evdev = False

    def get_screen_info(self) -> dict:
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            left = int(user32.GetSystemMetrics(76))   # SM_XVIRTUALSCREEN
            top = int(user32.GetSystemMetrics(77))    # SM_YVIRTUALSCREEN
            width = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
            height = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
        except Exception as e:  # pragma: no cover - only on non-Windows
            raise BackendUnavailable(f"win32 screen info unavailable: {e!r}")
        return {"x": left, "y": top, "left": left, "top": top,
                "width": max(1, width), "height": max(1, height)}

    def capture_start(self, on_event):
        raise BackendUnavailable(
            "win32 capture is handled by the native tray.py hook thread"
        )

    def inject_event(self, event: dict):
        raise BackendUnavailable(
            "win32 injection is handled by the native tray.py SendInput path"
        )
