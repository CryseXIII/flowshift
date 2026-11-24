"""Registration status of the overlay hotkeys (clipboard overlay, command wheel).

Each hotkey is a trigger of ``input_trigger``: a key (``RegisterHotKey``) or a
mouse button (the always-on low-level mouse hook). Cleared hotkeys have no
entry at all, so they are never shown as a problem.

``RegisterHotKey`` fails silently from the user's point of view: when Windows
or another program already owns a combination (``ERROR_HOTKEY_ALREADY_REGISTERED``
= 1409) the key press never reaches FlowShift. This module turns the raw
registration results of ``tray.register_runtime_hotkeys`` into a report with
plain-text messages that name the busy hotkey and what works instead. The
report is published in ``GET /api/status`` (``hotkeys``) and shown as a tray
notification. Platform independent so it is unit-testable without Windows.
"""

from __future__ import annotations

import input_trigger as it
from runtime_model import MOD_ALT, MOD_CTRL, MOD_SHIFT, MOD_WIN

ERROR_HOTKEY_ALREADY_REGISTERED = 1409
VK_V = 0x56

DEFAULT_CLIPBOARD_OVERLAY_HOTKEY = {"mods": MOD_CTRL | MOD_ALT, "vk": VK_V}
CTRL_WIN_V = {"mods": MOD_CTRL | MOD_WIN, "vk": VK_V}
WIN_V = {"mods": MOD_WIN, "vk": VK_V}
_ALL_MODS = MOD_CTRL | MOD_SHIFT | MOD_ALT | MOD_WIN

SLOT_CLIPBOARD = "clipboard_overlay"
SLOT_CLIPBOARD_CTRL_WIN_V = "clipboard_ctrl_win_v"
SLOT_CLIPBOARD_WIN_V = "clipboard_win_v"
SLOT_WHEEL = "command_wheel"
# The command wheel has exactly one hotkey (``command_wheel.hotkey``).
WHEEL_SLOTS = (SLOT_WHEEL,)

CLIPBOARD_SLOTS = (SLOT_CLIPBOARD, SLOT_CLIPBOARD_CTRL_WIN_V, SLOT_CLIPBOARD_WIN_V)

TARGET_LABELS = {
    SLOT_CLIPBOARD: "Clipboard overlay",
    SLOT_CLIPBOARD_CTRL_WIN_V: "Clipboard overlay",
    SLOT_CLIPBOARD_WIN_V: "Clipboard overlay (Win+V interception)",
    SLOT_WHEEL: "Command wheel",
}
SETTINGS_HINTS = {
    SLOT_CLIPBOARD: "Settings > Clipboard > Overlay hotkey",
    SLOT_CLIPBOARD_CTRL_WIN_V: "Settings > Clipboard > Overlay hotkey",
    SLOT_CLIPBOARD_WIN_V: "Settings > Clipboard > Overlay hotkey",
    SLOT_WHEEL: "Settings > Command Wheel > Open hotkey",
}
# Always-available fallbacks that do not depend on the configured hotkeys.
WHEEL_FALLBACK = "Ctrl+Right click on the FlowShift tray icon"
CLIPBOARD_FALLBACK = "tray menu > Clipboard"


def normalize_hotkey(value, default=None):
    """Return a valid key or mouse trigger, or ``default`` (copied) when invalid/empty."""
    try:
        trigger = it.normalize(value, key_needs_modifier=True)
    except it.TriggerError:
        trigger = None
    if trigger is not None:
        return trigger
    return dict(default) if isinstance(default, dict) else default


def clipboard_overlay_hotkey(clipboard_settings):
    """Configured clipboard overlay hotkey (default Ctrl+Alt+V); ``None`` when cleared."""
    if not isinstance(clipboard_settings, dict):
        return dict(DEFAULT_CLIPBOARD_OVERLAY_HOTKEY)
    if "overlay_hotkey" in clipboard_settings and clipboard_settings["overlay_hotkey"] is None:
        return None
    return normalize_hotkey(clipboard_settings.get("overlay_hotkey"),
                            DEFAULT_CLIPBOARD_OVERLAY_HOTKEY)


def same_hotkey(a, b):
    return it.same(a, b)


def display(hotkey):
    return it.display(hotkey)


def make_entry(slot, hotkey, ok, err=0, reason=None):
    """Raw result of one registration attempt (key: RegisterHotKey, mouse: mouse hook)."""
    return {
        "slot": slot,
        "target": TARGET_LABELS.get(slot, slot),
        "mods": int(hotkey["mods"]),
        "vk": int(hotkey.get("vk", 0)),
        "mouse": hotkey.get("mouse"),
        "hotkey": display(hotkey),
        "active": bool(ok),
        "error_code": 0 if ok else int(err or 0),
        "reason": None if ok else reason,
    }


def collect_assignments(wheel_triggers, clipboard_hotkey, peer_rows, clipboard_enabled=True):
    """Every set trigger as ``(slot, label, trigger)`` for ``input_trigger.find_conflicts``.

    ``wheel_triggers`` comes from ``overlay_actions.wheel_triggers``,
    ``peer_rows`` is ``config["hotkeys"]``. The fixed kill switch and, while the
    clipboard is on, Ctrl+Win+V take part too.
    """
    out = [(slot, TARGET_LABELS.get(slot, slot), hk) for slot, hk in wheel_triggers]
    if clipboard_hotkey is not None:
        out.append((SLOT_CLIPBOARD, TARGET_LABELS[SLOT_CLIPBOARD], clipboard_hotkey))
    for i, row in enumerate(peer_rows or []):
        trigger = it.peer_hotkey_trigger(row)
        if trigger is not None:
            label = (row.get("label") or row.get("action") or f"Hotkey {i + 1}")
            out.append((f"peer_hotkey_{i}", label, trigger))
    out.append(("kill_switch", "Emergency stop", dict(it.KILL_SWITCH)))
    if clipboard_enabled and not it.same(clipboard_hotkey, it.CTRL_WIN_V):
        out.append((SLOT_CLIPBOARD_CTRL_WIN_V, "Clipboard overlay (Ctrl+Win+V)",
                    dict(it.CTRL_WIN_V)))
    return out


def _failure_reason(entry):
    if entry.get("reason"):
        return entry["reason"]
    if entry["error_code"] == ERROR_HOTKEY_ALREADY_REGISTERED:
        return "is already taken by Windows or another program"
    if entry["error_code"]:
        return f"could not be registered (Windows error {entry['error_code']})"
    return "could not be registered"


def _alternatives(entry, entries):
    if entry["slot"] in CLIPBOARD_SLOTS:
        alts = [e["hotkey"] for e in entries
                if e["slot"] in CLIPBOARD_SLOTS and e["active"] and e["hotkey"] != entry["hotkey"]]
        alts.append(CLIPBOARD_FALLBACK)
    elif entry["slot"] in WHEEL_SLOTS:
        alts = [e["hotkey"] for e in entries
                if e["slot"] in WHEEL_SLOTS and e["active"] and e["hotkey"] != entry["hotkey"]]
        alts.append(WHEEL_FALLBACK)
    else:
        alts = []
    return list(dict.fromkeys(alts))


def build_report(entries):
    """Add ``message``/``alternatives`` to every entry and summarize the conflicts."""
    out = []
    for raw in entries:
        entry = dict(raw)
        if entry["active"]:
            entry["alternatives"] = []
            entry["message"] = f"{entry['hotkey']} opens the {entry['target'].lower()}."
        else:
            alts = _alternatives(entry, entries)
            entry["alternatives"] = alts
            text = f"{entry['hotkey']} ({entry['target']}) {_failure_reason(entry)}."
            if alts:
                text += f" Use {' or '.join(alts)} instead"
                text += f", or choose another hotkey under {SETTINGS_HINTS.get(entry['slot'], 'Settings')}."
            entry["message"] = text
        out.append(entry)
    conflicts = [e for e in out if not e["active"]]
    return {
        "ok": not conflicts,
        "entries": out,
        "conflicts": [e["slot"] for e in conflicts],
        "summary": " ".join(e["message"] for e in conflicts) if conflicts else "",
    }


def notification_text(report, max_len=255):
    """Short tray balloon text (Windows limits szInfo to 256 chars) or ``""``."""
    conflicts = [e for e in report.get("entries", []) if not e.get("active")]
    if not conflicts:
        return ""
    parts = []
    for e in conflicts:
        alts = e.get("alternatives") or []
        part = f"{e['hotkey']} ({e['target']}) is taken"
        if alts:
            part += f", use {alts[0]}"
        parts.append(part + ".")
    text = " ".join(parts)
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


def empty_report():
    return {"ok": True, "entries": [], "conflicts": [], "summary": ""}
