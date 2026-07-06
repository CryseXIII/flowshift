"""FlowShift platform capability model + ``hello`` handshake (protocol v1).

Prepares FlowShift for a cross-platform agent. Every peer advertises its OS,
desktop, input backend and concrete capabilities in the ``hello`` message so the
other side knows what the peer can actually do (capture / inject / screen info)
and what it still needs (uinput / evdev / a privileged helper).

Backward compatibility: old peers send a minimal ``hello`` without these fields.
``parse_hello`` fills conservative defaults so such peers are handled tolerantly
and never crash the runtime.

Pure module (only ``os``/``sys``), safe to import on any OS.
"""
from __future__ import annotations

import os
import sys

PROTOCOL_VERSION = 1

CAPABILITY_KEYS = (
    "keyboard_capture",
    "mouse_capture",
    "keyboard_inject",
    "mouse_inject",
    "screen_info",
    "requires_privileged_helper",
    "requires_uinput",
    "requires_evdev",
)


# ── Detection (local machine) ───────────────────────────────────────
def detect_os():
    p = sys.platform
    if p == "win32":
        return "windows"
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "macos"
    return "unknown"


def detect_desktop(os_name=None):
    os_name = os_name or detect_os()
    if os_name == "windows":
        return "win32"
    if os_name == "linux":
        if os.environ.get("WAYLAND_DISPLAY"):
            return "wayland"
        if os.environ.get("DISPLAY"):
            return "x11"
        return "unknown"
    return "unknown"


def detect_input_backend(os_name=None):
    """Intended input backend id for this OS (may not be functional yet)."""
    os_name = os_name or detect_os()
    if os_name == "windows":
        return "win32"
    if os_name == "linux":
        return "evdev_uinput"
    return "unsupported"


# ── Defaults for remote peers that omit fields (tolerant parsing) ────
def default_desktop_for_os(os_name):
    return "win32" if os_name == "windows" else "unknown"


def default_input_backend_for_os(os_name):
    if os_name == "windows":
        return "win32"
    if os_name == "linux":
        return "evdev_uinput"
    return "unsupported"


def default_capabilities(os_name):
    """Conservative capability guess for a peer that did not advertise any.

    Only Windows peers are assumed input-capable (that is the productive path).
    Everything else defaults to "cannot do it" until proven otherwise, so we
    never over-claim a remote peer's abilities.
    """
    caps = {k: False for k in CAPABILITY_KEYS}
    if os_name == "windows":
        caps.update(
            keyboard_capture=True,
            mouse_capture=True,
            keyboard_inject=True,
            mouse_inject=True,
            screen_info=True,
        )
    return caps


def normalize_capabilities(raw, os_name):
    """Return a full capability dict.

    ``None`` -> conservative defaults for ``os_name``. A partial dict is merged
    onto an all-false template (unknown keys ignored, missing keys -> False).
    """
    if not isinstance(raw, dict):
        return default_capabilities(os_name)
    caps = {k: False for k in CAPABILITY_KEYS}
    for k in CAPABILITY_KEYS:
        if k in raw:
            caps[k] = bool(raw[k])
    return caps


def normalize_screen(spec):
    """Accept ``{left,top,...}`` or ``{x,y,...}`` and return a canonical dict.

    The returned dict carries both ``x/y`` (canonical, per protocol v1) and
    ``left/top`` (kept for the existing mouse-scaling code) so it is readable by
    old and new peers alike.
    """
    if not isinstance(spec, dict):
        return None
    left = spec.get("left", spec.get("x", 0))
    top = spec.get("top", spec.get("y", 0))
    width = spec.get("width", 0)
    height = spec.get("height", 0)
    try:
        left, top, width, height = int(left), int(top), int(width), int(height)
    except (TypeError, ValueError):
        return None
    return {"x": left, "y": top, "left": left, "top": top, "width": width, "height": height}


# ── hello build / parse ─────────────────────────────────────────────
def build_hello(device_id, display_name, screen, os_name, desktop,
                input_backend, capabilities, port=None, msg_type="hello"):
    hello = {
        "type": msg_type,
        "protocol_version": PROTOCOL_VERSION,
        "device_id": device_id or "",
        "display_name": display_name or "",
        "os": os_name,
        "desktop": desktop,
        "input_backend": input_backend,
        "screen": normalize_screen(screen) or {},
        "capabilities": normalize_capabilities(capabilities, os_name),
    }
    if port is not None:
        hello["port"] = port
    return hello


def build_hello_from_backend(device_id, display_name, screen, backend,
                             port=None, msg_type="hello"):
    return build_hello(
        device_id, display_name, screen,
        backend.os_name, backend.desktop, backend.input_backend,
        backend.get_capabilities(), port=port, msg_type=msg_type,
    )


def parse_hello(msg):
    """Parse a possibly-old ``hello``/``ping``/``pong`` into a full record."""
    msg = msg or {}
    os_name = msg.get("os") or "unknown"
    return {
        "type": msg.get("type", "hello"),
        "protocol_version": int(msg.get("protocol_version", 0) or 0),
        "device_id": msg.get("device_id", "") or "",
        "display_name": msg.get("display_name", "") or "",
        "os": os_name,
        "desktop": msg.get("desktop") or default_desktop_for_os(os_name),
        "input_backend": msg.get("input_backend") or default_input_backend_for_os(os_name),
        "screen": normalize_screen(msg.get("screen")),
        "capabilities": normalize_capabilities(msg.get("capabilities"), os_name),
    }
