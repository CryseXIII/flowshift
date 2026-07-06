"""FlowShift key-code mapping (platform neutral).

The productive Windows wire currently carries raw Windows virtual-key codes.
For a cross-platform FlowShift agent (Linux evdev/uinput etc.) the protocol must
not be bound to Windows codes. This module provides the mapping layer:

    Windows VK   <->  canonical key name  <->  Linux evdev code

Canonical names follow the W3C ``KeyboardEvent.code`` convention (``KeyA``,
``Digit1``, ``ArrowLeft``, ``ControlLeft`` ...). The tables here are a solid,
accurate subset; unknown codes fall back to ``None`` (callers keep the raw
``native_code`` so nothing is lost).

Nothing in here imports platform APIs, so it is unit-testable on any OS.
"""
from __future__ import annotations

# Each entry: canonical -> (windows_vk, linux_evdev_code)
# windows_vk uses the *distinct* (sided) VK where one exists.
# linux_evdev_code values are from <linux/input-event-codes.h>.
_KEYMAP = {
    # Letters
    "KeyA": (0x41, 30), "KeyB": (0x42, 48), "KeyC": (0x43, 46), "KeyD": (0x44, 32),
    "KeyE": (0x45, 18), "KeyF": (0x46, 33), "KeyG": (0x47, 34), "KeyH": (0x48, 35),
    "KeyI": (0x49, 23), "KeyJ": (0x4A, 36), "KeyK": (0x4B, 37), "KeyL": (0x4C, 38),
    "KeyM": (0x4D, 50), "KeyN": (0x4E, 49), "KeyO": (0x4F, 24), "KeyP": (0x50, 25),
    "KeyQ": (0x51, 16), "KeyR": (0x52, 19), "KeyS": (0x53, 31), "KeyT": (0x54, 20),
    "KeyU": (0x55, 22), "KeyV": (0x56, 47), "KeyW": (0x57, 17), "KeyX": (0x58, 45),
    "KeyY": (0x59, 21), "KeyZ": (0x5A, 44),
    # Digits (top row)
    "Digit1": (0x31, 2), "Digit2": (0x32, 3), "Digit3": (0x33, 4), "Digit4": (0x34, 5),
    "Digit5": (0x35, 6), "Digit6": (0x36, 7), "Digit7": (0x37, 8), "Digit8": (0x38, 9),
    "Digit9": (0x39, 10), "Digit0": (0x30, 11),
    # Function keys
    "F1": (0x70, 59), "F2": (0x71, 60), "F3": (0x72, 61), "F4": (0x73, 62),
    "F5": (0x74, 63), "F6": (0x75, 64), "F7": (0x76, 65), "F8": (0x77, 66),
    "F9": (0x78, 67), "F10": (0x79, 68), "F11": (0x7A, 87), "F12": (0x7B, 88),
    # Editing / navigation
    "Enter": (0x0D, 28), "Escape": (0x1B, 1), "Space": (0x20, 57), "Tab": (0x09, 15),
    "Backspace": (0x08, 14), "Delete": (0x2E, 111), "Insert": (0x2D, 110),
    "Home": (0x24, 102), "End": (0x23, 107), "PageUp": (0x21, 104), "PageDown": (0x22, 109),
    "ArrowLeft": (0x25, 105), "ArrowUp": (0x26, 103),
    "ArrowRight": (0x27, 106), "ArrowDown": (0x28, 108),
    # Modifiers (sided)
    "ControlLeft": (0xA2, 29), "ControlRight": (0xA3, 97),
    "ShiftLeft": (0xA0, 42), "ShiftRight": (0xA1, 54),
    "AltLeft": (0xA4, 56), "AltRight": (0xA5, 100),
    "MetaLeft": (0x5B, 125), "MetaRight": (0x5C, 126),
}

# Forward tables
CANONICAL_TO_WIN_VK = {name: vk for name, (vk, _ev) in _KEYMAP.items()}
CANONICAL_TO_EVDEV = {name: ev for name, (_vk, ev) in _KEYMAP.items()}

# Reverse tables (sided entries win; add the generic Windows modifier VKs so the
# low-level hook's non-sided Ctrl/Shift/Alt still map to a canonical name).
WIN_VK_TO_CANONICAL = {vk: name for name, (vk, _ev) in _KEYMAP.items()}
WIN_VK_TO_CANONICAL.setdefault(0x11, "ControlLeft")  # generic VK_CONTROL
WIN_VK_TO_CANONICAL.setdefault(0x10, "ShiftLeft")    # generic VK_SHIFT
WIN_VK_TO_CANONICAL.setdefault(0x12, "AltLeft")      # generic VK_MENU

EVDEV_TO_CANONICAL = {ev: name for name, (_vk, ev) in _KEYMAP.items()}


def win_vk_to_canonical(vk):
    return WIN_VK_TO_CANONICAL.get(vk)


def canonical_to_win_vk(name):
    return CANONICAL_TO_WIN_VK.get(name)


def evdev_to_canonical(code):
    return EVDEV_TO_CANONICAL.get(code)


def canonical_to_evdev(name):
    return CANONICAL_TO_EVDEV.get(name)


def from_native(code, os_name):
    """Native key code -> canonical name (or ``None``)."""
    if os_name == "windows":
        return win_vk_to_canonical(code)
    if os_name == "linux":
        return evdev_to_canonical(code)
    return None


def to_native(name, os_name):
    """Canonical name -> native key code for ``os_name`` (or ``None``)."""
    if os_name == "windows":
        return canonical_to_win_vk(name)
    if os_name == "linux":
        return canonical_to_evdev(name)
    return None


# ── Mouse buttons ───────────────────────────────────────────────────
# Windows tray uses integer button ids (0=L,1=R,2=M). Canonical names are used
# on the wire so Linux/other backends are not tied to the Windows numbering.
_BUTTON_ID_TO_NAME = {0: "left", 1: "right", 2: "middle", 3: "x1", 4: "x2"}
_BUTTON_NAME_TO_ID = {v: k for k, v in _BUTTON_ID_TO_NAME.items()}


def button_id_to_name(bid):
    return _BUTTON_ID_TO_NAME.get(bid, "left")


def button_name_to_id(name):
    return _BUTTON_NAME_TO_ID.get(name, 0)
