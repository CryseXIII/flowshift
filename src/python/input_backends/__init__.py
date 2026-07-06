"""FlowShift input backend abstraction.

A backend encapsulates OS-specific input *capture* and *injection*. It also
reports its identity (os / desktop / backend id) and concrete capabilities so
they can be advertised in the ``hello`` handshake.

Current status:

* ``windows_win32`` – reports full capabilities and screen info. The productive
  capture/injection still lives in ``tray.py`` (native Win32 hooks + SendInput);
  the backend intentionally does not duplicate it yet.
* ``linux_stub`` – NOT implemented. Reports honestly that it cannot capture or
  inject and which privileges (uinput / evdev) it will need. Never crashes.
* ``unsupported`` – controlled errors on every input operation.

``get_backend()`` selects the right backend for the current OS. Importing this
package must never crash on a non-Windows system.
"""
from __future__ import annotations

import platform_capabilities as _caps

from .base import InputBackend, BackendUnavailable
from .unsupported import UnsupportedBackend
from .linux_stub import LinuxStubBackend
from .windows_win32 import WindowsWin32Backend


def get_backend(os_name=None):
    """Return an :class:`InputBackend` instance for ``os_name`` (default: local)."""
    os_name = os_name or _caps.detect_os()
    if os_name == "windows":
        return WindowsWin32Backend()
    if os_name == "linux":
        return LinuxStubBackend()
    return UnsupportedBackend(os_name)


__all__ = [
    "InputBackend",
    "BackendUnavailable",
    "UnsupportedBackend",
    "LinuxStubBackend",
    "WindowsWin32Backend",
    "get_backend",
]
