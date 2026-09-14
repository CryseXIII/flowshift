"""Validated Action Registry for the FlowShift Command Wheel (pure, testable).

The registry is the only source of actions the overlay may trigger. React never
executes operating-system actions itself; it sends an ``action_id`` to the
Web-API, which validates it here and lets the runtime perform the effect.

Action kinds:

- ``keys``: local keyboard injection into the previously focused window. The
  ``sequence`` is a list of ``(vk, kind)`` steps where ``kind`` is ``"down"``
  or ``"up"``; the runtime converts them to ``inject_queue`` events.
- ``runtime``: a named runtime operation (open clipboard overlay, sync).

The wheel configuration lives in ``config["command_wheel"]``.
"""
from __future__ import annotations

import copy

from runtime_model import MOD_ALT, MOD_CTRL, MOD_SHIFT, MOD_WIN, format_hotkey

REGISTRY_VERSION = 1
MAX_SLOTS_PER_PAGE = 8
MAX_PAGES = 16
ACTION_ID_MAX_LENGTH = 64
CONTEXT_MAX_KEYS = 16
CONTEXT_VALUE_MAX_LENGTH = 256

VK_CONTROL = 0x11
VK_DELETE = 0x2E
VK_SPACE = 0x20
_ALL_MODS = MOD_CTRL | MOD_SHIFT | MOD_ALT | MOD_WIN


class ActionError(ValueError):
    """A rejected action id, wheel configuration, or execution request."""

    def __init__(self, code, message):
        self.code = str(code)
        super().__init__(str(message))


def _ctrl_tap(vk):
    return ((VK_CONTROL, "down"), (vk, "down"), (vk, "up"), (VK_CONTROL, "up"))


def _tap(vk):
    return ((vk, "down"), (vk, "up"))


# Ordered so the default wheel page is meaningful.
_ACTIONS = (
    {"id": "copy", "label": "Copy", "icon": "copy", "kind": "keys",
     "sequence": _ctrl_tap(0x43)},
    {"id": "paste", "label": "Paste", "icon": "paste", "kind": "keys",
     "sequence": _ctrl_tap(0x56)},
    {"id": "cut", "label": "Cut", "icon": "scissors", "kind": "keys",
     "sequence": _ctrl_tap(0x58)},
    {"id": "delete", "label": "Delete", "icon": "trash", "kind": "keys",
     "sequence": _tap(VK_DELETE)},
    {"id": "select_all", "label": "Select all", "icon": "object-group", "kind": "keys",
     "sequence": _ctrl_tap(0x41)},
    {"id": "undo", "label": "Undo", "icon": "rotate-left", "kind": "keys",
     "sequence": _ctrl_tap(0x5A)},
    {"id": "redo", "label": "Redo", "icon": "rotate-right", "kind": "keys",
     "sequence": _ctrl_tap(0x59)},
    {"id": "open_clipboard", "label": "Clipboard", "icon": "clipboard-list",
     "kind": "runtime", "operation": "open_clipboard"},
    {"id": "clipboard_sync", "label": "Sync clipboard", "icon": "cloud-arrow-down",
     "kind": "runtime", "operation": "clipboard_sync"},
)
_BY_ID = {action["id"]: action for action in _ACTIONS}
assert len(_BY_ID) == len(_ACTIONS), "duplicate action id"

DEFAULT_PAGES = (
    ("copy", "paste", "cut", "delete", "select_all", "undo", "redo", "open_clipboard"),
    ("clipboard_sync",),
)
DEFAULT_HOTKEY = {"mods": MOD_CTRL | MOD_ALT, "vk": VK_SPACE}


def action_ids():
    return tuple(action["id"] for action in _ACTIONS)


def get_action(action_id):
    """Return the immutable definition for ``action_id`` or raise ActionError."""
    if not isinstance(action_id, str):
        raise ActionError("invalid_action", "action_id must be a string")
    text = action_id.strip()
    if not text or len(text) > ACTION_ID_MAX_LENGTH:
        raise ActionError("invalid_action", "action_id has an invalid length")
    action = _BY_ID.get(text)
    if action is None:
        raise ActionError("unknown_action", f"unknown action: {text}")
    return action


def public_actions():
    """Public projection for the Web-API and the React overlay."""
    return [
        {"id": a["id"], "label": a["label"], "icon": a["icon"], "kind": a["kind"]}
        for a in _ACTIONS
    ]


def key_events_for(action):
    """Translate a ``keys`` action into ``inject_queue`` events."""
    if action["kind"] != "keys":
        raise ActionError("not_key_action", f"{action['id']} is not a keyboard action")
    events = []
    for vk, step in action["sequence"]:
        events.append({"type": "key" if step == "down" else "key_up", "code": int(vk)})
    return events


def _normalize_page(page, index):
    if not isinstance(page, (list, tuple)):
        raise ActionError("invalid_pages", f"page {index} must be a list")
    if len(page) > MAX_SLOTS_PER_PAGE:
        raise ActionError("too_many_slots",
                          f"page {index} has more than {MAX_SLOTS_PER_PAGE} slots")
    slots = []
    for slot in page:
        if slot is None or slot == "":
            continue
        slots.append(get_action(slot)["id"])
    return slots


def normalize_pages(value):
    """Validate wheel pages; ``None`` yields the defaults. Empty pages are dropped."""
    if value is None:
        return [list(page) for page in DEFAULT_PAGES]
    if not isinstance(value, (list, tuple)):
        raise ActionError("invalid_pages", "pages must be a list of pages")
    if len(value) > MAX_PAGES:
        raise ActionError("too_many_pages", f"more than {MAX_PAGES} pages")
    pages = [slots for slots in (_normalize_page(p, i) for i, p in enumerate(value)) if slots]
    if not pages:
        raise ActionError("empty_wheel", "the wheel needs at least one action")
    return pages


def normalize_hotkey(value):
    """Validate ``{"mods", "vk"}``; ``None`` yields the default hotkey."""
    if value is None:
        return dict(DEFAULT_HOTKEY)
    if not isinstance(value, dict) or set(value) != {"mods", "vk"}:
        raise ActionError("invalid_hotkey", "hotkey must contain exactly mods and vk")
    mods, vk = value["mods"], value["vk"]
    for name, item in (("mods", mods), ("vk", vk)):
        if isinstance(item, bool) or not isinstance(item, int):
            raise ActionError("invalid_hotkey", f"hotkey {name} must be an integer")
    if mods & ~_ALL_MODS or mods == 0:
        raise ActionError("invalid_hotkey", "hotkey needs at least one valid modifier")
    if not 0x08 <= vk <= 0xFE:
        raise ActionError("invalid_hotkey", "hotkey vk is out of range")
    return {"mods": mods, "vk": vk}


def normalize_wheel_config(value):
    """Return the validated ``command_wheel`` config block (defaults applied)."""
    block = value if isinstance(value, dict) else {}
    return {
        "pages": normalize_pages(block.get("pages")),
        "hotkey": normalize_hotkey(block.get("hotkey")),
    }


def wheel_config_from(config):
    """Read the wheel block from a full config; invalid stored data falls back to defaults."""
    raw = config.get("command_wheel") if isinstance(config, dict) else None
    try:
        return normalize_wheel_config(raw)
    except ActionError:
        return normalize_wheel_config(None)


def wheel_payload(config):
    """Data handed to the overlay host for ``command_wheel`` shows."""
    wheel = wheel_config_from(config)
    labels = {a["id"]: {"label": a["label"], "icon": a["icon"]} for a in _ACTIONS}
    return {
        "registry_version": REGISTRY_VERSION,
        "pages": wheel["pages"],
        "actions": {aid: labels[aid] for page in wheel["pages"] for aid in page},
        "hotkey": format_hotkey(wheel["hotkey"]["mods"], wheel["hotkey"]["vk"]),
    }


def validate_execute_request(body):
    """Validate an execute request body; returns ``(action, context)``."""
    if not isinstance(body, dict):
        raise ActionError("invalid_request", "request body must be an object")
    if not set(body).issubset({"action_id", "context"}):
        raise ActionError("invalid_request", "request allows only action_id and context")
    action = get_action(body.get("action_id"))
    context = body.get("context", {})
    if context is None:
        context = {}
    if not isinstance(context, dict) or len(context) > CONTEXT_MAX_KEYS:
        raise ActionError("invalid_context", "context must be a small object")
    clean = {}
    for key, item in context.items():
        if not isinstance(key, str) or not key or len(key) > ACTION_ID_MAX_LENGTH:
            raise ActionError("invalid_context", "context keys must be short strings")
        if isinstance(item, bool) or item is None or isinstance(item, (int, float)):
            clean[key] = item
        elif isinstance(item, str) and len(item) <= CONTEXT_VALUE_MAX_LENGTH:
            clean[key] = item
        else:
            raise ActionError("invalid_context", f"context value {key} is not allowed")
    return copy.deepcopy(action), clean
