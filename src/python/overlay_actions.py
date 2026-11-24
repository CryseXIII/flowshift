"""Validated Action Registry for the FlowShift Command Wheel (pure, testable).

The registry is the only source of actions the overlay may trigger. React never
executes operating-system actions itself; it sends an ``action_id`` to the
Web-API, which validates it here and lets the runtime perform the effect.

Action kinds:

- ``keys``: local keyboard injection into the previously focused window. The
  ``sequence`` is a list of ``(vk, kind)`` steps where ``kind`` is ``"down"``
  or ``"up"``; the runtime converts them to ``inject_queue`` events.
- ``runtime``: a named runtime operation (open clipboard overlay, sync).

The wheel configuration lives in ``config["command_wheel"]`` (``version`` 3):
``pages``, ``hotkey`` (the one trigger that opens the wheel, default Ctrl+right
click, ``None`` = cleared) and ``trigger`` (tap / hold-and-swipe timing). A
trigger is a key or a mouse button with modifiers (``input_trigger``). Older
blocks (without ``version`` before 0.7.0-dev.49, ``version`` 2 with a list of
``extra_hotkeys`` until 0.7.0-dev.50) are migrated by :func:`migrate_wheel_block`.
"""
from __future__ import annotations

import copy

import input_trigger as it
from runtime_model import MOD_ALT, MOD_CTRL, MOD_SHIFT, MOD_WIN
from wheel_gesture import DEFAULT_TRIGGER, HOLD_MS_MAX, HOLD_MS_MIN

REGISTRY_VERSION = 1
MAX_SLOTS_PER_PAGE = 8
MAX_PAGES = 16
ACTION_ID_MAX_LENGTH = 64
CONTEXT_MAX_KEYS = 16
CONTEXT_VALUE_MAX_LENGTH = 256

VK_CONTROL = 0x11
VK_DELETE = 0x2E
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
# Default hotkey of a fresh config: Ctrl+right click (before 0.7.0-dev.49 a fixed
# extra path next to the Ctrl+Alt+Space hotkey, which Windows often owns).
DEFAULT_HOTKEY = {"mods": MOD_CTRL, "mouse": "right"}
WHEEL_CONFIG_VERSION = 3


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
    """Validate one trigger (key or mouse button with modifiers); ``None`` = empty."""
    try:
        return it.normalize(value, key_needs_modifier=True)
    except it.TriggerError as exc:
        raise ActionError(exc.code, str(exc)) from None


def wheel_triggers(wheel):
    """``(slot, trigger)`` of the wheel hotkey of a normalized block (empty when cleared)."""
    if wheel.get("hotkey") is None:
        return []
    return [("command_wheel", wheel["hotkey"])]


def normalize_trigger(value, base=None):
    """Validate the ``trigger`` block; missing keys come from ``base`` or the defaults.

    At least one of ``tap_open`` / ``hold_swipe`` must stay enabled, otherwise
    the wheel could open but never be used.
    """
    if value is None:
        value = {}
    if not isinstance(value, dict) or not set(value).issubset(DEFAULT_TRIGGER):
        raise ActionError("invalid_trigger",
                          "trigger allows only tap_open, hold_swipe and hold_ms")
    trigger = dict(DEFAULT_TRIGGER)
    if isinstance(base, dict):
        trigger.update({k: v for k, v in base.items() if k in DEFAULT_TRIGGER})
    trigger.update(value)
    for name in ("tap_open", "hold_swipe"):
        if not isinstance(trigger[name], bool):
            raise ActionError("invalid_trigger", f"trigger {name} must be true or false")
    hold_ms = trigger["hold_ms"]
    if (isinstance(hold_ms, bool) or not isinstance(hold_ms, int)
            or not HOLD_MS_MIN <= hold_ms <= HOLD_MS_MAX):
        raise ActionError("invalid_trigger",
                          f"trigger hold_ms must be {HOLD_MS_MIN}..{HOLD_MS_MAX}")
    if not trigger["tap_open"] and not trigger["hold_swipe"]:
        raise ActionError("trigger_disabled",
                          "tap to open and hold to swipe cannot both be off")
    return {key: trigger[key] for key in DEFAULT_TRIGGER}


def _first_trigger(values):
    """First valid, set trigger of ``values`` or ``None``."""
    for value in values:
        try:
            trigger = it.normalize(value, key_needs_modifier=True)
        except it.TriggerError:
            continue
        if trigger is not None:
            return trigger
    return None


def migrate_wheel_block(block):
    """Turn a stored wheel block of an older version into the version 3 shape.

    Version 3 has exactly one hotkey. Before 0.7.0-dev.49 (no ``version``)
    Ctrl+right click always opened the wheel next to a keyboard hotkey and an
    optional side button; it stays the hotkey. Version 2 (0.7.0-dev.49/50)
    kept ``hotkey`` plus a list ``extra_hotkeys``: the first set entry of
    ``[hotkey, *extra_hotkeys]`` wins, a cleared ``"hotkey": null`` without
    further entries stays cleared. Version 3 blocks are returned unchanged.
    """
    if not isinstance(block, dict) or block.get("version") == WHEEL_CONFIG_VERSION:
        return block
    out = {"version": WHEEL_CONFIG_VERSION}
    if "pages" in block:
        out["pages"] = block["pages"]
    raw_trigger = block.get("trigger") if isinstance(block.get("trigger"), dict) else {}
    out["trigger"] = {k: v for k, v in raw_trigger.items() if k in DEFAULT_TRIGGER}
    if "version" not in block:
        out["hotkey"] = dict(DEFAULT_HOTKEY)
        return out
    extras = block.get("extra_hotkeys")
    extras = list(extras) if isinstance(extras, (list, tuple)) else []
    if "hotkey" in block:
        out["hotkey"] = _first_trigger([block["hotkey"], *extras])
    else:
        out["hotkey"] = dict(DEFAULT_HOTKEY)
    return out


def normalize_wheel_config(value):
    """Return the validated ``command_wheel`` block (version 3, defaults applied).

    A missing ``hotkey`` key yields the default; ``"hotkey": null`` stays empty
    (cleared in the WebGUI).
    """
    block = value if isinstance(value, dict) else {}
    block = migrate_wheel_block(block) if block else block
    return {
        "version": WHEEL_CONFIG_VERSION,
        "pages": normalize_pages(block.get("pages")),
        "hotkey": (normalize_hotkey(block["hotkey"]) if "hotkey" in block
                   else dict(DEFAULT_HOTKEY)),
        "trigger": normalize_trigger(block.get("trigger")),
    }


def wheel_config_from(config):
    """Read the wheel block from a full config; invalid stored data falls back to defaults.

    The fallback is per field: one invalid part (for example a page with an
    action a later version no longer knows) must not silently put the user's
    hotkey back to the default.
    """
    raw = config.get("command_wheel") if isinstance(config, dict) else None
    try:
        return normalize_wheel_config(raw)
    except ActionError:
        pass
    block = raw if isinstance(raw, dict) else {}
    try:
        block = migrate_wheel_block(block) if block else block
    except (ActionError, it.TriggerError):
        block = {}

    def _field(normalize, value, default):
        try:
            return normalize(value)
        except ActionError:
            return default

    return {
        "version": WHEEL_CONFIG_VERSION,
        "pages": _field(normalize_pages, block.get("pages"),
                        [list(page) for page in DEFAULT_PAGES]),
        # An unreadable stored hotkey counts as cleared: never silently Ctrl+right click.
        "hotkey": (_field(normalize_hotkey, block["hotkey"], None)
                   if "hotkey" in block else dict(DEFAULT_HOTKEY)),
        "trigger": _field(normalize_trigger, block.get("trigger"), dict(DEFAULT_TRIGGER)),
    }


def migrate_wheel_config(config):
    """Store the version 3 wheel block in ``config``; True when it changed (save it)."""
    if not isinstance(config, dict):
        return False
    raw = config.get("command_wheel")
    if isinstance(raw, dict) and raw.get("version") == WHEEL_CONFIG_VERSION:
        return False
    if raw is None:
        return False
    config["command_wheel"] = wheel_config_from(config)
    return True


def wheel_payload(config):
    """Data handed to the overlay host for ``command_wheel`` shows."""
    wheel = wheel_config_from(config)
    labels = {a["id"]: {"label": a["label"], "icon": a["icon"]} for a in _ACTIONS}
    return {
        "registry_version": REGISTRY_VERSION,
        "pages": wheel["pages"],
        "actions": {aid: labels[aid] for page in wheel["pages"] for aid in page},
        "hotkey": it.display(wheel["hotkey"]),
        "trigger": dict(wheel["trigger"]),
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
