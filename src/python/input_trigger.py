"""Hotkey triggers: a key or a mouse button, each with modifiers (pure, testable).

Every configurable hotkey of FlowShift (command wheel, clipboard overlay, peer
switching, return to local) stores one trigger:

* keyboard: ``{"mods": <bits>, "vk": <virtual key>}``, registered with
  ``RegisterHotKey`` exactly as before;
* mouse: ``{"mods": <bits>, "mouse": "<button>"}``, handled by the always-on
  low-level mouse hook in ``tray.py`` (:class:`MouseTriggerMatcher` decides),
  which swallows the button press and its release when it triggers;
* empty: ``None`` (the hotkey is deliberately cleared, nothing is registered
  and nothing is reported as a problem).

``mods`` uses the runtime bits of ``runtime_model`` (Ctrl=1, Shift=2, Alt=4,
Win=8). A left or right click and the mouse wheel need at least one modifier,
otherwise the computer could no longer be used; middle click and the side
buttons may be used alone.
"""
from __future__ import annotations

import threading
import time

from runtime_model import MOD_ALT, MOD_CTRL, MOD_SHIFT, MOD_WIN, format_hotkey, mods_name

ALL_MODS = MOD_CTRL | MOD_SHIFT | MOD_ALT | MOD_WIN

MOUSE_BUTTONS = ("left", "right", "middle", "x1", "x2", "wheel_up", "wheel_down")
WHEEL_BUTTONS = frozenset({"wheel_up", "wheel_down"})
# Buttons that never work as a trigger on their own.
NEEDS_MODIFIER = frozenset({"left", "right", "wheel_up", "wheel_down"})
MOUSE_LABELS = {
    "left": "Left click",
    "right": "Right click",
    "middle": "Middle click",
    "x1": "Side button 1",
    "x2": "Side button 2",
    "wheel_up": "Wheel up",
    "wheel_down": "Wheel down",
}

# One scroll gesture sends many wheel notches; a wheel trigger fires once.
WHEEL_REPEAT_S = 0.4
# A press whose release never arrives (lost up event) stops being held.
MAX_HELD_S = 60.0

# Fixed hotkeys that are always registered by the runtime.
KILL_SWITCH = {"mods": ALL_MODS, "vk": 0x7B}             # Ctrl+Alt+Shift+Win+F12
CTRL_WIN_V = {"mods": MOD_CTRL | MOD_WIN, "vk": 0x56}


class TriggerError(ValueError):
    """A rejected trigger (``code`` is the API error code)."""

    def __init__(self, code, message):
        self.code = str(code)
        super().__init__(message)


def _int(value):
    return not isinstance(value, bool) and isinstance(value, int)


def normalize(value, *, key_needs_modifier=True, allow_empty=True):
    """Validate a trigger; returns ``None`` (empty), a key or a mouse trigger.

    ``key_needs_modifier`` keeps the rule of the overlay hotkeys (a plain key
    would be taken from every program); peer hotkeys historically allow a key
    alone (F-keys).
    """
    if value is None:
        if allow_empty:
            return None
        raise TriggerError("invalid_hotkey", "a hotkey is required")
    if not isinstance(value, dict):
        raise TriggerError("invalid_hotkey", "hotkey must be an object")
    keys = set(value)
    if keys == {"mods", "vk"}:
        mods, vk = value["mods"], value["vk"]
        if not _int(mods) or not _int(vk):
            raise TriggerError("invalid_hotkey", "hotkey mods and vk must be integers")
        if mods & ~ALL_MODS or (key_needs_modifier and mods == 0):
            raise TriggerError("invalid_hotkey", "hotkey needs at least one valid modifier")
        if not 0x08 <= vk <= 0xFE:
            raise TriggerError("invalid_hotkey", "hotkey vk is out of range")
        return {"mods": mods, "vk": vk}
    if keys == {"mods", "mouse"}:
        mods, button = value["mods"], value["mouse"]
        if not _int(mods) or mods & ~ALL_MODS:
            raise TriggerError("invalid_hotkey", "hotkey mods must be valid modifier bits")
        if button not in MOUSE_BUTTONS:
            raise TriggerError("invalid_hotkey",
                               "mouse must be one of " + ", ".join(MOUSE_BUTTONS))
        if mods == 0 and button in NEEDS_MODIFIER:
            raise TriggerError("mouse_needs_modifier",
                               f"{MOUSE_LABELS[button]} needs Ctrl, Alt, Shift or Win, "
                               "otherwise the computer can no longer be used")
        return {"mods": mods, "mouse": button}
    raise TriggerError("invalid_hotkey", "hotkey must contain mods and vk, or mods and mouse")


def is_mouse(trigger):
    return isinstance(trigger, dict) and "mouse" in trigger


def is_key(trigger):
    return isinstance(trigger, dict) and "vk" in trigger


def identity(trigger):
    """Hashable identity for conflict checks, ``None`` for an empty trigger."""
    if is_key(trigger):
        return ("key", int(trigger["mods"]), int(trigger["vk"]))
    if is_mouse(trigger):
        return ("mouse", int(trigger["mods"]), trigger["mouse"])
    return None


def same(a, b):
    ia = identity(a)
    return ia is not None and ia == identity(b)


def display(trigger):
    """Plain text like ``Ctrl+Alt+V`` or ``Ctrl+Right click``; ``""`` when empty."""
    if is_key(trigger):
        return format_hotkey(trigger["mods"], trigger["vk"])
    if is_mouse(trigger):
        label = MOUSE_LABELS.get(trigger["mouse"], trigger["mouse"])
        prefix = mods_name(trigger["mods"])
        return f"{prefix}+{label}" if prefix else label
    return ""


# ── Peer hotkeys (config["hotkeys"]) ────────────────────────────────
def peer_hotkey_trigger(hk):
    """Trigger of a stored peer hotkey row (``mouse`` or ``key``; key 0 = empty)."""
    if not isinstance(hk, dict):
        return None
    mods = hk.get("mods", MOD_CTRL | MOD_ALT)
    if not _int(mods):
        return None
    if hk.get("mouse") in MOUSE_BUTTONS:
        if mods == 0 and hk["mouse"] in NEEDS_MODIFIER:
            return None
        return {"mods": mods, "mouse": hk["mouse"]}
    key = hk.get("key", 0)
    if _int(key) and 0x08 <= key <= 0xFE:
        return {"mods": mods, "vk": key}
    return None


def normalize_peer_hotkeys(rows):
    """Validate posted peer hotkey rows; mouse rows keep ``key: 0`` for old readers."""
    if not isinstance(rows, list):
        raise TriggerError("invalid_hotkeys", "hotkeys must be a list")
    out = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("action", ""), str):
            raise TriggerError("invalid_hotkeys", "every hotkey needs an action")
        clean = {"action": row.get("action", ""),
                 "label": row.get("label", "") if isinstance(row.get("label", ""), str) else ""}
        mods = row.get("mods", MOD_CTRL | MOD_ALT)
        if row.get("mouse") is not None:
            trig = normalize({"mods": mods, "mouse": row["mouse"]}, key_needs_modifier=False)
            clean.update(mods=trig["mods"], key=0, mouse=trig["mouse"])
        else:
            key = row.get("key", 0)
            if not _int(mods) or mods & ~ALL_MODS or not _int(key) or not 0 <= key <= 0xFE:
                raise TriggerError("invalid_hotkey", "hotkey mods and key must be valid integers")
            clean.update(mods=mods, key=key)
        out.append(clean)
    return out


# ── Conflicts ───────────────────────────────────────────────────────
def find_conflicts(assignments):
    """Triggers assigned more than once.

    ``assignments`` is a list of ``(slot, label, trigger)``; empty triggers are
    ignored. Returns ``[{"hotkey", "slots", "labels", "message"}]``.
    """
    groups = {}
    for slot, label, trigger in assignments:
        ident = identity(trigger)
        if ident is None:
            continue
        groups.setdefault(ident, []).append((slot, label, trigger))
    out = []
    for items in groups.values():
        if len(items) < 2:
            continue
        text = display(items[0][2])
        labels = [label for _slot, label, _t in items]
        out.append({
            "hotkey": text,
            "slots": [slot for slot, _label, _t in items],
            "labels": labels,
            "message": f"{text} is assigned twice: {' and '.join(labels)}. "
                       "Choose another hotkey for one of them.",
        })
    return out


# ── Mouse hook decision ─────────────────────────────────────────────
class MouseTriggerMatcher:
    """Decides in the low-level mouse hook whether a mouse event is a trigger.

    Bindings are ``(trigger, target, when)``: ``when`` is ``"local"`` (only while
    input stays on this PC: command wheel, clipboard overlay, switch to a peer)
    or ``"forwarding"`` (only while forwarding: return to local). ``decide``
    returns ``(decision, target, button)`` with decision

    * ``"press"``: swallow the button press, the trigger starts (hold);
    * ``"release"``: swallow the matching release, the trigger ends;
    * ``"fire"``: one wheel notch triggered (no hold), swallow it;
    * ``"swallow"``: further notches of the same scroll, swallow silently;
    * ``"pass"``: not ours, hand the event on.

    Injected events (FlowShift's own SendInput) always pass. The release of a
    swallowed press is swallowed even when the modifiers changed meanwhile.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._bindings = {}
        self._buttons = frozenset()
        self._held = {}
        self._last_wheel = {}

    def set_bindings(self, bindings):
        table = {}
        for trigger, target, when in bindings:
            if is_mouse(trigger):
                table.setdefault((trigger["mods"], trigger["mouse"], when), target)
        with self._lock:
            self._bindings = table
            self._buttons = frozenset(button for _mods, button, _when in table)

    def interested(self, button):
        """Cheap pre-check for the hook: a bound or currently held button."""
        return button in self._buttons or button in self._held

    def bindings(self):
        with self._lock:
            return dict(self._bindings)

    def held(self):
        with self._lock:
            return dict((button, target) for button, (target, _t) in self._held.items())

    def decide(self, kind, button, mods, injected=False, forwarding_active=False):
        """``kind`` is ``"down"``, ``"up"`` or ``"wheel"`` (button wheel_up/down)."""
        if injected or button not in MOUSE_BUTTONS:
            return "pass", None, button
        now = self._clock()
        when = "forwarding" if forwarding_active else "local"
        with self._lock:
            if kind == "up":
                held = self._held.pop(button, None)
                if held is not None:
                    return "release", held[0], button
                return "pass", None, button
            target = self._bindings.get((mods, button, when))
            if kind == "down":
                if target is None or button in WHEEL_BUTTONS:
                    return "pass", None, button
                held = self._held.get(button)
                if held is not None and now - held[1] < MAX_HELD_S:
                    return "swallow", held[0], button
                self._held[button] = (target, now)
                return "press", target, button
            if kind == "wheel" and button in WHEEL_BUTTONS:
                if target is None:
                    return "pass", None, button
                last = self._last_wheel.get(button)
                self._last_wheel[button] = now
                if last is not None and now - last < WHEEL_REPEAT_S:
                    return "swallow", target, button
                return "fire", target, button
        return "pass", None, button
