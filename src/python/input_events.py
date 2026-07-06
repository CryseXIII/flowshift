"""FlowShift platform-neutral input events.

The productive Windows wire uses ``{"type": "key", "code": <win_vk>}`` style
events. To prepare a cross-platform agent (Linux evdev/uinput) the canonical,
OS-independent event shape is defined here together with converters to/from the
current Windows event dicts.

Canonical event (``kind``-based)::

    {
      "kind": "key_down|key_up|mouse_move|mouse_down|mouse_up|wheel",
      "code": "KeyA",            # canonical key name (key events)
      "native_code": 65,          # original OS code (loss-less fallback)
      "os": "windows",           # OS that produced native_code
      "button": "left",          # mouse button events
      "mode": "absolute",        # mouse_move: absolute | relative
      "x": 100, "y": 200,         # absolute mouse position
      "dx": 0, "dy": 0,           # relative mouse motion
      "delta": 120,               # wheel
      "source_screen": { ... },
      "modifiers": {"ctrl": false, "alt": false, "shift": false, "meta": false}
    }

Design rule for injection: if the receiver's OS equals the event's ``os`` and a
``native_code`` is present, use it directly; otherwise map via the canonical
``code``. So the Windows↔Windows path is loss-less and a future Linux backend
plugs in without a protocol break.

Pure module, safe to import on any OS.
"""
from __future__ import annotations

import keymap

_WIN_TYPE_TO_KIND = {
    "key": "key_down",
    "key_up": "key_up",
    "mousemove": "mouse_move",
    "mousedown": "mouse_down",
    "mouseup": "mouse_up",
    "wheel": "wheel",
}
_KIND_TO_WIN_TYPE = {v: k for k, v in _WIN_TYPE_TO_KIND.items()}


def empty_modifiers():
    return {"ctrl": False, "alt": False, "shift": False, "meta": False}


def win_event_to_neutral(ev, os_name="windows", modifiers=None):
    """Convert a current Windows tray event dict to a canonical event."""
    t = ev.get("type")
    kind = _WIN_TYPE_TO_KIND.get(t)
    if kind is None:
        return None
    out = {"kind": kind, "os": os_name, "modifiers": modifiers or empty_modifiers()}
    if kind in ("key_down", "key_up"):
        vk = ev.get("code")
        out["native_code"] = vk
        out["code"] = keymap.from_native(vk, os_name)
    elif kind == "mouse_move":
        if ev.get("mode") == "relative" or "dx" in ev or "dy" in ev:
            out["mode"] = "relative"
            out["dx"] = ev.get("dx", 0)
            out["dy"] = ev.get("dy", 0)
        else:
            out["mode"] = "absolute"
            out["x"] = ev.get("x", 0)
            out["y"] = ev.get("y", 0)
            if ev.get("source_screen"):
                out["source_screen"] = ev["source_screen"]
    elif kind in ("mouse_down", "mouse_up"):
        out["button"] = keymap.button_id_to_name(ev.get("button", 0))
    elif kind == "wheel":
        out["delta"] = ev.get("delta", 0)
    return out


def neutral_to_win_event(neutral):
    """Convert a canonical event back to a Windows tray event dict.

    Uses ``native_code`` when the source OS was Windows, else maps the canonical
    name to a Windows VK. Returns ``None`` if the key cannot be resolved.
    """
    kind = neutral.get("kind")
    t = _KIND_TO_WIN_TYPE.get(kind)
    if t is None:
        return None
    if kind in ("key_down", "key_up"):
        vk = None
        if neutral.get("os") == "windows" and isinstance(neutral.get("native_code"), int):
            vk = neutral["native_code"]
        if vk is None:
            vk = keymap.canonical_to_win_vk(neutral.get("code"))
        if vk is None:
            return None
        return {"type": t, "code": vk}
    if kind == "mouse_move":
        if neutral.get("mode") == "relative":
            return {
                "type": t,
                "dx": neutral.get("dx", 0),
                "dy": neutral.get("dy", 0),
                "mode": "relative",
            }
        out = {"type": t, "x": neutral.get("x", 0), "y": neutral.get("y", 0)}
        if neutral.get("source_screen"):
            out["source_screen"] = neutral["source_screen"]
        return out
    if kind in ("mouse_down", "mouse_up"):
        return {"type": t, "button": keymap.button_name_to_id(neutral.get("button", "left"))}
    if kind == "wheel":
        return {"type": t, "delta": neutral.get("delta", 0)}
    return None
